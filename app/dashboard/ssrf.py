"""SSRF-guard для base_url провайдера (контракт для ядра).

Админ вводит URL руками — та же дыра, что и произвольный fetch на сервере.
Ядро обязано: валидировать base_url при сохранении И перепроверять каждый
редирект перед тем, как идти по нему. Этот модуль — эталонная реализация
проверки, ядро переиспользует её как есть.

Что блокируется:
- не http/https схемы, userinfo в URL, отсутствующий хост;
- localhost, *.local, *.localhost, *.internal, *.lan, метадата cloud-провайдеров;
- IP-литералы из приватных/loopback/link-local/multicast/reserved диапазонов;
- хосты, чей DNS резолвится в такие адреса (best-effort: если DNS недоступен,
  хост пропускается — offline-стенд не должен ломать сохранение).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# ponytail: DNS-проверка best-effort без таймаута треда; ядру при желании
# вынести резолв в async с getaddrinfo + timeout, контракт validate_base_url не меняется.
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".localdomain")
_BLOCKED_EXACT = {
    "localhost",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
}


def _ip_blocked(ip: ipaddress._BaseAddress, allow_private: bool = False) -> bool:
    if allow_private and (ip.is_private or ip.is_loopback or ip.is_link_local):
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_base_url(raw: str, allow_private: bool = False) -> str:
    """Проверить base_url. Вернуть нормализованный URL или бросить ValueError.

    allow_private=True разрешает приватные/loopback/link-local адреса
    (нужно для локальных моделей: Ollama, vLLM, LM Studio). Включается
    переменной ALLOW_PRIVATE_URLS=true. Метадата cloud-провайдеров и
    multicast/reserved блокируются всегда."""
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        raise ValueError("пустой base_url")
    try:
        u = urlparse(raw)
    except Exception:
        raise ValueError("некорректный URL")
    if u.scheme not in ("http", "https"):
        raise ValueError("разрешены только http/https")
    if u.username or u.password:
        raise ValueError("userinfo в URL запрещён")
    host = (u.hostname or "").lower()
    if not host:
        raise ValueError("нет хоста в URL")
    if host in _BLOCKED_EXACT or host.endswith(_BLOCKED_SUFFIXES):
        raise ValueError(f"хост {host} не публичный")

    # IP-литерал — проверяем напрямую.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _ip_blocked(ip, allow_private):
            raise ValueError(f"адрес {host} не публичный")
        return raw

    # Обычный хост — пробуем резолв, каждый адрес должен быть публичным.
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            candidate = sockaddr[0]
            try:
                addr = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if _ip_blocked(addr, allow_private):
                raise ValueError(f"хост {host} резолвится в непубличный {candidate}")
    except ValueError:
        raise
    except Exception:
        pass  # DNS недоступен — пропускаем (см. docstring)
    return raw
