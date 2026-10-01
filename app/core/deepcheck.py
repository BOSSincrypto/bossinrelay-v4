"""Тяжёлая deep-check проверка модели (truemodel-подход).

Дешёвая проверка (engine.check_upstream) сверяет только response.model.
Здесь — 7 сигналов с взвешенным голосованием hard=3/soft=2/context=1:

- served_id_match (hard) — сверка response.model с claimed;
- error_id_leak (soft) — выловить id модели из текста validation-ошибки;
- output_ceiling (soft) — просьба большого max_tokens, сверка потолка с FAMILIES;
- tokenizer_fingerprint (hard, допуск 5%) — дословный повтор эталона ~200
  токенов, сравнение usage с медианой когорты семейства;
- randomness_profile (soft) — N=20 однотокенных сэмплов, энтропия vs равномерное;
- knowledge_cutoff (context) — self-report cutoff + «НЕ ЗНАЮ»-проба за cutoff;
- puzzle_battery (context) — арифметика, подсчёт букв, строгое следование формату.

RAM-бюджет 512MB: только stdlib + httpx (через providers) + ленивый numpy.
Без torch/transformers/scipy. Общий бюджет времени ~120с, каждый сигнал
в try/except — падение одного не роняет весь отчёт.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import re
import time

from ..dashboard.registry import FAMILIES
from ..dashboard.store import find_provider
from ..providers import base as pbase
from ..providers import native, openai_compat
from . import engine as _engine

DEFAULT_TIMEOUT_S = 120.0
_RANDOM_N = 20
_FINGERPRINT_TOL = 0.05

_WEIGHTS = {"hard": 3, "soft": 2, "context": 1}

_CHEAP_MARKERS = ("mini", "haiku", "flash", "nano", "lite")


def _tier(model_id: str) -> str:
    m = (model_id or "").lower()
    return "cheap" if any(t in m for t in _CHEAP_MARKERS) else "full"


# ---------- транспорт (зеркалит engine._call_one, но пробрасывает extra) ----------

async def _call_one(provider_id: str, model: str, key: dict, messages: list,
                    timeout_s: float, extra: dict | None = None) -> pbase.ProviderResult:
    """Один вызов апстрима. В self-test monkeypatch'ится целиком."""
    prov = find_provider(provider_id) or {}
    base_url = prov.get("base_url", "")
    raw_key = _engine._raw_key(provider_id, key)
    if not raw_key and not _engine._is_local(provider_id, base_url):
        raise pbase.UpstreamError(
            f"не задан ключ апстрима {provider_id} (env {provider_id.upper()}_KEYS)",
            retryable=False, error_class="auth")
    kind = native.provider_kind(provider_id)
    if kind == "anthropic":
        return await native.anthropic_chat(messages, model, raw_key, timeout_s)
    if kind == "gemini":
        return await native.gemini_chat(messages, model, raw_key, timeout_s)
    if provider_id.lower() == "mistral" and not extra:
        return await native.mistral_chat(messages, model, raw_key, timeout_s)
    return await openai_compat.chat(messages, model, base_url, raw_key,
                                    timeout_s, extra=extra)


def _pick_key(provider_id: str) -> dict | None:
    keys = _engine.live_keys(provider_id)
    if keys:
        return keys[0]
    prov = find_provider(provider_id) or {}
    on = [k for k in prov.get("keys", []) if k.get("enabled", True)]
    return on[0] if on else None


# ---------- эталоны ----------

REFERENCE_TEXT = (
    "The old lighthouse stood on the cliff for over a hundred years, guiding ships "
    "through fog and storm with its steady beam. Every evening the keeper climbed the "
    "narrow iron stairs, polished the great lens, and lit the lamp at dusk. Sailors far "
    "out at sea watched for that single point of light, and knowing its rhythm, found "
    "their way home. In winter the wind howled around the tower, and ice formed on the "
    "windows, but the light never went out. Generations of keepers wrote their names in "
    "a worn leather book, noting weather, passing vessels, and quiet nights. When radio "
    "beacons arrived, the lighthouse kept shining anyway, a patient monument to careful work."
)

_PUZZLE_EXPECTED = ["408", "30492", "5", "3", "ГОТОВО"]

_OLYMPIC_YEARS = (2024, 2028, 2032, 2036, 2040)


def _fam(model: str) -> dict:
    return FAMILIES.get(model, {}) or {}


def _cutoff_of(model: str) -> str:
    return str(_fam(model).get("cutoff", "2023-10") or "2023-10")


