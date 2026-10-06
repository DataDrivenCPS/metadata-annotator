"""Startup regressions require a fresh interpreter, before the SDK is imported."""

import subprocess
import sys
import textwrap


def run_script(source):
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(source)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_failed_sdk_import_does_not_break_server_logging():
    run_script('''
        import importlib.abc
        import io
        import logging
        import sys

        log = logging.getLogger("asyncio")
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        log.addHandler(handler)
        user_filter = logging.Filter()
        log.addFilter(user_filter)

        class FailAfterLoggingSetup(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.startswith("litellm.") and any(
                    type(f).__module__ == "litellm._logging" for f in log.filters
                ):
                    sys.meta_path.remove(self)
                    raise ImportError("simulated SDK initialization failure")

        sys.meta_path.insert(0, FailAfterLoggingSetup())
        try:
            import workbench.llm
        except ImportError as exc:
            assert "simulated SDK initialization failure" in str(exc)
        else:
            raise AssertionError("failure hook did not run")

        assert "litellm" not in sys.modules
        assert user_filter in log.filters
        log.error("original failure is still visible")
        assert "original failure is still visible" in sink.getvalue()
    ''')


def test_sdk_is_initialized_before_uvloop_exception_logging():
    run_script('''
        import io
        import logging
        import sys
        import uvloop
        from workbench.api import create_app

        assert "litellm" in sys.modules
        assert not sys.modules["litellm"].__spec__._initializing
        sink = io.StringIO()
        log = logging.getLogger("asyncio")
        log.addHandler(logging.StreamHandler(sink))

        async def main():
            import asyncio
            asyncio.get_running_loop().call_exception_handler({
                "message": "original loop error", "exception": ValueError("test cause")})

        uvloop.run(main())
        assert "original loop error" in sink.getvalue()
        assert "test cause" in sink.getvalue()
    ''')
