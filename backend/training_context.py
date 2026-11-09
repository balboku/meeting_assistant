"""Job-local course mode; company-approved vocabulary remains independently scoped."""
from contextlib import contextmanager
from contextvars import ContextVar

ACTIVE_TRAINING = ContextVar("training_transcription", default=False)


@contextmanager
def training_context(enabled):
    # Real course jobs carry their part ID; older callers can still use a boolean.
    token = ACTIVE_TRAINING.set(enabled or False)
    try:
        yield
    finally:
        ACTIVE_TRAINING.reset(token)
