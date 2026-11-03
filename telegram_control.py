"""Команды Telegram: ядро и промпт профилей. Опрос getUpdates, без хвоста в рассылках."""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime

import requests

from angar_parser import ANGAR_PROFILE
from equipment_parser import EQUIPMENT_PROFILE
from icetrade_parser_core import (
    _load_dotenv_if_present,
    angar_chat_refusal,
    resolve_bot_token,
    resolve_chat_id,
    send_telegram,
)
from it_parser import IT_PROFILE
from parser_controls import (
    apply_controls,
    digest_already_sent,
    effective_prompt,
    format_weekly_message,
    html_escape,
    load_controls,
    load_offsets,
    mark_digest_sent,
    previous_week_key,
    profile_bucket,
    save_controls,
    save_offsets,
)
from parser_controls import MINSK

PROFILES = (IT_PROFILE, ANGAR_PROFILE, EQUIPMENT_PROFILE)
IDS = {p.id: p for p in PROFILES}

HELP = (
    "<b>Команды</b>\n"
    "/help — этот список\n"
    "/core [it|angar|equipment] — семантическое ядро\n"
    "/prompt [it|angar|equipment] — системный промпт\n"
    "/core_add [профиль] корень — добавить корень\n"
    "/core_del [профиль] корень — убрать корень\n"
    "/prompt_set [профиль] текст — заменить промпт\n"
    "/reset [профиль] core — ядро как в коде\n"
    "/reset [профиль] prompt — промпт как в коде\n\n"
    "Профиль можно не писать, если в этом чате он один. "
    "Правки подхватит следующий запуск парсера. "
    "По вторникам в рабочее время (Минск, 9:00–18:00) сюда один раз приходит сводка: "
    "ядро, промпт, сколько прошло семантику, сколько отсеяно и отобрано и на какую сумму."
)


def _script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _chats() -> dict[str, list]:
    """chat_id → профили, которые в него пишут."""
    found: dict[str, list] = {}
    for profile in PROFILES:
        chat = resolve_chat_id(profile)
        if not chat:
            continue
        if profile.id == "angar" and angar_chat_refusal(resolve_bot_token(profile), chat):
            print(f"Пропускаю команды angar: чат {chat} не Sales/Ангары")
            continue
        found.setdefault(chat, []).append(profile)
    return found


def _resolve_profile(chat_profiles: list, arg: str):
    token = (arg or "").strip().lower()
    if token in IDS and any(p.id == token for p in chat_profiles):
        return IDS[token], ""
    if len(chat_profiles) == 1 and token not in IDS:
        return chat_profiles[0], arg
    if token in IDS:
        return None, "Этот профиль пишет в другой чат."
    names = ", ".join(p.id for p in chat_profiles)
    return None, f"Укажите профиль: {names}"


def _split_profile(chat_profiles: list, rest: str):
    rest = (rest or "").strip()
    if not rest:
        profile, _ = _resolve_profile(chat_profiles, "")
        return profile, "", "Укажите профиль." if profile is None and len(chat_profiles) != 1 else ""
    head, _, tail = rest.partition(" ")
    if head.lower() in IDS:
        profile, err = _resolve_profile(chat_profiles, head)
        return profile, tail.strip(), err or ""
    profile, leftover = _resolve_profile(chat_profiles, "")
    if profile is None:
        return None, rest, leftover if isinstance(leftover, str) and leftover.startswith("Укаж") else "Укажите профиль."
    return profile, rest, ""


def _reply_core(profile, script_dir: str) -> str:
    live = apply_controls(profile, script_dir)
    lines = "\n".join(f"• {html_escape(r)}" for r in live.keywords_roots) or "—"
    return f"<b>Ядро {profile.id}</b>\n{lines}"


def _reply_prompt(profile, script_dir: str) -> str:
    text = html_escape(effective_prompt(apply_controls(profile, script_dir), script_dir) or "—")
    note = "" if profile.ai_filter else "\n\nAI-фильтр у этого профиля выключен: текст хранится, на отбор лотов не влияет."
    return f"<b>Промпт {profile.id}</b>\n{text}{note}"


