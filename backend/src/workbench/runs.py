"""Background agent runs with persisted progress and cooperative cancellation."""

from __future__ import annotations

import logging
import secrets
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .agent.correction import run_correction
from .agent.multisource import run_source_build, run_with_evidence
from .agent.guidance import SkillGuidance
from .config import Settings
from .events import EventBus
from .llm import Cancelled, CancelToken, LLMError, make_client
from .project import Project, StaleRevision
from .questions import readable_questions
from .schemas import AgentRun, ChangeProposal, ProgressEvent, SelectionScope, SourceRegion, now
from .sources import SourceError

log = logging.getLogger(__name__)


class ProviderUnavailable(Exception):
    pass


MAX_HISTORY_TURNS = 12
MAX_TURN_CHARS = 2000


def run_reply_text(run: AgentRun) -> str:
    """What the assistant said in a finished run, as one conversation turn."""
    if run.status != "succeeded":
        return f"(The run {run.status}{': ' + run.error if run.error else ''}.)"
    parts = [run.outcome.get("explanation") or ""]
    if questions := run.outcome.get("questions"):
        parts.append("Questions for you:\n" + "\n".join(f"- {q}" for q in questions))
    if run.outcome.get("proposal_id"):
        parts.append(f"(Proposed change {run.outcome['proposal_id']}.)")
    return "\n\n".join(p for p in parts if p).strip()


