from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class IntegrationError(Exception):
    """Base for external integration failures."""


class TransientError(IntegrationError):
    pass


class RateLimitedError(TransientError):
    def __init__(self, msg="rate limited", retry_after: float = 0):
        super().__init__(msg)
        self.retry_after = retry_after


class AuthExpiredError(IntegrationError):
    """Not retryable: requires re-authorisation."""


class UncertainOutcomeError(IntegrationError):
    """The request may or may not have taken effect. Never blindly retry; reconcile."""


def with_backoff(fn: Callable[[], T], max_attempts: int = 3, base: float = 0.5, sleep=time.sleep) -> tuple[T, int]:
    """Retry only TransientError. Returns (result, attempts). Other errors propagate immediately."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(), attempt
        except TransientError as e:
            if attempt >= max_attempts:
                raise
            delay = max(getattr(e, "retry_after", 0), base * (2 ** (attempt - 1)))
            sleep(delay)
