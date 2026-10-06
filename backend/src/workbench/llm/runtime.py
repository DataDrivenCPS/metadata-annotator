"""Initialize the SDK without leaving broken logging filters after an import failure."""

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
