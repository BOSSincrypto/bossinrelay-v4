"""Общий интерфейс провайдеров: нормализованный результат + типизированная ошибка."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProviderResult:
    text: str
    served_model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class StreamEvent:
    delta: str = ""
    served_model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    done: bool = False


class UpstreamError(Exception):
    """Ошибка апстрима. retryable=False — не пробовать следующий ключ
    (например, 401/404/422: проблема в конфиге, а не в ключе)."""

    def __init__(self, message: str, status: int | None = None,
                 retryable: bool = True, error_class: str | None = None,
                 retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.error_class = error_class or "server"
        self.retry_after = retry_after


def classify_status(status: int | None) -> tuple[bool, str]:
    if status == 401 or status == 403:
        return False, "auth"
    if status == 404:
        return False, "not_found"
    if status == 422:
        return False, "server"
    if status == 429:
        return True, "rate_limited"
    if status == 402:
        return False, "quota"
    return True, "server"
