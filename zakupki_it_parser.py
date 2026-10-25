"""
Парсер zakupki.gov.ru (44-ФЗ и 223-ФЗ) -> Telegram.

Ключевые слова и чёрный список — те же, что у белорусских ИТ-тендеров (it_parser.py).
Чат отдельный: ZAKUPKI_TELEGRAM_CHAT_ID (группа FinVat Sales/IT Россия).
"""
from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import urllib3
from bs4 import BeautifulSoup

import it_parser
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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SEARCH_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"
BASE = "https://zakupki.gov.ru"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def _curl(url: str, timeout: float, cookie_file: str) -> str:
    sec = str(max(10, int(timeout)))
    fetched = subprocess.run(
        [
            "curl",
            "-sk",
            "--max-time",
            sec,
            "-A",
            UA,
            "-e",
            BASE + "/",
            "-b",
            cookie_file,
            "-c",
            cookie_file,
            "-o",
            "-",
            "-w",
            "\n__HTTP_CODE__:%{http_code}",
            url,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout + 15,
        check=False,
    )
    if fetched.returncode != 0:
        raise RuntimeError((fetched.stderr or "").strip() or f"curl exit {fetched.returncode}")
    raw = fetched.stdout or ""
    if "\n__HTTP_CODE__:" in raw:
        html, _, code_s = raw.rpartition("\n__HTTP_CODE__:")
        code = int(code_s.strip() or "0")
    else:
        html, code = raw, 200
    if code >= 400:
        raise RuntimeError(f"HTTP {code}")
    if "search-registry-entry-block" not in html and len(html) < 2000:
        raise RuntimeError("пустой или чужой HTML ЕИС")
    return html


def _search_url(date_from: str, date_to: str, page: int) -> str:
    params = {
        "morphology": "on",
        "search-filter": "Дате размещения",
        "pageNumber": str(page),
        "sortDirection": "false",
        "recordsPerPage": "_50",
        "showLotsInfoHidden": "false",
        "sortBy": "UPDATE_DATE",
        "fz44": "on",
        "fz223": "on",
        "af": "on",
        "currencyIdGeneral": "-1",
        "publishDateFrom": date_from,
        "publishDateTo": date_to,
    }
    return SEARCH_URL + "?" + urlencode(params)


def _block_labeled(block, title: str) -> str:
    for part in block.select(".registry-entry__body-block"):
        label = part.select_one(".registry-entry__body-title")
        if not label or title not in label.get_text(" ", strip=True):
            continue
        value = part.select_one(".registry-entry__body-value, .registry-entry__body-href, a")
        if value:
            return value.get_text(" ", strip=True)
    return ""


def _price_and_deadline(block) -> tuple[str, str]:
    right = block.select_one(".registry-entry__right-block")
    if not right:
        return "—", "—"
    lines = [ln.strip() for ln in right.get_text("\n", strip=True).splitlines() if ln.strip()]
    price = "—"
    deadline = "—"
    for i, line in enumerate(lines):
        if line.startswith("Начальная цена") and i + 1 < len(lines):
            price = lines[i + 1]
        if "Окончание подачи" in line and i + 1 < len(lines):
            deadline = lines[i + 1]
    return price, deadline


def parse_page(html: str) -> tuple[list[dict], int]:
    soup = BeautifulSoup(html, "html.parser")
    found = []
    blocks = soup.select("div.search-registry-entry-block")
    roots = tuple(it_parser.KEYWORDS_ROOTS)
    blacklist = tuple(it_parser.BLACKLIST)
    for block in blocks:
        link = block.select_one("a[href*='regNumber=']")
        href = (link.get("href") if link else "") or ""
        if "notice/" not in href or "printForm" in href:
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
        if is_blacklisted(haystack, blacklist):
            continue
        price, deadline = _price_and_deadline(block)
        url = href if href.startswith("http") else BASE + href
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
    token = _env_first("ZAKUPKI_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN") or _DEFAULT_BOT_TOKEN_FALLBACK
    chat = _env_first("ZAKUPKI_TELEGRAM_CHAT_ID")
    mention = os.environ.get("ZAKUPKI_TELEGRAM_MENTION", "").strip() or resolve_mention(it_parser.IT_PROFILE)
    days_back = _int_env("ZAKUPKI_DAYS_BACK", 7)
    max_pages = _int_env("ZAKUPKI_MAX_PAGES", 15)
    retries = _int_env("TELEGRAM_SEND_RETRIES", 6)
    timeout = float(os.environ.get("ZAKUPKI_HTTP_TIMEOUT", "40") or "40")

    log_path = os.path.join(script_dir, "zakupki_it_parser_log.txt")
    if os.path.exists(log_path):
        os.remove(log_path)
    log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
    sys.stdout = icetrade_tee(sys.stdout, log_handle)
    sys.stderr = icetrade_tee(sys.stderr, log_handle)

    if not chat:
        print("❌ Задайте ZAKUPKI_TELEGRAM_CHAT_ID (группа FinVat Sales/IT Россия).")
        return 1

    today = datetime.now()
    date_to = today.strftime("%d.%m.%Y")
    date_from = (today - timedelta(days=days_back)).strftime("%d.%m.%Y")
    sent_path = os.path.join(script_dir, "sent_zakupki_it_ids.txt")
    sent = load_sent_ids(sent_path)
    cookie = os.path.join(script_dir, "logs", "zakupki.cookies")
    os.makedirs(os.path.dirname(cookie), exist_ok=True)

    print("Парсер ИТ-тендеров ЕИС (zakupki.gov.ru) запущен")
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
        append_transfer_journal(script_dir, "zakupki_cycle_summary", new_candidates=0, telegram_ok=True)
        print("Новых ИТ-закупок нет.")
        return 0

    rc = RunnerConfig(
        profile=it_parser.IT_PROFILE,
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
            f"📋 <b>Новые ИТ-тендеры ЕИС (Россия) за последние {kw.get('days_back', days_back)} дней: "
            f"{kw.get('total', 0)}</b>\n\n"
        )

    def tmpl_part(**kw: object) -> str:
        return (
            f"{kw.get('mention', mention)}\n"
            f"📋 <b>Новые ИТ-тендеры ЕИС (Россия): {kw.get('total', 0)} шт.</b> "
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
        "zakupki_cycle_summary",
        new_candidates=len(fresh),
        telegram_ok=ok_all,
        telegram_ids_saved=saved,
    )
    print(f"Записано id: {saved}")
    return 0 if ok_all else 1


def icetrade_tee(orig, log_handle):
    from icetrade_parser_core import Tee

    return Tee(orig, log_handle)


if __name__ == "__main__":
    raise SystemExit(main())