def handle_command(chat_profiles: list, text: str, script_dir: str) -> str:
    raw = text.strip()
    if not raw.startswith("/"):
        return ""
    body = raw.split()[0].split("@", 1)[0].lower()
    rest = raw[len(raw.split()[0]) :].strip()
    if body == "/help":
        return HELP
    if body == "/core":
        profile, err = _resolve_profile(chat_profiles, rest.split()[0] if rest else "")
        if rest and rest.split()[0].lower() not in IDS and len(chat_profiles) != 1:
            profile, err = _resolve_profile(chat_profiles, "")
        if profile is None:
            return err
        return _reply_core(profile, script_dir)
    if body == "/prompt":
        profile, err = _resolve_profile(chat_profiles, rest.split()[0] if rest else "")
        if profile is None:
            return err
        return _reply_prompt(profile, script_dir)
    if body in ("/core_add", "/core_del"):
        profile, root, err = _split_profile(chat_profiles, rest)
        if profile is None:
            return err or "Укажите профиль."
        if not root:
            return "Напишите корень после команды."
        data = load_controls(script_dir)
        bucket = profile_bucket(data, profile.id)
        key = root.lower()
        if body == "/core_add":
            bucket["roots_removed"] = [x for x in bucket["roots_removed"] if str(x).lower() != key]
            if not any(str(x).lower() == key for x in bucket["roots_added"]):
                if not any(r.lower() == key for r in profile.keywords_roots):
                    bucket["roots_added"].append(root)
            save_controls(script_dir, data)
            return f"Корень «{html_escape(root)}» добавлен в {profile.id}.\n\n" + _reply_core(profile, script_dir)
        bucket["roots_added"] = [x for x in bucket["roots_added"] if str(x).lower() != key]
        if not any(str(x).lower() == key for x in bucket["roots_removed"]):
            bucket["roots_removed"].append(root)
        save_controls(script_dir, data)
        return f"Корень «{html_escape(root)}» убран из {profile.id}.\n\n" + _reply_core(profile, script_dir)
    if body == "/prompt_set":
        profile, prompt, err = _split_profile(chat_profiles, rest)
        if profile is None:
            return err or "Укажите профиль."
        if not prompt:
            return "Напишите текст промпта после команды."
        data = load_controls(script_dir)
        profile_bucket(data, profile.id)["prompt"] = prompt
        save_controls(script_dir, data)
        return f"Промпт {profile.id} сохранён.\n\n" + _reply_prompt(profile, script_dir)
    if body == "/reset":
        profile, what, err = _split_profile(chat_profiles, rest)
        if profile is None:
            return err or "Укажите профиль."
        what = what.strip().lower()
        data = load_controls(script_dir)
        bucket = profile_bucket(data, profile.id)
        if what == "core":
            bucket["roots_added"] = []
            bucket["roots_removed"] = []
            save_controls(script_dir, data)
            return f"Ядро {profile.id} сброшено.\n\n" + _reply_core(profile, script_dir)
        if what == "prompt":
            bucket["prompt"] = None
            save_controls(script_dir, data)
            return f"Промпт {profile.id} сброшен.\n\n" + _reply_prompt(profile, script_dir)
        return "Напишите: /reset core или /reset prompt."
    return ""


def _chunks(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    buf = ""
    for line in text.split("\n"):
        piece = line + "\n"
        if buf and len(buf) + len(piece) > limit:
            parts.append(buf)
            buf = ""
        buf += piece
    if buf:
        parts.append(buf)
    return parts or [text[:limit]]


def poll_once(script_dir: str) -> None:
    chats = _chats()
    if not chats:
        print("Нет chat_id — команды некому принимать.")
        return
    tokens: list[str] = []
    for profile in PROFILES:
        token = resolve_bot_token(profile)
        if token and resolve_chat_id(profile) and token not in tokens:
            tokens.append(token)
    offsets = load_offsets(script_dir)
    for token in tokens:
        key = hashlib.sha256(token.encode()).hexdigest()[:12]
        offset = offsets.get(key, 0)
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'},
                timeout=30,
            )
        except requests.RequestException as e:
            print(f"getUpdates: {e}")
            continue
        if not r.ok:
            print(f"getUpdates HTTP {r.status_code}: {r.text[:200]}")
            continue
        for upd in r.json().get("result") or []:
            offset = max(offset, int(upd.get("update_id", 0)) + 1)
            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            text = msg.get("text") or ""
            profiles = chats.get(chat_id)
            if not profiles or not text.startswith("/"):
                continue
            answer = handle_command(profiles, text, script_dir)
            if not answer:
                continue
            for part in _chunks(answer):
                send_telegram(token, chat_id, part, retries=3)
        offsets[key] = offset
    save_offsets(script_dir, offsets)


def maybe_weekly(script_dir: str) -> None:
    """Один раз по вторникам, 9:00–18:00 Минск, в чат каждого профиля."""
    now = datetime.now(MINSK)
    if now.weekday() != 1 or not (9 <= now.hour < 18):
        return
    key = previous_week_key(now)
    for profile in PROFILES:
        chat = resolve_chat_id(profile)
        token = resolve_bot_token(profile)
        if not chat or not token:
            continue
        if profile.id == "angar" and angar_chat_refusal(token, chat):
            print("Сводка angar не отправлена: чат не Sales/Ангары")
            continue
        if digest_already_sent(script_dir, profile.id, key):
            continue
        live = apply_controls(profile, script_dir)
        text = format_weekly_message(live, script_dir, key)
        ok = True
        for part in _chunks(text):
            if not send_telegram(token, chat, part, retries=3):
                ok = False
                break
        if ok:
            mark_digest_sent(script_dir, profile.id, key)
            print(f"Сводка {profile.id} за {key} отправлена")


def main() -> None:
    script_dir = _script_dir()
    _load_dotenv_if_present(script_dir)
    poll_once(script_dir)
    maybe_weekly(script_dir)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"telegram_control: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