def _ceiling_of(model: str) -> int:
    try:
        return int(_fam(model).get("output_ceiling", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _median_of(model: str) -> float:
    m = _fam(model).get("cohort_median_tokens")
    if m:
        try:
            return float(m)
        except (TypeError, ValueError):
            pass
    return max(50.0, len(REFERENCE_TEXT) / 4.0)  # эвристика ~4 символа на токен


def _sig(name: str, weight: str, result: str, detail: str = "") -> dict:
    return {"name": name, "weight": weight, "result": result, "detail": detail[:500]}


def _entropy(samples: list[str]) -> tuple[float, int]:
    """Энтропия распределения сэмплов (биты). numpy — лениво, внутри функции."""
    import numpy as np  # noqa: PLC0415 — RAM: не держать в памяти модуля

    vals, counts = np.unique(np.asarray(samples, dtype=str), return_counts=True)
    p = counts / counts.sum()
    h = float(-np.sum(p * np.log2(p)))
    return h, int(len(vals))


# ---------- сигналы (каждый — маленькая корутина, снаружи try/except) ----------

async def _s_served_id(provider_id: str, model: str, key: dict, t: float) -> dict:
    res = await _call_one(provider_id, model, key,
                          [{"role": "user", "content": "ping"}], t)
    served = res.served_model or ""
    if served == model or served == f"{provider_id}:{model}":
        return _sig("served_id_match", "hard", "pass", f"served={served}")
    fam_claimed = str(_fam(model).get("family", "") or "").lower()
    if served and (served.split("-")[0] == model.split("-")[0]
                   or (fam_claimed and fam_claimed in served.lower())):
        return _sig("served_id_match", "hard", "partial",
                    f"семейство похоже, id другой: served={served}")
    return _sig("served_id_match", "hard", "fail", f"served={served or '∅'}")


async def _s_error_leak(provider_id: str, model: str, key: dict, t: float) -> dict:
    """Заведомо кривой запрос (нет такого id) — читаем, что прокололось в ошибке."""
    try:
        await _call_one(provider_id, f"{model}-invalid-probe-{int(time.time()) % 997}",
                        key, [{"role": "user", "content": "ping"}], t)
        return _sig("error_id_leak", "soft", "partial",
                    "апстрим принял несуществующий id — подозрительно")
    except pbase.UpstreamError as e:
        text = str(e).lower()
        # Ищем самый длинный совпавший id семейства: claimed может быть
        # подстрокой чужого id (gpt-4o в gpt-4o-mini) — это НЕ его упоминание.
        hits = [m for m in FAMILIES if m.lower() in text]
        if hits:
            longest = max(hits, key=len)
            if longest == model:
                return _sig("error_id_leak", "soft", "pass",
                            f"ошибка прозрачно содержит claimed id: {e}"[:300])
            return _sig("error_id_leak", "soft", "fail",
                        f"ошибка светит чужой id: {longest}")
        return _sig("error_id_leak", "soft", "partial",
                    f"ошибка без id моделей: {e}"[:300])
    except Exception as e:  # сеть и т.п. — не голос ошибки, а падение сигнала
        raise RuntimeError(f"транспорт: {e}")


async def _s_ceiling(provider_id: str, model: str, key: dict, t: float) -> dict:
    want = min(max(_ceiling_of(model), 4000), 8000) or 4000
    res = await _call_one(
        provider_id, model, key,
        [{"role": "user", "content":
          "Перечисли числа от 1 до 1500, каждое с новой строки, без пояснений."}],
        t, extra={"max_tokens": want, "temperature": 0.0})
    ct = int(res.completion_tokens or 0)
    if ct >= 1000:
        return _sig("output_ceiling", "soft", "pass",
                    f"completion_tokens={ct} при запросе max_tokens={want}")
    if ct >= 200:
        return _sig("output_ceiling", "soft", "partial",
                    f"потолок скромный: completion_tokens={ct}")
    return _sig("output_ceiling", "soft", "fail",
                f"потолок зажат: completion_tokens={ct}")


async def _s_fingerprint(provider_id: str, model: str, key: dict, t: float) -> dict:
    res = await _call_one(
        provider_id, model, key,
        [{"role": "user", "content":
          "Повтори следующий текст ДОСЛОВНО, без добавлений и пояснений:\n\n"
          + REFERENCE_TEXT}],
        t, extra={"temperature": 0.0})
    ct = int(res.completion_tokens or 0)
    if ct <= 0:  # usage не дали — грубая оценка по длине ответа
        ct = max(1, len(res.text) // 4)
    median = _median_of(model)
    dev = abs(ct - median) / median
    if dev <= _FINGERPRINT_TOL:
        return _sig("tokenizer_fingerprint", "hard", "pass",
                    f"tokens={ct} vs медиана когорты {median:.0f} (откл. {dev:.1%})")
    if dev <= 0.15:
        return _sig("tokenizer_fingerprint", "hard", "partial",
                    f"tokens={ct} vs медиана {median:.0f} (откл. {dev:.1%})")
    return _sig("tokenizer_fingerprint", "hard", "fail",
                f"tokens={ct} vs медиана {median:.0f} (откл. {dev:.1%})")


async def _s_randomness(provider_id: str, model: str, key: dict, t: float) -> dict:
    async def one() -> str:
        try:
            r = await _call_one(
                provider_id, model, key,
                [{"role": "user", "content":
                  "Выведи одну случайную цифру от 0 до 9. Только цифру, без пояснений."}],
                t, extra={"temperature": 1.0, "max_tokens": 1})
            m = re.search(r"\d", r.text or "")
            return m.group(0) if m else "?"
        except Exception:
            return "?"

    samples = await asyncio.gather(*[one() for _ in range(_RANDOM_N)])
    samples = [s for s in samples if s != "?"]
    if len(samples) < 10:
        raise RuntimeError(f"слишком мало сэмплов: {len(samples)}/{_RANDOM_N}")
    h, uniq = _entropy(samples)
    detail = f"H={h:.2f} бит, уникальных={uniq}/{len(samples)}"
    if uniq == 1:
        return _sig("randomness_profile", "soft", "fail", detail + " — выборка зажата")
    if h >= 2.0:
        return _sig("randomness_profile", "soft", "pass", detail)
    if h >= 1.0:
        return _sig("randomness_profile", "soft", "partial", detail)
    return _sig("randomness_profile", "soft", "fail", detail)


async def _s_cutoff(provider_id: str, model: str, key: dict, t: float) -> dict:
    cutoff = _cutoff_of(model)
    r1 = await _call_one(
        provider_id, model, key,
        [{"role": "user", "content":
          "Назови дату окончания своих обучающих данных (knowledge cutoff). "
          "Ответь СТРОГО одной строкой в формате ГГГГ-ММ, без пояснений."}],
        t, extra={"temperature": 0.0})
    ok1 = False
    m = re.search(r"(\d{4})[-.](\d{2})", r1.text or "")
    if m:
        try:
            cy, cm = int(cutoff.split("-")[0]), int(cutoff.split("-")[1])
            ry, rm = int(m.group(1)), int(m.group(2))
            ok1 = abs((ry * 12 + rm) - (cy * 12 + cm)) <= 3
        except (ValueError, IndexError):
            ok1 = False
    try:
        cy = int(cutoff.split("-")[0])
    except ValueError:
        cy = 2023
    oly = next((y for y in _OLYMPIC_YEARS if y > cy), cy + 2)
    r2 = await _call_one(
        provider_id, model, key,
        [{"role": "user", "content":
          f"Кто выиграл мужской забег на 100 метров на летних Олимпийских играх "
          f"{oly}? Если это событие позже твоего knowledge cutoff — "
          f"ответь ровно: НЕ ЗНАЮ."}],
        t, extra={"temperature": 0.0})
    t2 = (r2.text or "").lower()
    ok2 = ("не знаю" in t2 or "не могу" in t2 or "неизвестн" in t2
           or "cutoff" in t2 or "после моего" in t2)
    score = int(ok1) + int(ok2)
    detail = f"self-report cutoff: {'ок' if ok1 else 'мимо'} ({(r1.text or '').strip()[:40]}); " \
             f"НЕ-ЗНАЮ проба {oly}г: {'ок' if ok2 else 'мимо'}"
    if score == 2:
        return _sig("knowledge_cutoff", "context", "pass", detail)
    if score == 1:
        return _sig("knowledge_cutoff", "context", "partial", detail)
    return _sig("knowledge_cutoff", "context", "fail", detail)


async def _s_puzzle(provider_id: str, model: str, key: dict, t: float) -> dict:
    res = await _call_one(
        provider_id, model, key,
        [{"role": "user", "content":
          "Реши 5 заданий. Ответь СТРОГО 5 строками, каждая строка — только ответ, "
          "без пояснений:\n"
          "1) Сколько будет 17*24? Ответ: число.\n"
          "2) Сколько будет 847*36? Ответ: число.\n"
          "3) Сколько букв «а» в слове «абракадабра»? Ответ: число.\n"
          "4) Сколько букв «z» в слове «zqzqz»? Ответ: число.\n"
          "5) Пятая строка — ровно слово ГОТОВО."}],
        t, extra={"temperature": 0.0})
    lines = [ln.strip() for ln in (res.text or "").splitlines() if ln.strip()]
    hits = 0
    for i, exp in enumerate(_PUZZLE_EXPECTED):
        if i >= len(lines):
            break
        if exp == "ГОТОВО":
            hits += lines[i].strip().upper() == "ГОТОВО"
        else:
            hits += exp in re.findall(r"\d+", lines[i])
    detail = f"совпадений {hits}/5"
    if hits >= 4:
        return _sig("puzzle_battery", "context", "pass", detail)
    if hits >= 2:
        return _sig("puzzle_battery", "context", "partial", detail)
    return _sig("puzzle_battery", "context", "fail", detail)


_SIGNALS: tuple[tuple[str, float], ...] = (
    ("served_id_match", 20.0),
    ("error_id_leak", 15.0),
    ("output_ceiling", 45.0),
    ("tokenizer_fingerprint", 20.0),
    ("randomness_profile", 12.0),
    ("knowledge_cutoff", 15.0),
    ("puzzle_battery", 20.0),
)
_FNS = {"served_id_match": _s_served_id, "error_id_leak": _s_error_leak,
        "output_ceiling": _s_ceiling, "tokenizer_fingerprint": _s_fingerprint,
        "randomness_profile": _s_randomness, "knowledge_cutoff": _s_cutoff,
        "puzzle_battery": _s_puzzle}


# ---------- вердикт ----------

def _judge(signals: list[dict], claimed: str, served: str | None) -> tuple[str, float]:
    pro, con = 0.0, 0.0
    for s in signals:
        w = _WEIGHTS.get(s.get("weight", ""), 0)
        if s.get("result") == "pass":
            pro += w
        elif s.get("result") == "fail":
            con += w
        elif s.get("result") == "partial":
            pro += w * 0.5
    total = pro + con
    if total <= 0:
        return "ambiguous", 0.0
    conf = round(pro / total, 2)
    if served and _tier(served) != _tier(claimed):
        return "wrong-tier", conf
    if conf >= 0.9:
        return "matches", conf
    if conf >= 0.7:
        return "likely", conf
    if pro > 0 and con > 0:
        return "ambiguous", conf
    return "mismatch", conf


# ---------- точка входа ----------

async def run_deep_check(provider_id: str, model: str, key: dict | None = None,
                         timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Тяжёлая проверка модели. Каждый сигнал изолирован try/except."""
    t0 = time.monotonic()
    claimed = model
    k = key or _pick_key(provider_id)
    if not k:
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "failed", "provider": provider_id, "model": model,
                "claimed": claimed, "signals": [], "verdict": "ambiguous",
                "confidence": 0.0, "duration_ms": ms,
                "note": "нет включённых ключей у провайдера"}
    signals: list[dict] = []
    served: str | None = None
    deadline = t0 + max(20.0, timeout)
    for name, own_t in _SIGNALS:
        left = deadline - time.monotonic()
        if left < 8.0:  # бюджет исчерпан — остальные сигналы не запускаем
            signals.append(_sig(name, _wname(name), "error", "бюджет времени исчерпан"))
            continue
        try:
            s = await asyncio.wait_for(_FNS[name](provider_id, model, k, min(own_t, left)),
                                       timeout=min(own_t, left) + 2.0)
            signals.append(s)
            if name == "served_id_match":
                d = s.get("detail", "")
                m = re.search(r"served=(\S+)", d)
                served = m.group(1) if m else None
        except Exception as e:
            signals.append(_sig(name, _wname(name), "error",
                                f"{type(e).__name__}: {e}"[:200]))
    verdict, conf = _judge(signals, claimed, served)
    voted = [s for s in signals if s["result"] in ("pass", "partial", "fail")]
    status = "done" if len(voted) == len(signals) else ("partial" if voted else "failed")
    if status == "failed":
        verdict, conf = "ambiguous", 0.0
    ms = int((time.monotonic() - t0) * 1000)
    return {"status": status, "provider": provider_id, "model": model,
            "claimed": claimed, "signals": signals, "verdict": verdict,
            "confidence": conf, "duration_ms": ms, "at": int(time.time())}


def _wname(name: str) -> str:
    return {"served_id_match": "hard", "error_id_leak": "soft",
            "output_ceiling": "soft", "tokenizer_fingerprint": "hard",
            "randomness_profile": "soft", "knowledge_cutoff": "context",
            "puzzle_battery": "context"}[name]


# ---------- self-test: мок _call_one, без живых ключей ----------

class _Canned:
    """Canned-транспорт: match — честная модель, mismatch — подмена."""

    def __init__(self, mode: str, claimed: str):
        assert mode in ("match", "mismatch")
        self.mode = mode
        self.claimed = claimed
        self.digits = ["3", "7", "1", "9", "4", "0", "6", "2", "8", "5",
                       "0", "4", "8", "2", "6", "1", "9", "5", "3", "7"]
        self.i = 0

    def _res(self, text: str, ct: int, served: str | None = None) -> pbase.ProviderResult:
        return pbase.ProviderResult(text=text, served_model=served,
                                     prompt_tokens=10, completion_tokens=ct)

    async def __call__(self, provider_id: str, model: str, key: dict,
                       messages: list, timeout_s: float,
                       extra: dict | None = None) -> pbase.ProviderResult:
        prompt = " ".join(str(m.get("content", "")) for m in messages)
        bad = model.endswith("-invalid-probe") or "invalid-probe" in model
        if bad:
            if self.mode == "match":
                raise pbase.UpstreamError(
                    f"upstream 404: {{\"error\": \"model '{self.claimed}' does not exist\"}}",
                    status=404, retryable=False, error_class="not_found")
            raise pbase.UpstreamError(
                'upstream 404: {"error": "model \'gpt-4o-mini\' does not exist"}',
                status=404, retryable=False, error_class="not_found")
        if prompt.strip() == "ping":
            served = self.claimed if self.mode == "match" else "some-other-model"
            return self._res("pong", 3, served)
        if "Перечисли числа" in prompt:
            n = 1500 if self.mode == "match" else 120
            body = "\n".join(str(i) for i in range(1, n + 1))
            return self._res(body, n, self.claimed)
        if "ДОСЛОВНО" in prompt:
            n = 203 if self.mode == "match" else 260
            return self._res(REFERENCE_TEXT, n, self.claimed)
        if "случайную цифру" in prompt:
            if self.mode == "match":
                d = self.digits[self.i % len(self.digits)]
                self.i += 1
                return self._res(d, 1, self.claimed)
            return self._res("7", 1, "some-other-model")
        if "knowledge cutoff" in prompt:
            text = "2023-10" if self.mode == "match" else "2025-06"
            return self._res(text, 4, self.claimed)
        if "Олимпийских играх" in prompt:
            text = "НЕ ЗНАЮ" if self.mode == "match" else "Американец Ноа Лайлс."
            return self._res(text, 5, self.claimed)
        if "5 заданий" in prompt:
            if self.mode == "match":
                return self._res("408\n30492\n5\n3\nГОТОВО", 12, self.claimed)
            return self._res("400\n30000\n4\n2\nготово наверное", 12, "some-other-model")
        return self._res("?", 1, self.claimed)


async def _self_test() -> int:
    global _call_one
    real = _call_one
    failures = 0
    try:
        for mode, want in (("match", "matches"), ("mismatch", "mismatch")):
            _call_one = _Canned(mode, "gpt-4o")  # type: ignore[assignment]
            try:
                rep = await run_deep_check("openai", "gpt-4o", {"id": "k1"}, timeout=60)
            finally:
                _call_one = real  # type: ignore[assignment]
            got = rep["verdict"]
            line = " ".join(f"{s['name']}={s['result']}" for s in rep["signals"])
            print(f"[{mode}] verdict={got} conf={rep['confidence']} "
                  f"status={rep['status']} {rep['duration_ms']}ms", flush=True)
            print(f"  {line}", flush=True)
            if got != want:
                print(f"  ОЖИДАЛОСЬ {want} — ПРОВАЛ", flush=True)
                failures += 1
        print("SELF-TEST OK" if not failures else "SELF-TEST FAILED", flush=True)
    finally:
        from ..db import client as _dbc
        _dbc.close_client()  # освободить поток libsql-executor, иначе вис на выходе
    return 1 if failures else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="deep-check self-test без живых ключей")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        raise SystemExit(asyncio.run(_self_test()))
    ap.print_help()


if __name__ == "__main__":
    main()
