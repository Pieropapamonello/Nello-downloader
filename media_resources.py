"""One expensive media operation at a time across all frontend threads."""

from functools import wraps
from threading import BoundedSemaphore

_media_slot = BoundedSemaphore(1)


def limited_media(function):
    @wraps(function)
    def run(*args, **kwargs):
        # Acquire in the worker thread, never on an asyncio event loop.
        # Cancelling the caller does not release the slot while work still runs.
        with _media_slot:
            return function(*args, **kwargs)
    return run
