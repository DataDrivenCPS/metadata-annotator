"""Background agent runs with persisted progress and cooperative cancellation."""

from __future__ import annotations

import logging
import secrets
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .agent.correction import run_correction
from .agent.guidance import SkillGuidance
from .config import Settings
from .events import EventBus
from .llm import Cancelled, CancelToken, LLMError, make_client
from .project import Project, StaleRevision
from .schemas import AgentRun, ChangeProposal, ProgressEvent, SelectionScope, now

log = logging.getLogger(__name__)


class ProviderUnavailable(Exception):
    pass


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
                         instruction: str, provider: str | None = None) -> AgentRun:
        if base != project.head():
            raise StaleRevision(base, project.head())
        cfg = self.settings.provider(provider)
        if cfg.kind == "openai":  # fail fast instead of starting a run that cannot reach its model
            health = make_client(cfg).health()  # type: ignore[attr-defined]
            if not health["ok"]:
                raise ProviderUnavailable(health["detail"])
        run = AgentRun(
            id=f"run-{secrets.token_hex(4)}", kind="correction", input_revision=base,
            selection=selection, instruction=instruction, provider=cfg.name, model=cfg.model,
            skill_version=self.guidance.version,
        )
        token = CancelToken()
        with self._lock:
            self.tokens[run.id] = token
        self.save(project, run)
        self.pool.submit(self._execute, project, run, token,
                         lambda progress: run_correction(
                             project, make_client(cfg), self.guidance, base, selection,
                             instruction, run.id, progress, token))
        return run

    def start_build(self, project: Project, base: str, source_ids: list[str], instruction: str,
                    provider: str | None = None) -> AgentRun:
        """Build model entities from confirmed source records (agent/build.py)."""
        from .agent.build import run_build

        if base != project.head():
            raise StaleRevision(base, project.head())
        cfg = self.settings.provider(provider)
        if cfg.kind == "openai":
            health = make_client(cfg).health()  # type: ignore[attr-defined]
            if not health["ok"]:
                raise ProviderUnavailable(health["detail"])
        run = AgentRun(
            id=f"run-{secrets.token_hex(4)}", kind="build", input_revision=base, source_ids=source_ids,
            instruction=instruction, provider=cfg.name, model=cfg.model, skill_version=self.guidance.version,
        )
        token = CancelToken()
        with self._lock:
            self.tokens[run.id] = token
        self.save(project, run)
        self.pool.submit(self._execute, project, run, token,
                         lambda progress: run_build(project, make_client(cfg), self.guidance, base, source_ids,
                                                    instruction, run.id, progress, token))
        return run

    def start_revision(self, project: Project, proposal: ChangeProposal, instruction: str,
                       provider: str | None = None) -> AgentRun:
        """Revise a pending proposal with the person's reply before applying anything."""
        if proposal.status != "pending":
            raise ValueError(f"proposal is {proposal.status}")
        if proposal.base_revision != project.head():
            raise StaleRevision(proposal.base_revision, project.head())
        cfg = self.settings.provider(provider)
        if cfg.kind == "openai":
            health = make_client(cfg).health()  # type: ignore[attr-defined]
            if not health["ok"]:
                raise ProviderUnavailable(health["detail"])
        run = AgentRun(
            id=f"run-{secrets.token_hex(4)}", kind="correction", input_revision=proposal.base_revision,
            selection=proposal.selection, instruction=instruction, provider=cfg.name, model=cfg.model,
            skill_version=self.guidance.version,
        )
        token = CancelToken()
        with self._lock:
            self.tokens[run.id] = token
        self.save(project, run)
        self.pool.submit(self._execute, project, run, token,
                         lambda progress: run_correction(
                             project, make_client(cfg), self.guidance, proposal.base_revision,
                             proposal.selection, instruction, run.id, progress, token,
                             prior_proposal=proposal))
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
                "questions": getattr(outcome, "questions", []),
                "explanation": getattr(outcome, "explanation", ""),
                "steps": getattr(outcome, "steps", 0),
                "input_tokens": getattr(outcome, "input_tokens", 0),
                "output_tokens": getattr(outcome, "output_tokens", 0),
            }
            run.status = "succeeded"
        except Cancelled:
            run.status = "cancelled"
            run.error = "Cancelled"
        except LLMError as exc:
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
