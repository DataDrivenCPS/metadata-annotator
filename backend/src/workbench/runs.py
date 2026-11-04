"""Background agent runs with persisted progress and cooperative cancellation."""

from __future__ import annotations

import logging
import secrets
from dataclasses import replace
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .agent.correction import run_correction
from .agent.multisource import run_source_build, run_with_evidence
from .agent.guidance import SkillGuidance
from .choices import chat_choices
from .config import Settings
from .events import EventBus
from .llm import Cancelled, CancelToken, LLMError, make_client
from .llm.endpoint import EndpointInfo, resolved_model
from .llm.litellm_client import estimate_tokens
from .project import Project, StaleRevision
from .questions import readable_questions
from .schemas import AgentRun, ChangeProposal, ProgressEvent, SelectionScope, SourceRegion, now
from .sources import SourceError

log = logging.getLogger(__name__)


class ProviderUnavailable(Exception):
    pass


MAX_HISTORY_TURNS = 12
MAX_TURN_CHARS = 2000
HEALTH_TTL_S = 60.0
MAX_RECORDED_CALLS = 200  # per-call usage kept on a run's outcome


def run_reply_text(run: AgentRun) -> str:
    """What the assistant said in a finished run, as one conversation turn."""
    if run.status != "succeeded":
        return f"(The run {run.status}{': ' + run.error if run.error else ''}.)"
    parts = [run.outcome.get("explanation") or ""]
    if questions := run.outcome.get("questions"):
        parts.append("Questions for you:\n" + "\n".join(f"- {q}" for q in questions))
    for choice in run.outcome.get("choices") or []:  # offered as options; the person may pick one or answer
        parts.append("Choose one:\n" + "\n".join(
            [f"- {choice.get('question', '')}", *(f"  • {o.get('label', '')}" for o in choice.get("options", []))]))
    if run.outcome.get("proposal_id"):
        parts.append(f"(Proposed change {run.outcome['proposal_id']}.)")
    return "\n\n".join(p for p in parts if p).strip()


def attachment_note(project: Project, run: AgentRun) -> str:
    """What the person attached to a message, so later turns know it was read then (not now)."""
    names = []
    for region in (run.selection.source_regions if run.selection else []):
        try:
            source = project.source(region.source_id)
        except KeyError:
            continue
        pages = region.pages
        if pages and source.kind == "pdf" and len(pages) < (source.page_count or 0):
            span = f"page {pages[0]}" if len(pages) == 1 else f"pages {min(pages)}–{max(pages)}" if (
                max(pages) - min(pages) + 1 == len(pages)) else f"{len(pages)} pages"
            names.append(f"{source.filename}, {span}")
        else:
            names.append(source.filename)
    if not names or run.mode == "build":
        return ""
    return f"\n(Attached to this message and read then: {'; '.join(dict.fromkeys(names))}.)"


