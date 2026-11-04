"""Initialize the SDK without leaving broken logging filters after an import failure."""

import atexit
import importlib
import logging


def initialize_litellm():
    try:
        return importlib.import_module("litellm")
    except BaseException:
        # LiteLLM attaches filters to asyncio/uvicorn/httpx before its import is
        # complete. A failed import removes sys.modules['litellm'], but logging
        # keeps those filters alive; their lazy imports then hide the real error.
        loggers = [logging.getLogger(), *[
            value for value in logging.Logger.manager.loggerDict.copy().values()
            if isinstance(value, logging.Logger)]]
        targets = [*loggers, *{handler for logger in loggers for handler in logger.handlers}]
        for target in targets:
            for filt in tuple(target.filters):
                if type(filt).__module__.startswith("litellm."):
                    target.removeFilter(filt)
        raise


litellm = initialize_litellm()


def _skip_async_handler_finalizers() -> None:
    """At exit, stop LiteLLM's async HTTP handlers from cleaning up in ``__del__``.

    That finalizer imports a module. Run during interpreter teardown, the import can deadlock
    on the import lock, and the process never exits (test runs that pass and then hang). The
    process is ending, so there is nothing left for it to close.
    """
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    AsyncHTTPHandler.__del__ = lambda self: None  # type: ignore[method-assign]


atexit.register(_skip_async_handler_finalizers)
