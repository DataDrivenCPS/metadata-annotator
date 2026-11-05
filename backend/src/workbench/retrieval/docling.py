"""Docling, as a local docling-serve process the workbench installs and runs itself.

Docling brings PyTorch and layout/table/OCR models (about 2 GB installed, plus ~500 MB of
models on first start), so it lives in its own environment under the data directory instead of
the workbench's: ``<data>/retrieval/docling-serve/v<version>/``, created with uv (which runs the
workbench, and tells its children where it is in ``UV``). ``--torch-backend auto`` picks a CPU
PyTorch on machines without a GPU instead of multi-gigabyte CUDA builds.

The server listens on 127.0.0.1 only, on a free port, with a random API key, and stops after
being idle for a while (its models hold ~2 GB of RAM). ``server.json`` records the running
server; the API key proves it is the one recorded there. The workbench that owns the server
touches ``server.json`` as a heartbeat. Another workbench on the same data directory that finds a
fresh heartbeat only shares the server: it records its use in ``server.used`` and never stops
it. One that finds a stale heartbeat (the owner restarted or crashed) takes ownership. An owner
that shuts down leaves the server running when another workbench used it recently.

With ``retrieval.docling_url`` set, an existing docling-serve is used and nothing is installed.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import RetrievalConfig
from ..llm import CancelToken

log = logging.getLogger(__name__)

PYTHON = "3.12"
BOOT_TIMEOUT_S = 30 * 60  # the first start downloads the layout, table and OCR models
POLL_WAIT_S = 5
HEARTBEAT_S = 15
STALE_S = 60  # an owner that has not touched server.json for this long is gone


class DoclingUnavailable(Exception):
    """Docling cannot be installed, started or reached. ``str()`` is user-facing."""


class DoclingFailed(Exception):
    """Docling ran but could not convert this document. ``str()`` is user-facing."""


Progress = Callable[[str], None]


class DoclingService:
    def __init__(self, cfg: RetrievalConfig, root: Path):
        self.cfg = cfg
        self.root = root
        self.external = cfg.docling_url.rstrip("/") if cfg.docling_url else None
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._adopted_pid: int | None = None
        self._owner = False  # False while sharing another workbench's server
        self._last_used_wall = time.time()
        self._url: str | None = None
        self._key: str | None = None
        self._busy = 0
        self._last_used = time.monotonic()
        self._stopping = threading.Event()
        self._watcher: threading.Thread | None = None
        self._failure: str | None = None  # an install failure is not retried until restart

    # -------------------------------------------------------------- paths

    @property
    def env_dir(self) -> Path:
        return self.root / f"v{self.cfg.docling_version}"

    @property
    def state_file(self) -> Path:
        return self.root / "server.json"

    @property
    def used_file(self) -> Path:
        return self.root / "server.used"

    def _heartbeat(self) -> None:
        try:
            os.utime(self.state_file)
        except OSError:
            pass

    def _record_use(self) -> None:
        self._last_used_wall = time.time()
        try:
            self.used_file.write_text(json.dumps({"pid": os.getpid(), "at": self._last_used_wall}), encoding="utf-8")
        except OSError:
            pass

    def _others_used_since(self, since: float) -> bool:
        """Another workbench used the server after ``since`` (wall clock)."""
        try:
            used = json.loads(self.used_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return used.get("pid") != os.getpid() and float(used.get("at", 0)) > since

    def _last_use_anywhere(self) -> float:
        try:
            used = json.loads(self.used_file.read_text(encoding="utf-8"))
            return max(self._last_used_wall, float(used.get("at", 0)))
        except (OSError, ValueError):
            return self._last_used_wall

    def _executable(self) -> Path:
        bin_dir = self.env_dir / ("Scripts" if os.name == "nt" else "bin")
        return bin_dir / ("docling-serve.exe" if os.name == "nt" else "docling-serve")

    def _python(self) -> Path:
        return self.env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def installed(self) -> bool:
        return (self.env_dir / "workbench-installed.json").exists() and self._executable().exists()

    # ------------------------------------------------------------ install

    def _install(self, progress: Progress, cancel: CancelToken) -> None:
        if platform.system() == "Darwin" and platform.machine() == "x86_64":
            raise DoclingUnavailable("Docling has no PyTorch builds for Intel Macs. Run docling-serve on another "
                                     "machine and set retrieval.docling_url, or use parser = \"basic\".")
        uv = os.environ.get("UV") or shutil.which("uv")
        if not uv:
            raise DoclingUnavailable("uv was not found, so the document parser cannot be installed. "
                                     "Start the workbench with uv, or set retrieval.docling_url.")
        self.root.mkdir(parents=True, exist_ok=True)
        log_path = self.root / "install.log"
        progress(f"Installing the document parser (docling-serve {self.cfg.docling_version}; "
                 "first use only, about 2 GB)")
        env = _clean_env()
        steps = [
            [uv, "venv", "--clear", "--python", PYTHON, str(self.env_dir)],
            [uv, "pip", "install", "--python", str(self._python()), "--torch-backend", "auto",
             f"docling-serve=={self.cfg.docling_version}"],
        ]
        with log_path.open("a", encoding="utf-8") as out:
            for cmd in steps:
                out.write(f"$ {' '.join(cmd)}\n")
                out.flush()
                proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        env=env, cwd=self.root)
                while proc.poll() is None:
                    if cancel.cancelled or self._stopping.is_set():
                        proc.terminate()
                        proc.wait(30)
                        cancel.check()
                        raise DoclingUnavailable("The workbench is shutting down.")
                    time.sleep(0.5)
                if proc.returncode != 0:
                    raise DoclingUnavailable(f"Installing the document parser failed; see {log_path}. "
                                             + _tail(log_path))
        (self.env_dir / "workbench-installed.json").write_text(
            json.dumps({"docling_serve": self.cfg.docling_version, "python": sys.version.split()[0]}), encoding="utf-8")

    # -------------------------------------------------------------- start

    def _alive(self, url: str, key: str | None) -> bool:
        """The server at ``url`` answers and accepts ``key`` (so it is the one we started)."""
        try:
            with httpx.Client(timeout=3) as client:
                if client.get(f"{url}/health").status_code != 200:
                    return False
                probe = client.get(f"{url}/v1/status/poll/workbench-probe", headers=_headers(key))
                return probe.status_code not in (401, 403)
        except httpx.HTTPError:
            return False

    def _adopt(self) -> bool:
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        url = f"http://127.0.0.1:{state.get('port')}"
        if state.get("version") != self.cfg.docling_version or not self._alive(url, state.get("key")):
            return False
        try:
            owner_alive = time.time() - self.state_file.stat().st_mtime < STALE_S
        except OSError:
            owner_alive = False
        self._url, self._key, self._adopted_pid = url, state["key"], state.get("pid")
        self._owner = not owner_alive
        if self._owner:
            self._heartbeat()
        log.info("%s docling-serve at %s (pid %s)", "taking over" if self._owner else "sharing", url, self._adopted_pid)
        return True

    def _start(self, progress: Progress, cancel: CancelToken) -> None:
        port = _free_port()
        key = secrets.token_urlsafe(24)
        env = _clean_env() | {
            "HF_HOME": str(self.root / "models"),
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DOCLING_SERVE_API_KEY": key,
            "DOCLING_SERVE_ENABLE_UI": "false",
            "DOCLING_SERVE_ENABLE_REMOTE_SERVICES": "false",
            "DOCLING_SERVE_SCRATCH_PATH": str(self.root / "scratch"),
        }
        log_path = self.root / "server.log"
        progress("Starting the document parser" + ("" if (self.root / "models").exists()
                                                    else " (first start downloads its models, about 500 MB)"))
        out = log_path.open("a", encoding="utf-8")
        try:
            proc = subprocess.Popen([str(self._executable()), "run", "--host", "127.0.0.1", "--port", str(port)],
                                    stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                    cwd=self.root)
        finally:
            out.close()
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + BOOT_TIMEOUT_S
        while True:
            if proc.poll() is not None:
                raise DoclingUnavailable(f"The document parser stopped while starting; see {log_path}. " + _tail(log_path))
            if self._alive(url, key):
                break
            if cancel.cancelled or self._stopping.is_set() or time.monotonic() > deadline:
                _terminate(proc)
                cancel.check()
                raise DoclingUnavailable("The document parser did not start in time." if time.monotonic() > deadline
                                         else "The workbench is shutting down.")
            time.sleep(1)
        self._proc, self._url, self._key, self._adopted_pid = proc, url, key, None
        self._owner = True
        self.state_file.write_text(json.dumps({"pid": proc.pid, "port": port, "key": key,
                                               "version": self.cfg.docling_version}), encoding="utf-8")
        log.info("docling-serve started at %s (pid %s)", url, proc.pid)

    def ensure(self, progress: Progress, cancel: CancelToken) -> tuple[str, str | None]:
        """The URL and API key of a running docling-serve, installing and starting it if needed."""
        if self.external:
            key = os.environ.get(self.cfg.docling_api_key_env) if self.cfg.docling_api_key_env else None
            if not self._alive(self.external, key):
                raise DoclingUnavailable(f"The document parser at {self.external} is not reachable.")
            return self.external, key
        with self._lock:
            if self._failure:
                raise DoclingUnavailable(self._failure)
            if self._url and self._alive(self._url, self._key):
                return self._url, self._key
            self._url = self._key = None
            if not self._adopt():
                try:
                    if not self.installed():
                        self._install(progress, cancel)
                    self._start(progress, cancel)
                except DoclingUnavailable as exc:
                    if not (cancel.cancelled or self._stopping.is_set()):
                        self._failure = str(exc)
                    raise
            self._watch_idle()
            url, key = self._url, self._key
            if url is None:
                raise DoclingUnavailable("The document parser is not running.")
            return url, key

    # ------------------------------------------------------------ convert

    def convert(self, data: bytes, filename: str, pages: tuple[int, int] | None,
                progress: Progress, cancel: CancelToken) -> dict[str, Any]:
        """The DoclingDocument (JSON) of ``data``, or of pages ``pages`` (inclusive) of a PDF."""
        url, key = self.ensure(progress, cancel)
        with self._lock:
            self._busy += 1
        self._record_use()
        try:
            form: dict[str, Any] = {"to_formats": "json", "image_export_mode": "placeholder",
                                    "include_images": "false", "abort_on_error": "false",
                                    "document_timeout": "1800"}
            if pages:
                form["page_range"] = [str(pages[0]), str(pages[1])]
            with httpx.Client(timeout=120, headers=_headers(key)) as client:
                started = client.post(f"{url}/v1/convert/file/async", data=form,
                                      files={"files": (filename, data)})
                _raise_for(started)
                task = started.json()["task_id"]
                while True:
                    cancel.check()
                    poll = client.get(f"{url}/v1/status/poll/{task}", params={"wait": POLL_WAIT_S})
                    _raise_for(poll)
                    status = poll.json()
                    if status.get("task_status") in ("success", "partial_success"):
                        break
                    if status.get("task_status") in ("failure", "skipped"):
                        raise DoclingFailed(status.get("error_message") or "The document parser could not read this file.")
                result = client.get(f"{url}/v1/result/{task}")
                _raise_for(result)
            body = result.json()
            document = (body.get("document") or {}).get("json_content")
            if not document:
                errors = "; ".join(str(e.get("error_message", e)) for e in body.get("errors") or [])
                raise DoclingFailed(errors or "The document parser returned no content.")
            return document
        except httpx.HTTPError as exc:
            raise DoclingUnavailable(f"Lost the connection to the document parser: {exc}") from None
        finally:
            with self._lock:
                self._busy -= 1
                self._last_used = time.monotonic()
            self._record_use()

    # --------------------------------------------------------------- stop

    def _watch_idle(self) -> None:
        if self._watcher and self._watcher.is_alive():
            return

        def watch() -> None:
            idle_s = self.cfg.docling_idle_minutes * 60
            while not self._stopping.wait(HEARTBEAT_S):
                with self._lock:
                    if not self._url:
                        return
                    if not self._owner:
                        continue  # the owner decides when it stops
                    if not self._busy and time.time() - self._last_use_anywhere() > idle_s:
                        log.info("stopping idle docling-serve")
                        self._stop_locked()
                        return
                    self._heartbeat()

        self._watcher = threading.Thread(target=watch, daemon=True, name="docling-idle")
        self._watcher.start()

    def _stop_locked(self) -> None:
        if not self._owner:  # sharing another workbench's server: just let go of it
            self._proc = self._url = self._key = self._adopted_pid = None
            return
        if self._proc is not None:
            _terminate(self._proc)
        elif self._adopted_pid and self._url and self._alive(self._url, self._key):
            try:
                os.kill(self._adopted_pid, signal.SIGTERM)  # TerminateProcess on Windows
            except OSError:
                pass
        self._proc = self._url = self._key = self._adopted_pid = None
        self._owner = False
        try:
            self.state_file.unlink()
        except OSError:
            pass

    def stop(self) -> None:
        """At shutdown: stop the server we own, unless another workbench used it recently (then
        leave it running; that workbench takes it over when it next needs it)."""
        self._stopping.set()
        with self._lock:
            if not self._url or self.external:
                return
            idle_s = self.cfg.docling_idle_minutes * 60
            if self._owner and self._others_used_since(time.time() - idle_s):
                log.info("leaving docling-serve running for another workbench")
                self._proc = self._url = self._key = self._adopted_pid = None
                self._owner = False
                return
            self._stop_locked()


def _clean_env() -> dict[str, str]:
    """The workbench's environment without what would point a child at the workbench's own Python."""
    env = dict(os.environ)
    for name in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT"):
        env.pop(name, None)
    return env


def _headers(key: str | None) -> dict[str, str]:
    return {"X-Api-Key": key} if key else {}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


def _raise_for(response: httpx.Response) -> None:
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:300]
        raise DoclingFailed(f"The document parser answered {response.status_code}: {detail}")


def _tail(path: Path, lines: int = 3) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return " / ".join(line.strip() for line in text[-lines:])
