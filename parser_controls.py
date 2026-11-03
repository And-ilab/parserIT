"""Правки ядра и промпта из Telegram и недельная статистика. Файл на сервере, не в git."""
from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

MINSK = timezone(timedelta(hours=3))
CONTROLS_NAME = "parser_controls.json"
STATS_NAME = "parser_weekly_stats.json"
OFFSET_NAME = "parser_controls_offset.txt"

JSON_RULE = (
    'Верни только JSON {"keep": [номера релевантных пунктов]}. '
    "Если ничего не подходит — {\"keep\": []}."
)


def controls_path(script_dir: str) -> str:
    return os.path.join(script_dir, CONTROLS_NAME)


def stats_path(script_dir: str) -> str:
    return os.path.join(script_dir, STATS_NAME)


def offset_path(script_dir: str) -> str:
    return os.path.join(script_dir, OFFSET_NAME)


def week_key(moment: datetime | None = None) -> str:
    now = moment or datetime.now(MINSK)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MINSK)
    iso = now.astimezone(MINSK).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def previous_week_key(moment: datetime | None = None) -> str:
    now = moment or datetime.now(MINSK)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MINSK)
    return week_key(now.astimezone(MINSK) - timedelta(days=7))


def _read_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_controls(script_dir: str) -> dict:
    return _read_json(controls_path(script_dir))


def save_controls(script_dir: str, data: dict) -> None:
    _write_json(controls_path(script_dir), data)


def profile_bucket(data: dict, profile_id: str) -> dict:
    raw = data.get(profile_id)
    if not isinstance(raw, dict):
        raw = {}
        data[profile_id] = raw
    raw.setdefault("roots_added", [])
    raw.setdefault("roots_removed", [])
    raw.setdefault("prompt", None)
    return raw


def effective_roots(profile, script_dir: str) -> tuple[str, ...]:
    bucket = profile_bucket(load_controls(script_dir), profile.id)
    removed = {str(x).strip().lower() for x in bucket["roots_removed"] if str(x).strip()}
    roots = [r for r in profile.keywords_roots if r.lower() not in removed]
    have = {r.lower() for r in roots}
    for extra in bucket["roots_added"]:
        text = str(extra).strip()
        if text and text.lower() not in have and text.lower() not in removed:
            roots.append(text)
            have.add(text.lower())
    return tuple(roots)


def effective_prompt(profile, script_dir: str) -> str:
    bucket = profile_bucket(load_controls(script_dir), profile.id)
    custom = bucket.get("prompt")
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    return (getattr(profile, "ai_system_prompt", "") or "").strip()


def apply_controls(profile, script_dir: str):
    prompt = effective_prompt(profile, script_dir)
    return replace(
        profile,
        keywords_roots=effective_roots(profile, script_dir),
        ai_system_prompt=prompt,
    )


def price_byn(raw: str | None) -> float | None:
    if not raw:
        return None
    text = str(raw).replace("\u00a0", " ").replace("\u202f", " ").strip()
    m = re.search(r"([\d\s\.,]+)\s*(BYN|руб\.?)\b", text, re.I)
    if not m:
        return None
    nums = "".join(ch for ch in m.group(1) if not ch.isspace())
    if nums.count(",") and nums.count("."):
        if nums.rfind(",") > nums.rfind("."):
            nums = nums.replace(".", "").replace(",", ".")
        else:
            nums = nums.replace(",", "")
    elif nums.count(","):
        if nums.count(",") == 1 and len(nums.split(",")[-1]) <= 2:
            nums = nums.replace(",", ".")
        else:
            nums = nums.replace(",", "")
    try:
        return float(nums)
    except ValueError:
        return None


def _sum_prices(tenders: list[dict]) -> tuple[float, int]:
    total = 0.0
    missing = 0
    for t in tenders:
        val = price_byn(t.get("price"))
        if val is None:
            missing += 1
        else:
            total += val
    return total, missing


