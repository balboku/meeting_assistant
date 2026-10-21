"""Job-local course mode; company-approved vocabulary remains independently scoped."""
from contextlib import contextmanager
from contextvars import ContextVar

ACTIVE_TRAINING = ContextVar("training_transcription", default=False)


@contextmanager
def training_context(enabled):
    token = ACTIVE_TRAINING.set(bool(enabled))
    try:
        yield
    finally:
        ACTIVE_TRAINING.reset(token)
