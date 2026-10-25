"""
Парсер zakupki.gov.ru (44-ФЗ и 223-ФЗ) -> Telegram, профиль оборудования.

Ключевые слова и стоп-листы — те же, что у белорусского equipment_parser.py.
Чат: ZAKUPKI_EQUIPMENT_TELEGRAM_CHAT_ID (группа Sales/Оборудование Россия).
"""
from __future__ import annotations

import os
import random
import sys
import time
from datetime import datetime, timedelta

import equipment_parser
from icetrade_parser_core import (
    HEADER_RESERVE_DEFAULT,
    RunnerConfig,
    _DEFAULT_BOT_TOKEN_FALLBACK,
    _env_first,
    _load_dotenv_if_present,
    append_transfer_journal,
    build_telegram_chunks,
    extract_tender_id,
    is_blacklisted,
    load_sent_ids,
    matches_roots,
    resolve_mention,
    save_sent_id,
    send_telegram,
    telegram_warmup,
)
from zakupki_it_parser import (
    _block_labeled,
    _curl,
    _int_env,
    _price_and_deadline,
    _search_url,
    icetrade_tee,
)


def parse_page(html: str) -> tuple[list[dict], int]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    found = []
    blocks = soup.select("div.search-registry-entry-block")
    roots = tuple(equipment_parser.KEYWORDS_ROOTS)
    blacklist = tuple(equipment_parser.BLACKLIST)
    blacklist_soft = tuple(equipment_parser.BLACKLIST_SOFT)
    strong = tuple(equipment_parser.KEYWORDS_STRONG)
    for block in blocks:
        href = ""
        for candidate in block.select("a[href*='regNumber=']"):
            h = candidate.get("href") or ""
            if "notice/" in h and "printForm" not in h and "view" in h:
                href = h
                break
        reg = extract_tender_id(href)
        if not reg or not href:
            continue
        title = _block_labeled(block, "Объект закупки")
        customer = _block_labeled(block, "Заказчик")
        haystack = f"{title} {customer}"
        if not matches_roots(haystack, roots):
            continue
        if is_blacklisted(
            haystack,
            blacklist,
            blacklist_soft=blacklist_soft,
            keywords_strong_roots=strong,
        ):
            continue
        price, deadline = _price_and_deadline(block)
        url = href if href.startswith("http") else "https://zakupki.gov.ru" + href
        found.append(
            {
                "title": title or f"Закупка {reg}",
                "customer": customer or "—",
                "price": price,
                "date_end": deadline,
                "url": url,
            }
        )
    return found, len(blocks)