def record_week_stats(
    script_dir: str,
    profile_id: str,
    *,
    semantic: int,
    rejected: list[dict],
    sent: list[dict],
) -> None:
    path = stats_path(script_dir)
    data = _read_json(path)
    weeks = data.setdefault(profile_id, {})
    if not isinstance(weeks, dict):
        weeks = {}
        data[profile_id] = weeks
    key = week_key()
    slot = weeks.get(key)
    if not isinstance(slot, dict):
        slot = {
            "semantic": 0,
            "rejected": 0,
            "sent": 0,
            "sum_rejected": 0.0,
            "sum_sent": 0.0,
            "no_price_rejected": 0,
            "no_price_sent": 0,
        }
        weeks[key] = slot
    rej_sum, rej_miss = _sum_prices(rejected)
    sent_sum, sent_miss = _sum_prices(sent)
    slot["semantic"] = int(slot.get("semantic", 0)) + semantic
    slot["rejected"] = int(slot.get("rejected", 0)) + len(rejected)
    slot["sent"] = int(slot.get("sent", 0)) + len(sent)
    slot["sum_rejected"] = float(slot.get("sum_rejected", 0)) + rej_sum
    slot["sum_sent"] = float(slot.get("sum_sent", 0)) + sent_sum
    slot["no_price_rejected"] = int(slot.get("no_price_rejected", 0)) + rej_miss
    slot["no_price_sent"] = int(slot.get("no_price_sent", 0)) + sent_miss
    _write_json(path, data)


def week_slot(script_dir: str, profile_id: str, key: str) -> dict[str, Any]:
    data = _read_json(stats_path(script_dir))
    weeks = data.get(profile_id)
    if isinstance(weeks, dict) and isinstance(weeks.get(key), dict):
        return weeks[key]
    return {}


def digest_already_sent(script_dir: str, profile_id: str, key: str) -> bool:
    data = _read_json(stats_path(script_dir))
    sent = data.get("_digest_sent")
    return isinstance(sent, dict) and sent.get(profile_id) == key


def mark_digest_sent(script_dir: str, profile_id: str, key: str) -> None:
    path = stats_path(script_dir)
    data = _read_json(path)
    sent = data.get("_digest_sent")
    if not isinstance(sent, dict):
        sent = {}
        data["_digest_sent"] = sent
    sent[profile_id] = key
    _write_json(path, data)


def _fmt_money(value: float, missing: int) -> str:
    n = int(round(value)) if abs(value - round(value)) < 0.01 else value
    if isinstance(n, float):
        text = f"{n:,.2f}".replace(",", " ")
    else:
        text = f"{n:,}".replace(",", " ")
    extra = f", без цены: {missing}" if missing else ""
    return f"{text} BYN{extra}"


def format_weekly_message(profile, script_dir: str, key: str) -> str:
    slot = week_slot(script_dir, profile.id, key)
    roots = "\n".join(f"• {html_escape(r)}" for r in profile.keywords_roots) or "—"
    prompt = html_escape(effective_prompt(profile, script_dir) or "—")
    semantic = int(slot.get("semantic", 0))
    rejected = int(slot.get("rejected", 0))
    sent = int(slot.get("sent", 0))
    year, week = key.split("-W")
    start = datetime.fromisocalendar(int(year), int(week), 1).date()
    end = start + timedelta(days=6)
    return (
        f"📊 <b>Сводка {profile.id}</b> за {start.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')}\n\n"
        f"<b>Семантическое ядро</b>\n{roots}\n\n"
        f"<b>Системный промпт</b>\n{prompt}\n\n"
        f"Прошли семантику: <b>{semantic}</b>\n"
        f"Отсеяно: <b>{rejected}</b> на {_fmt_money(float(slot.get('sum_rejected', 0)), int(slot.get('no_price_rejected', 0)))}\n"
        f"Отобрано: <b>{sent}</b> на {_fmt_money(float(slot.get('sum_sent', 0)), int(slot.get('no_price_sent', 0)))}"
    )


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def load_offsets(script_dir: str) -> dict[str, int]:
    data = _read_json(offset_path(script_dir))
    out: dict[str, int] = {}
    for key, val in data.items():
        try:
            out[str(key)] = int(val)
        except (TypeError, ValueError):
            continue
    return out


def save_offsets(script_dir: str, offsets: dict[str, int]) -> None:
    _write_json(offset_path(script_dir), offsets)