class RunManager:
    def __init__(self, settings: Settings, guidance: SkillGuidance, bus: EventBus):
        self.settings = settings
        self.guidance = guidance
        self.bus = bus
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent")
        self.tokens: dict[str, CancelToken] = {}
        self._lock = threading.Lock()

    # --------------------------------------------------------------- storage

    def save(self, project: Project, run: AgentRun) -> None:
        project.store.put_body("agent_runs", run.id, run.model_dump(mode="json"),
                               created_at=run.created_at, status=run.status)
        self.bus.publish(project.id, {"type": "run", "run": run.model_dump(mode="json")})

    def get(self, project: Project, run_id: str) -> AgentRun:
        body = project.store.get_body("agent_runs", run_id)
        if body is None:
            raise KeyError(f"no run {run_id}")
        return AgentRun.model_validate(body)

    def list(self, project: Project, limit: int = 30) -> list[AgentRun]:
        return [AgentRun.model_validate(b) for b in
                project.store.list_bodies("agent_runs", order="created_at DESC")[:limit]]

    def usage(self, project: Project) -> dict[str, dict[str, int]]:
        """Recorded provider usage across all runs, including those outside chat history."""
        return {body["id"]: {key: body.get("outcome", {}).get(key, 0)
                             for key in ("input_tokens", "output_tokens")}
                for body in project.store.list_bodies("agent_runs")}

    def tracked_client(self, cfg, project: Project, run: AgentRun):
        """Persist provider-reported usage after each call, even if later work fails."""
        manager = self
        client = make_client(cfg)
        usage_lock = threading.Lock()

        class TrackedClient:
            def __getattr__(self, name):
                return getattr(client, name)

            def complete_json(self, *args, **kwargs):
                try:
                    result = client.complete_json(*args, **kwargs)
                except LLMError as exc:
                    record(exc)
                    raise
                record(result)
                return result

        def record(result):
            with usage_lock:
                for key in ("input_tokens", "output_tokens"):
                    run.outcome[key] = run.outcome.get(key, 0) + getattr(result, key, 0)
                manager.save(project, run)

        return TrackedClient()

    def history(self, project: Project, parent_run_id: str | None) -> list[dict[str, str]]:
        """The conversation up to and including the parent run's exchange, newest turns kept."""
        if not parent_run_id:
            return []
        parent = self.get(project, parent_run_id)
        if parent.status in ("queued", "running"):
            raise ValueError(f"run {parent_run_id} has not finished")
        turns = [*parent.conversation, {"role": "user", "text": parent.instruction},
                 {"role": "assistant", "text": run_reply_text(parent)}]
        return [{"role": t["role"], "text": t["text"][:MAX_TURN_CHARS]} for t in turns if t["text"]][-MAX_HISTORY_TURNS:]

    def continued_selection(self, project: Project, parent_run_id: str | None,
                            selection: SelectionScope) -> SelectionScope:
        """A follow-up with nothing selected keeps talking about what the earlier run was about."""
        empty = not (selection.entity_ids or selection.relationship_ids or selection.field_ids
                     or selection.source_regions)
        if not parent_run_id or not empty:
            return selection
        inherited = self.get(project, parent_run_id).selection or SelectionScope()
        live = project.view(project.head()).rows()
        return inherited.model_copy(update={
            "entity_ids": [i for i in inherited.entity_ids if i in live],
            "relationship_ids": [i for i in inherited.relationship_ids if i in live],
        })

    def recover(self, project: Project) -> None:
        """Runs that were in flight when the process stopped cannot resume; mark them."""
        for b in project.store.list_bodies("agent_runs", "status IN ('queued','running')"):
            run = AgentRun.model_validate(b)
            if run.id not in self.tokens:
                run.status = "failed"
                run.error = "Interrupted: the workbench stopped while this was running. Start it again."
                run.finished_at = now()
                self.save(project, run)

    # ------------------------------------------------------------------ runs

    def start_correction(self, project: Project, base: str, selection: SelectionScope,
                         instruction: str, provider: str | None = None,
                         parent_run_id: str | None = None) -> AgentRun:
        if base != project.head():
            raise StaleRevision(base, project.head())
        history = self.history(project, parent_run_id)
        selection = self.continued_selection(project, parent_run_id, selection)
        cfg = self.settings.provider(provider)
        if cfg.kind == "openai":  # fail fast instead of starting a run that cannot reach its model
            health = make_client(cfg).health()  # type: ignore[attr-defined]
            if not health["ok"]:
                raise ProviderUnavailable(health["detail"])
        run = AgentRun(
            id=f"run-{secrets.token_hex(4)}", kind="correction", input_revision=base,
            selection=selection, instruction=instruction, parent_run_id=parent_run_id,
            conversation=history, provider=cfg.name, model=cfg.model,
            skill_version=self.guidance.version,
        )
        token = CancelToken()
        with self._lock:
            self.tokens[run.id] = token
        self.save(project, run)
        self.pool.submit(self._execute, project, run, token,
                         lambda progress: run_with_evidence(
                             project, self.tracked_client(cfg, project, run), self.guidance, base, selection,
                             instruction, run.id, progress, token, history=history, agent=self.settings.agent))
        return run

    def start_autofix(self, project: Project, base: str, issue_ids: list[str],
                      provider: str | None = None) -> AgentRun:
        """Fix issues group by group (autofix.py); obvious fixes are applied as automatic revisions."""
        from .autofix import run_autofix

        if base != project.head():
            raise StaleRevision(base, project.head())
        cfg = self.settings.provider(provider)
        if cfg.kind == "openai":
            health = make_client(cfg).health()  # type: ignore[attr-defined]
            if not health["ok"]:
                raise ProviderUnavailable(health["detail"])
        run = AgentRun(
            id=f"run-{secrets.token_hex(4)}", kind="autofix", input_revision=base,
            instruction=f"Auto-fix {len(issue_ids) or 'all open'} issue(s)", provider=cfg.name, model=cfg.model,
            skill_version=self.guidance.version,
        )
        token = CancelToken()
        with self._lock:
            self.tokens[run.id] = token
        self.save(project, run)
        self.pool.submit(self._execute, project, run, token,
                         lambda progress: run_autofix(project, self.tracked_client(cfg, project, run), self.guidance, issue_ids or None,
                                                      run.id, progress, token, agent=self.settings.agent))
        return run

    def start_build(self, project: Project, base: str, source_ids: list[str], instruction: str,
                    provider: str | None = None, source_pages: dict[str, list[int]] | None = None,
                    source_roles: dict[str, str] | None = None) -> AgentRun:
        """Build from confirmed CSV records or selected image/document evidence."""
        from .agent.build import run_build

        if base != project.head():
            raise StaleRevision(base, project.head())
        sources = [project.source(sid) for sid in source_ids]
        documents = any(source.kind != "csv" for source in sources) or "reference" in (source_roles or {}).values()
        if not sources or len(source_ids) != len(set(source_ids)):
            raise SourceError("Select distinct sources, including at least one build input.")
        if set(source_roles or {}) - set(source_ids):
            raise SourceError("Source roles must refer to selected sources.")
        roles = {sid: (source_roles or {}).get(sid, "input") for sid in source_ids}
        if any(role not in ("input", "reference") for role in roles.values()):
            raise SourceError("Choose build input or supporting evidence for each source.")
        if "input" not in roles.values():
            raise SourceError("Select at least one build input; supporting evidence alone cannot start a build.")
        for source in sources:
            if source.kind == "csv" and source.status != "configured":
                raise SourceError("Confirm each CSV structure before using its records.")
        if source_pages and (set(source_pages) - set(source_ids) or any(
            source.kind != "pdf" and source.id in source_pages for source in sources
        )):
            raise SourceError("Page selections must refer to PDF sources in this build.")
        selection = SelectionScope(source_regions=[SourceRegion(
            source_id=sid, pages=(source_pages or {}).get(sid), role=roles[sid]) for sid in source_ids])
        if selection:
            from .documents import validate_regions
            validate_regions(project, selection.source_regions, batched=True)
        cfg = self.settings.provider(provider)
        if documents and any(source.kind == "image" for source in sources) and not (
            cfg.supports_images or cfg.kind == "anthropic"
        ):
            raise SourceError("Choose a vision-capable model in the assistant panel to read images.")
        if cfg.kind == "openai":
            health = make_client(cfg).health()  # type: ignore[attr-defined]
            if not health["ok"]:
                raise ProviderUnavailable(health["detail"])
        run = AgentRun(
            id=f"run-{secrets.token_hex(4)}", kind="build", mode="build", input_revision=base, source_ids=source_ids,
            selection=selection, instruction=instruction, provider=cfg.name, model=cfg.model, skill_version=self.guidance.version,
        )
        token = CancelToken()
        with self._lock:
            self.tokens[run.id] = token
        self.save(project, run)
        if documents:
            self.pool.submit(self._execute, project, run, token,
                             lambda progress: run_source_build(
                                 project, self.tracked_client(cfg, project, run), self.guidance, base,
                                 selection.source_regions, instruction, run.id, progress, token, self.settings.agent))
        else:
            self.pool.submit(self._execute, project, run, token,
                             lambda progress: run_build(project, self.tracked_client(cfg, project, run), self.guidance, base, source_ids,
                                                        instruction, run.id, progress, token))
        return run

    def start_revision(self, project: Project, proposal: ChangeProposal, instruction: str,
                       provider: str | None = None, reconsider: bool = False,
                       parent_run_id: str | None = None,
                       source_regions: list[SourceRegion] | None = None) -> AgentRun:
        """Continue a pending proposal or reconsider it against the latest revision."""
        if proposal.status != "pending" and not (reconsider and proposal.status == "stale"):
            raise ValueError(f"proposal is {proposal.status}")
        history = self.history(project, parent_run_id or proposal.agent_run_id) if not reconsider else []
        base = project.head()
        if not reconsider and proposal.base_revision != base:
            raise StaleRevision(proposal.base_revision, project.head())
        inherited_regions = [] if (proposal.build_summary or {}).get("source_notes") else proposal.selection.source_regions
        selection = proposal.selection.model_copy(update={"source_regions":
                                                          inherited_regions if source_regions is None else source_regions})
        if reconsider and proposal.base_revision != base:
            live = project.view(base).rows()
            selection = selection.model_copy(update={
                "entity_ids": [i for i in selection.entity_ids if i in live],
                "relationship_ids": [i for i in selection.relationship_ids if i in live],
            })
        cfg = self.settings.provider(provider)
        if cfg.kind == "openai":
            health = make_client(cfg).health()  # type: ignore[attr-defined]
            if not health["ok"]:
                raise ProviderUnavailable(health["detail"])
        run = AgentRun(
            id=f"run-{secrets.token_hex(4)}", kind="correction", input_revision=base,
            mode="reconsider" if reconsider else "reply",
            parent_run_id=None if reconsider else (parent_run_id or proposal.agent_run_id),
            selection=selection, instruction=instruction, conversation=history,
            provider=cfg.name, model=cfg.model, skill_version=self.guidance.version,
        )
        token = CancelToken()
        with self._lock:
            self.tokens[run.id] = token
        self.save(project, run)
        self.pool.submit(self._execute, project, run, token,
                         lambda progress: run_with_evidence(
                             project, self.tracked_client(cfg, project, run), self.guidance, base,
                             selection, instruction, run.id, progress, token,
                             prior_proposal=proposal, reconsider=reconsider, history=history,
                             agent=self.settings.agent))
        return run

    def cancel(self, project: Project, run_id: str) -> AgentRun:
        with self._lock:
            token = self.tokens.get(run_id)
        if token is not None:
            token.cancel()
        run = self.get(project, run_id)
        if run.status == "queued":
            run.status = "cancelled"
            run.finished_at = now()
            self.save(project, run)
        return run

    def _execute(self, project: Project, run: AgentRun, token: CancelToken,
                 work: Callable[[Callable[[str, str, dict], None]], object]) -> None:
        def progress(stage: str, message: str, data: dict) -> None:
            run.progress.append(ProgressEvent(stage=stage, message=message, data=data))
            self.save(project, run)

        try:
            token.check()
            run.status = "running"
            self.save(project, run)
            outcome = work(progress)
            proposal = getattr(outcome, "proposal", None)
            run.outcome = {
                "proposal_id": proposal.id if proposal else None,
                "dismissed_proposal_id": getattr(outcome, "dismissed_proposal_id", None),
                "questions": readable_questions(getattr(outcome, "questions", [])) + (
                    readable_questions(getattr(outcome, "choices", []))
                    if not proposal and not hasattr(outcome, "autofix") else []),
                "explanation": getattr(outcome, "explanation", ""),
                "steps": getattr(outcome, "steps", 0),
                "input_tokens": run.outcome.get("input_tokens", getattr(outcome, "input_tokens", 0)),
                "output_tokens": run.outcome.get("output_tokens", getattr(outcome, "output_tokens", 0)),
                **({"autofix": outcome.autofix} if hasattr(outcome, "autofix") else {}),
            }
            run.status = "succeeded"
        except Cancelled:
            run.status = "cancelled"
            run.error = "Cancelled"
        except (LLMError, SourceError) as exc:
            run.status = "failed"
            run.error = str(exc)
        except StaleRevision as exc:
            run.status = "failed"
            run.error = f"The model changed while this was running ({exc})."
        except Exception as exc:  # noqa: BLE001 - reported to the user, logged in full
            log.error("run %s failed:\n%s", run.id, traceback.format_exc())
            run.status = "failed"
            run.error = f"Unexpected error: {type(exc).__name__}: {exc}"
        finally:
            run.finished_at = now()
            with self._lock:
                self.tokens.pop(run.id, None)
            self.save(project, run)