def main() -> int:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    _load_dotenv_if_present(script_dir)
    token = (
        _env_first("ZAKUPKI_EQUIPMENT_TELEGRAM_BOT_TOKEN",
            "EQUIPMENT_TELEGRAM_BOT_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "BOT_TOKEN")
        or _DEFAULT_BOT_TOKEN_FALLBACK
    )
    chat = _env_first("ZAKUPKI_EQUIPMENT_TELEGRAM_CHAT_ID")
    mention = os.environ.get("ZAKUPKI_EQUIPMENT_TELEGRAM_MENTION", "").strip() or resolve_mention(equipment_parser.EQUIPMENT_PROFILE)
    days_back = _int_env("ZAKUPKI_EQUIPMENT_DAYS_BACK", _int_env("ZAKUPKI_DAYS_BACK", 7))
    max_pages = _int_env("ZAKUPKI_EQUIPMENT_MAX_PAGES", _int_env("ZAKUPKI_MAX_PAGES", 15))
    retries = _int_env("TELEGRAM_SEND_RETRIES", 6)
    timeout = float(os.environ.get("ZAKUPKI_HTTP_TIMEOUT", "40") or "40")

    log_path = os.path.join(script_dir, "zakupki_equipment_parser_log.txt")
    if os.path.exists(log_path):
        os.remove(log_path)
    log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
    sys.stdout = icetrade_tee(sys.stdout, log_handle)
    sys.stderr = icetrade_tee(sys.stderr, log_handle)

    if not chat:
        print("❌ Задайте ZAKUPKI_EQUIPMENT_TELEGRAM_CHAT_ID (группа Sales/Оборудование Россия).")
        return 1

    today = datetime.now()
    date_to = today.strftime("%d.%m.%Y")
    date_from = (today - timedelta(days=days_back)).strftime("%d.%m.%Y")
    sent_path = os.path.join(script_dir, "sent_zakupki_equipment_ids.txt")
    sent = load_sent_ids(sent_path)
    cookie = os.path.join(script_dir, "logs", "zakupki_equipment.cookies")
    os.makedirs(os.path.dirname(cookie), exist_ok=True)

    print("Парсер станков и оборудования ЕИС (zakupki.gov.ru) запущен")
    print(f"Диапазон: {date_from} - {date_to} (последние {days_back} дней)")
    print(f"Уже отправлено: {len(sent)}")

    fresh: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        url = _search_url(date_from, date_to, page)
        print(f"\n--- Страница {page} ---")
        try:
            html = _curl(url, timeout, cookie)
        except Exception as e:
            print(f"❌ Страница {page}: {e}")
            if page == 1:
                return 1
            break
        batch, n_blocks = parse_page(html)
        print(f"  карточек: {n_blocks}, подходящих: {len(batch)}")
        if n_blocks == 0:
            break
        for t in batch:
            tid = extract_tender_id(t["url"])
            if not tid or tid in sent or tid in seen:
                continue
            seen.add(tid)
            fresh.append(t)
            print(f"  новый: {tid} {t['title'][:80]}")
        time.sleep(random.uniform(1.5, 3.0))

    print(f"\nИтого новых: {len(fresh)}")
    if not fresh:
        append_transfer_journal(script_dir, "zakupki_equipment_cycle_summary", new_candidates=0, telegram_ok=True)
        print("Новых закупок по оборудованию нет.")
        return 0

    rc = RunnerConfig(
        profile=equipment_parser.EQUIPMENT_PROFILE,
        script_dir=script_dir,
        bot_token=token,
        chat_id=chat,
        days_back=days_back,
        max_pages=max_pages,
        telegram_send_retries=retries,
        telegram_safe_text_limit=_int_env("TELEGRAM_SAFE_TEXT_LIMIT", 3800),
        header_reserve=HEADER_RESERVE_DEFAULT,
    )

    def tmpl_single(**kw: object) -> str:
        return (
            f"{kw.get('mention', mention)}\n"
            f"📋 <b>Новые тендеры ЕИС (Россия): станки, линии, оборудование — за {kw.get('days_back', days_back)} дней: "
            f"{kw.get('total', 0)}</b>\n\n"
        )

    def tmpl_part(**kw: object) -> str:
        return (
            f"{kw.get('mention', mention)}\n"
            f"📋 <b>Станки / линии / оборудование ЕИС (Россия): {kw.get('total', 0)} шт.</b> "
            f"(часть {kw.get('part')}/{kw.get('parts')}, последние {kw.get('days_back', days_back)} дн.)\n\n"
        )

    chunks = build_telegram_chunks(rc, mention, fresh, tmpl_single=tmpl_single, tmpl_part=tmpl_part)
    telegram_warmup(token)
    saved = 0
    ok_all = True
    for text, ids in chunks:
        if send_telegram(token, chat, text, retries):
            for tid in ids:
                if tid:
                    save_sent_id(sent_path, tid)
                    saved += 1
        else:
            ok_all = False
            break
    append_transfer_journal(
        script_dir,
        "zakupki_equipment_cycle_summary",
        new_candidates=len(fresh),
        telegram_ok=ok_all,
        telegram_ids_saved=saved,
    )
    print(f"Записано id: {saved}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
