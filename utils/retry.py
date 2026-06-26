"""
utils/retry.py

Shared exponential-backoff-with-jitter retry policy for every external call
in the pipeline (Azure DevOps REST, Groq, Databricks, SMTP/SendGrid/ACS,
Teams webhook).

Gap addressed: "Error Handling & Retry" (High) — the spec previously had
no retry logic anywhere, so a single transient network blip produced a
completely empty run with no recovery path.

Usage:
    from utils.retry import retryable

    @retryable("azure-devops")
    def _get(url, params=None):
        ...

Each call site gets independent retry state (no shared circuit breaker),
logs a warning on every retry attempt, and re-raises the final exception
after MAX_ATTEMPTS so the caller can decide how to degrade gracefully.
"""

import logging
import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
MIN_WAIT_SECONDS = 1
MAX_WAIT_SECONDS = 20

# Exceptions worth retrying — connection issues, timeouts, and 5xx/429 HTTP
# responses (raised as requests.HTTPError by response.raise_for_status()).
RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.HTTPError,
)


def _is_retryable_http_error(exc: BaseException) -> bool:
    """Only retry HTTPError for 429 / 5xx — never for 4xx client errors
    like 401/403/404, which won't be fixed by retrying."""
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status is not None and status not in (429, 500, 502, 503, 504):
            return False
        return True
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))


class _RetryPredicate:
    """tenacity retry predicate — only retry exceptions worth retrying."""

    def __call__(self, retry_state):
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if exc is None:
            return False
        return _is_retryable_http_error(exc)


def retryable(call_name: str, max_attempts: int = MAX_ATTEMPTS):
    """
    Decorator factory: exponential backoff with jitter, logging every retry.
    call_name is just a label used in log lines (e.g. "azure-devops", "groq").
    """
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_random_exponential(multiplier=MIN_WAIT_SECONDS, max=MAX_WAIT_SECONDS),
        retry=_RetryPredicate(),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )


def retryable_any_exception(call_name: str, max_attempts: int = MAX_ATTEMPTS):
    """
    Looser variant for clients (Groq SDK, Databricks connector, smtplib) that
    don't raise requests.exceptions — retries on any Exception. Used where the
    underlying SDK doesn't give us a clean way to distinguish transient vs.
    permanent failures.
    """
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_random_exponential(multiplier=MIN_WAIT_SECONDS, max=MAX_WAIT_SECONDS),
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