class RunManager:
    def __init__(self, settings: Settings, guidance: SkillGuidance, bus: EventBus):
        self.settings = settings
        self.guidance = guidance
        self.bus = bus
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent")
        self.tokens: dict[str, CancelToken] = {}
        self._lock = threading.Lock()
        self._health: dict[str, tuple[float, dict]] = {}  # provider name -> (checked at, last result)

    # ------------------------------------------------------------- providers

    def provider_health(self, cfg, fresh: bool = False) -> dict:
        """The endpoint's health; a recent good result is reused so starting a run is not a round trip."""
        key = f"{cfg.name}/{cfg.model}"  # a server may offer several models; each is checked on its own
        with self._lock:
            hit = self._health.get(key)
        if not fresh and hit and hit[1].get("ok") and time.monotonic() - hit[0] < HEALTH_TTL_S:
            return hit[1]
        health = make_client(cfg).health()  # type: ignore[attr-defined]
        with self._lock:
            self._health[key] = (time.monotonic(), health)
        return health

    def ensure_reachable(self, cfg) -> None:
        """Fail fast instead of starting a run that cannot reach its model."""
        self.choose(cfg)

    def choose(self, cfg):
        """``cfg`` with the model that will actually answer, checking the endpoint is reachable.

        A server that lists its own models (``model`` unset, or a single loaded model whatever
        name is asked for) reports which one runs, so the run records what produced it.
        """
        if cfg.kind != "openai":
            return replace(cfg, supports_images=EndpointInfo(cfg).supports_images())  # known without asking
        health = self.provider_health(cfg)
        if not health["ok"]:
            raise ProviderUnavailable(health["detail"])
        cfg = resolved_model(cfg, health.get("models") or [])
        if cfg.supports_images is None:  # what the server says about the model that will answer
            cfg = replace(cfg, supports_images=bool(health.get("supports_images")))
        if cfg.context_tokens is None and health.get("context_tokens"):  # recorded with the run
            cfg = replace(cfg, context_tokens=health["context_tokens"])
        return cfg

    def _forget_health(self, provider: str) -> None:
        """Drop the provider's cached checks, for every model of it."""
        with self._lock:
            for key in [k for k in self._health if k == provider or k.startswith(f"{provider}/")]:
                self._health.pop(key)

    def supersede(self, project: Project, proposal_id: str | None) -> None:
        """A new request replaces the proposal the person had open: it is closed, not left pending."""
        if not proposal_id:
            return
        try:
            project.dismiss_proposal(proposal_id)
        except KeyError:
            pass

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
                at = run.progress[-1].message[:120] if run.progress else ""  # what the call was for
                try:
                    result = client.complete_json(*args, **kwargs)
                except LLMError as exc:
                    record(exc, at, error=str(exc)[:120])
                    raise
                except Cancelled:
                    # Usage arrives at the end of a reply, so a cancelled call reports none; the
                    # provider still bills the prompt. Its estimate is noted, not counted.
                    system = args[0] if args else kwargs.get("system", "")
                    messages = args[1] if len(args) > 1 else kwargs.get("messages", [])
                    record(None, at, cancelled=estimate_tokens(system, messages, kwargs.get("images")))
                    raise
                record(result, at)
                return result

        def record(result, at: str, *, error: str | None = None, cancelled: int | None = None):
            call: dict = {"at": at, "input_tokens": getattr(result, "input_tokens", 0),
                          "output_tokens": getattr(result, "output_tokens", 0)}
            if getattr(result, "cached_tokens", 0):
                call["cached_tokens"] = result.cached_tokens
            if getattr(result, "estimated", False):
                call["estimated"] = True
            if error:
                call["error"] = error
            if cancelled is not None:
                call["cancelled"] = True
                call["unreported_input_tokens"] = cancelled
            with usage_lock:
                for key in ("input_tokens", "output_tokens"):
                    run.outcome[key] = run.outcome.get(key, 0) + call[key]
                if call.get("cached_tokens"):
                    run.outcome["cached_tokens"] = run.outcome.get("cached_tokens", 0) + call["cached_tokens"]
                if call.get("estimated"):
                    run.outcome["estimated"] = True
                calls = run.outcome.setdefault("calls", [])
                if len(calls) < MAX_RECORDED_CALLS:
                    calls.append(call)
                manager.save(project, run)

        return TrackedClient()

    def history(self, project: Project, parent_run_id: str | None) -> list[dict[str, str]]:
        """The conversation up to and including the parent run's exchange, newest turns kept."""
        if not parent_run_id:
            return []
        parent = self.get(project, parent_run_id)
        if parent.status in ("queued", "running"):
            raise ValueError(f"run {parent_run_id} has not finished")
        note = attachment_note(project, parent)
        turns = [*parent.conversation, {"role": "user", "text": parent.instruction[:MAX_TURN_CHARS - len(note)] + note},
                 {"role": "assistant", "text": run_reply_text(parent)}]
        return [{"role": t["role"], "text": t["text"][:MAX_TURN_CHARS]} for t in turns if t["text"]][-MAX_HISTORY_TURNS:]

    def continued_selection(self, project: Project, parent_run_id: str | None,
                            selection: SelectionScope) -> SelectionScope:
        """A follow-up with nothing selected keeps talking about what the earlier run was about."""
        empty = not (selection.entity_ids or selection.relationship_ids or selection.field_ids)
        if not parent_run_id or not empty:
            return selection
        inherited = self.get(project, parent_run_id).selection or SelectionScope()
        live = project.view(project.head()).rows()
        return inherited.model_copy(update={
            "entity_ids": [i for i in inherited.entity_ids if i in live],
            "relationship_ids": [i for i in inherited.relationship_ids if i in live],
            # Attached sources belong to the message they came with; a follow-up reads only its own.
            "source_regions": selection.source_regions,
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
                         parent_run_id: str | None = None, inherit_selection: bool = True,
                         supersedes_proposal_id: str | None = None) -> AgentRun:
        if base != project.head():
            raise StaleRevision(base, project.head())
        history = self.history(project, parent_run_id)
        if inherit_selection:
            selection = self.continued_selection(project, parent_run_id, selection)
        cfg = self.choose(self.settings.provider(provider))
        run = AgentRun(
            id=f"run-{secrets.token_hex(4)}", kind="correction", input_revision=base,
            selection=selection, instruction=instruction, parent_run_id=parent_run_id,
            conversation=history, provider=cfg.name, model=cfg.model, model_settings=cfg.run_settings(),
            skill_version=self.guidance.version,
        )
        token = CancelToken()
        with self._lock:
            self.tokens[run.id] = token
        self.supersede(project, supersedes_proposal_id)
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
        cfg = self.choose(self.settings.provider(provider))
        run = AgentRun(
            id=f"run-{secrets.token_hex(4)}", kind="autofix", input_revision=base,
            instruction=f"Auto-fix {len(issue_ids) or 'all open'} issue(s)", provider=cfg.name, model=cfg.model, model_settings=cfg.run_settings(),
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
                    source_roles: dict[str, str] | None = None,
                    supersedes_proposal_id: str | None = None) -> AgentRun:
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
        cfg = self.choose(self.settings.provider(provider))
        if documents and any(source.kind == "image" for source in sources) and not cfg.supports_images:
            raise SourceError(f"{cfg.model or cfg.name} does not read images; choose a vision-capable model "
                              "in the assistant panel.")
        run = AgentRun(
            id=f"run-{secrets.token_hex(4)}", kind="build", mode="build", input_revision=base, source_ids=source_ids,
            selection=selection, instruction=instruction, provider=cfg.name, model=cfg.model, model_settings=cfg.run_settings(), skill_version=self.guidance.version,
        )
        token = CancelToken()
        with self._lock:
            self.tokens[run.id] = token
        self.supersede(project, supersedes_proposal_id)
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
        cfg = self.choose(self.settings.provider(provider))
        run = AgentRun(
            id=f"run-{secrets.token_hex(4)}", kind="correction", input_revision=base,
            mode="reconsider" if reconsider else "reply",
            parent_run_id=None if reconsider else (parent_run_id or proposal.agent_run_id),
            selection=selection, instruction=instruction, conversation=history,
            provider=cfg.name, model=cfg.model, model_settings=cfg.run_settings(), skill_version=self.guidance.version,
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
            offered: list[dict] = []
            asked: list = []
            if not proposal and not hasattr(outcome, "autofix") and (choices := getattr(outcome, "choices", None)):
                # Each option becomes a checked pending proposal the person can pick instead of retyping it.
                if any(o.get("operations") for c in choices for o in c.get("options", [])):
                    progress("choices", "Checking the options", {})
                offered, asked = chat_choices(project, run.input_revision, choices, run.selection or SelectionScope(),
                                              run.instruction, run.id)
            run.outcome = {
                "proposal_id": proposal.id if proposal else None,
                "dismissed_proposal_id": getattr(outcome, "dismissed_proposal_id", None),
                "questions": readable_questions(getattr(outcome, "questions", [])) + readable_questions(asked),
                **({"choices": offered} if offered else {}),
                "explanation": getattr(outcome, "explanation", ""),
                "steps": getattr(outcome, "steps", 0),
                "input_tokens": run.outcome.get("input_tokens", getattr(outcome, "input_tokens", 0)),
                "output_tokens": run.outcome.get("output_tokens", getattr(outcome, "output_tokens", 0)),
                **{key: run.outcome[key] for key in ("cached_tokens", "estimated", "calls") if key in run.outcome},
                **({"autofix": outcome.autofix} if hasattr(outcome, "autofix") else {}),
            }
            run.status = "succeeded"
        except Cancelled:
            run.status = "cancelled"
            run.error = "Cancelled"
        except (LLMError, SourceError) as exc:
            run.status = "failed"
            run.error = str(exc)
            if isinstance(exc, LLMError):
                self._forget_health(run.provider)  # check the endpoint again before the next run
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
