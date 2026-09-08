"""
Ядро парсера ЕИС zakupki.gov.ru → Telegram.

Отдельный источник (не профиль icetrade): другая разметка, реестровые номера, антибот.
Переиспользуем отправку в Telegram, семантику и дедуп из icetrade_parser_core.
"""
from __future__ import annotations

import html
import os
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlencode, urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

from icetrade_parser_core import (
    HEADER_RESERVE_DEFAULT,
    Tee,
    _load_dotenv_if_present,
    append_transfer_journal,
    get_date_range,
    is_blacklisted,
    load_sent_ids,
    matches_roots,
    save_sent_id,
    send_telegram,
    telegram_warmup,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://zakupki.gov.ru"
SEARCH_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://zakupki.gov.ru/epz/order/extendedsearch/search.html",
    "Upgrade-Insecure-Requests": "1",
}

DEFAULT_SEARCH_QUERIES = (
    "разработка программ",
    "программное обеспечение",
    "информационная система",
    "информационн технолог",
    "автоматизированная система",
)

REG_NUM_RE = re.compile(r"(?:№\s*)?(\d{18,19})")
REG_IN_URL_RE = re.compile(r"regNumber=(\d{18,19})", re.I)

_http_session: requests.Session | None = None


@dataclass(frozen=True)
class ZakupkiParserProfile:
    id: str
    keywords_roots: tuple[str, ...]
    blacklist: tuple[str, ...]
    sent_ids_filename: str
    log_filename: str
    default_mention: str
    tmpl_empty_ok: str
    tmpl_chunk_header_single: str
    tmpl_chunk_header_part: str
    tmpl_run_title: str
    tmpl_done_count_label: str
    telegram_mention_env: str
    blacklist_soft: tuple[str, ...] = ()
    keywords_strong_roots: tuple[str, ...] = ()


@dataclass
class RunnerConfig:
    profile: ZakupkiParserProfile
    script_dir: str
    bot_token: str
    chat_id: str
    days_back: int
    max_pages: int
    telegram_send_retries: int
    telegram_safe_text_limit: int
    header_reserve: int
    search_queries: tuple[str, ...]
    records_per_page: int


class ZakupkiFetchLock:
    """File-lock: один процесс ходит на ЕИС."""

    def __init__(self, script_dir: str, profile_id: str):
        self.path = os.path.join(script_dir, "logs", "zakupki_fetch.lock")
        self.profile_id = profile_id
        self._fh = None

    def acquire(self, wait_sec: float = 900.0) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                self._fh = os.fdopen(fd, "w", encoding="utf-8")
                self._fh.write(f"{os.getpid()}\t{self.profile_id}\t{_utc_ts()}\n")
                self._fh.flush()
                return True
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                    if age > 45 * 60:
                        print(f"  ⚠️ Снимаем протухший lock ({age / 60:.0f} мин): {self.path}")
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                print("  ⏳ Ждём освобождения zakupki lock…")
                time.sleep(15 + random.uniform(0, 5))
            except OSError as e:
                print(f"  ⚠️ Lock недоступен ({e}), продолжаем без блокировки")
                return True
        print("  ❌ Не дождались zakupki lock")
        return False

    def release(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
        except OSError:
            pass
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except OSError:
            pass


def _utc_ts() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_first(*names: str) -> str:
    for name in names:
        v = os.environ.get(name)
        if v and str(v).strip():
            return str(v).strip()
    return ""


def resolve_bot_token() -> str:
    return _env_first("ZAKUPKI_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN")


def resolve_chat_id() -> str:
    return _env_first("ZAKUPKI_TELEGRAM_CHAT_ID")


def resolve_mention(profile: ZakupkiParserProfile) -> str:
    return (
        os.environ.get(profile.telegram_mention_env, "").strip() or profile.default_mention
    ).strip()


def resolve_search_queries() -> tuple[str, ...]:
    raw = os.environ.get("ZAKUPKI_SEARCH_QUERIES", "").strip()
    if not raw:
        return DEFAULT_SEARCH_QUERIES
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    return tuple(parts) if parts else DEFAULT_SEARCH_QUERIES


def _request_timeout() -> float:
    try:
        return float(os.environ.get("ZAKUPKI_HTTP_TIMEOUT", "45"))
    except ValueError:
        return 45.0


def _request_retries() -> int:
    try:
        return max(1, int(os.environ.get("ZAKUPKI_HTTP_RETRIES", "4")))
    except ValueError:
        return 4


def _records_per_page() -> int:
    try:
        n = int(os.environ.get("ZAKUPKI_ON_PAGE", "50"))
    except ValueError:
        n = 50
    return min(50, max(10, n))


def _norm_text(el) -> str:
    if el is None:
        return ""
    return " ".join(el.get_text(" ", strip=True).split())


def extract_tender_id(url: str | None, registry: str | None = None) -> str | None:
    if registry:
        m = REG_NUM_RE.search(str(registry))
        if m:
            return m.group(1)
    if not url:
        return None
    m = REG_IN_URL_RE.search(url)
    if m:
        return m.group(1)
    m = REG_NUM_RE.search(url)
    return m.group(1) if m else None


def absolute_url(href: str | None) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    return urljoin(BASE_URL + "/", href.lstrip("/"))


def format_zakupki_price(price_str: str) -> str:
    raw = (price_str or "").strip().replace("\u00a0", " ").replace("\u202f", " ")
    if not raw or raw in ("—", "-", "–"):
        return "—"
    return " ".join(raw.split())


def format_tender_block(index: int, t: dict) -> str:
    title = html.escape(t.get("title") or "Без названия", quote=False)
    customer = html.escape(t.get("customer") or "Не указан", quote=False)
    law = html.escape(t.get("law") or "", quote=False)
    price_fmt = html.escape(format_zakupki_price(t.get("price") or ""), quote=False)
    date_end = html.escape(t.get("date_end") or "—", quote=False)
    url = t.get("url") or ""
    tid = html.escape(t.get("registry") or extract_tender_id(url) or "ссылка", quote=False)
    law_line = f"   ⚖ {law}\n" if law else ""
    link = f'<a href="{html.escape(url, quote=True)}">№ {tid}</a>' if url else f"№ {tid}"
    return (
        f"{index}. <b>{title}</b>\n"
        f"   🏢 {customer}\n"
        f"   💰 {price_fmt}\n"
        f"{law_line}"
        f"   📅 Приём до: {date_end}\n"
        f"   🔗 {link}\n\n"
    )


def build_telegram_chunks(
    rc: RunnerConfig,
    mention: str,
    all_new_tenders: list[dict],
    *,
    tmpl_single: Callable[..., str],
    tmpl_part: Callable[..., str],
) -> list[tuple[str, list[str | None]]]:
    total = len(all_new_tenders)
    blocks = []
    for i, t in enumerate(all_new_tenders, 1):
        tid = extract_tender_id(t.get("url"), t.get("registry"))
        blocks.append({"text": format_tender_block(i, t), "id": tid})

    limit_body = rc.telegram_safe_text_limit - rc.header_reserve
    chunk_block_lists: list[list[dict]] = []
    current: list[dict] = []
    current_len = 0
    for b in blocks:
        piece_len = len(b["text"])
        if current and current_len + piece_len > limit_body:
            chunk_block_lists.append(current)
            current = []
            current_len = 0
        current.append(b)
        current_len += piece_len
    if current:
        chunk_block_lists.append(current)

    parts_n = len(chunk_block_lists)
    out: list[tuple[str, list[str | None]]] = []
    for part_i, blist in enumerate(chunk_block_lists, 1):
        if parts_n == 1:
            header = tmpl_single(mention=mention, days_back=rc.days_back, total=total)
        else:
            header = tmpl_part(
                mention=mention,
                days_back=rc.days_back,
                total=total,
                part=part_i,
                parts=parts_n,
            )
        body = "".join(x["text"] for x in blist)
        ids = [x["id"] for x in blist if x["id"]]
        out.append((header + body, ids))
    return out


def build_search_params(
    query: str,
    page_num: int,
    date_from: str,
    date_to: str,
    records_per_page: int,
) -> dict[str, str]:
    per = f"_{records_per_page}"
    return {
        "searchString": query,
        "morphology": "on",
        "search-filter": "Дате размещения",
        "pageNumber": str(page_num),
        "sortDirection": "false",
        "recordsPerPage": per,
        "showLotsInfoHidden": "false",
        "sortBy": "UPDATE_DATE",
        "fz44": "on",
        "fz223": "on",
        "af": "on",
        "currencyIdGeneral": "-1",
        "publishDateFrom": date_from,
        "publishDateTo": date_to,
    }


def _search_url_with_params(params: dict[str, str]) -> str:
    return SEARCH_URL + "?" + urlencode(params, doseq=True)


def get_http_session() -> requests.Session:
    global _http_session
    if _http_session is not None:
        return _http_session
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = False
    try:
        s.get(BASE_URL + "/", timeout=_request_timeout())
    except Exception as e:
        print(f"  ⚠️ Прогрев {BASE_URL}/ не удался: {e}")
    _http_session = s
    return s


def _response_looks_blocked(status_code: int, text: str) -> str | None:
    low = (text or "").lower()
    if status_code == 403:
        return "HTTP 403 Forbidden"
    if status_code == 429:
        return "HTTP 429 Too Many Requests"
    if status_code >= 500:
        return f"HTTP {status_code}"
    if "captcha" in low or "я не робот" in low:
        return "CAPTCHA / антибот"
    if "access denied" in low or "доступ запрещ" in low or "доступ ограничен" in low:
        return "страница Access denied"
    if "cf-browser-verification" in low or "just a moment" in low:
        return "Cloudflare challenge"
    has_cards = (
        "search-registry-entry-block" in low
        or "registry-entry__header" in low
        or "по вашему запросу ничего не найдено" in low
        or "ничего не найдено" in low
    )
    if not has_cards and (len(text) < 4000 or "notfound" in low):
        return "нет карточек ЕИС в HTML (похоже на блок/заглушку)"
    return None


def _fetch_html_powershell(url: str, timeout: float) -> str:
    import subprocess

    sec = max(1, int(timeout))
    escaped = url.replace("'", "''")
    cmd = f"(Invoke-WebRequest -Uri '{escaped}' -UseBasicParsing -TimeoutSec {sec}).Content"
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout + 15,
        check=False,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        raise RuntimeError(err)
    html_text = r.stdout or ""
    if len(html_text) < 100:
        raise RuntimeError("empty HTML from PowerShell")
    return html_text


def _get_page_powershell_fallback(page_num: int, params: dict[str, str], timeout: float) -> BeautifulSoup | None:
    if os.environ.get("ZAKUPKI_NO_POWERSHELL_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    if sys.platform != "win32":
        return None
    url = _search_url_with_params(params)
    try:
        html_text = _fetch_html_powershell(url, timeout)
        blocked = _response_looks_blocked(200, html_text)
        if blocked:
            print(f"  ❌ PowerShell fallback стр. {page_num}: {blocked}")
            return None
        print(f"  стр. {page_num} загружена (powershell)")
        return BeautifulSoup(html_text, "html.parser")
    except Exception as e:
        print(f"  ❌ PowerShell fallback стр. {page_num}: {e}")
        return None


def get_page(params: dict[str, str], page_num: int) -> BeautifulSoup | None:
    timeout = _request_timeout()
    retries = _request_retries()
    session = get_http_session()
    url = _search_url_with_params(params)
    last_err = ""
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=timeout)
            blocked = _response_looks_blocked(r.status_code, r.text)
            if blocked:
                last_err = blocked
                print(f"  ⚠️ стр. {page_num} попытка {attempt + 1}/{retries}: {blocked}")
                if attempt + 1 < retries:
                    time.sleep(4 + attempt * 3 + random.uniform(0, 1.5))
                    continue
                return _get_page_powershell_fallback(page_num, params, timeout)
            print(f"  стр. {page_num} загружена ({len(r.text)} байт)")
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"  ⚠️ стр. {page_num} попытка {attempt + 1}/{retries}: {last_err}")
            if attempt + 1 < retries:
                time.sleep(3 + attempt * 2 + random.uniform(0, 1))
                continue
            fb = _get_page_powershell_fallback(page_num, params, timeout)
            if fb is not None:
                return fb
            return None
    print(f"  ❌ Ошибка загрузки страницы {page_num}: {last_err or 'unknown'}")
    return _get_page_powershell_fallback(page_num, params, timeout)


def _select_cards(soup: BeautifulSoup):
    cards = soup.select(".search-registry-entry-block")
    if cards:
        return cards
    return soup.select(".registry-entry__form, div.registry-entry-block")


def _parse_dates(card) -> tuple[str, str]:
    date_pub = ""
    date_end = ""
    block = card.select_one(".data-block")
    if not block:
        vals = [_norm_text(x) for x in card.select(".data-block__value")]
        if vals:
            date_pub = vals[0]
            date_end = vals[-1]
        return date_pub, date_end
    titles = block.select(".data-block__title")
    values = block.select(".data-block__value")
    pairs = []
    for i, title_el in enumerate(titles):
        val = _norm_text(values[i]) if i < len(values) else ""
        pairs.append((_norm_text(title_el).lower(), val))
    for title, val in pairs:
        if "окончан" in title or "подач" in title:
            date_end = val
        elif "размещ" in title and not date_pub:
            date_pub = val
    if not date_end and values:
        date_end = _norm_text(values[-1])
    return date_pub, date_end


def parse_card(card) -> dict | None:
    num_a = card.select_one(".registry-entry__header-mid__number a, a[href*='regNumber=']")
    href = ""
    registry = ""
    if num_a:
        href = absolute_url(num_a.get("href"))
        registry = extract_tender_id(href, _norm_text(num_a)) or ""
    if not registry:
        raw_num = _norm_text(card.select_one(".registry-entry__header-mid__number"))
        registry = extract_tender_id(href, raw_num) or ""
    if not registry:
        return None
    if not href:
        qs = urlencode({"regNumber": registry})
        href = f"{BASE_URL}/epz/order/notice/ea44/view/common-info.html?{qs}"

    title_el = card.select_one(
        ".registry-entry__body-value.registry-entry__body-value-height, "
        ".registry-entry__body-value"
    )
    title = _norm_text(title_el)
    if not title:
        title = _norm_text(card.select_one(".registry-entry__body"))

    customer_el = card.select_one(".registry-entry__body-href a, .registry-entry__body-href")
    customer = _norm_text(customer_el) or "Не указан"

    price = _norm_text(card.select_one(".price-block__value, .price-block .price"))
    law = _norm_text(
        card.select_one(".registry-entry__header-top__title, .col-9.registry-entry__header-mid__title")
    )
    if not law:
        law = _norm_text(card.select_one(".registry-entry__header-top"))
    date_pub, date_end = _parse_dates(card)

    return {
        "title": title,
        "customer": customer,
        "price": price,
        "date_end": date_end or date_pub or "—",
        "date_pub": date_pub,
        "url": href,
        "registry": registry,
        "law": law,
    }


def parse_tenders(soup: BeautifulSoup, profile: ZakupkiParserProfile) -> tuple[list[dict], int]:
    """Вернуть (подходящие тендеры, число карточек на странице)."""
    tenders = []
    cards = _select_cards(soup)
    for card in cards:
        t = parse_card(card)
        if not t:
            continue
        hay = f"{t['title']} {t['customer']}"
        if not matches_roots(hay, profile.keywords_roots):
            continue
        if is_blacklisted(
            hay,
            profile.blacklist,
            blacklist_soft=profile.blacklist_soft,
            keywords_strong_roots=profile.keywords_strong_roots,
        ):
            print(f"   ⛔ Исключён по чёрному списку: {t['title'][:60]}")
            continue
        tenders.append(t)
    return tenders, len(cards)


def run_parser_cycle(
    rc: RunnerConfig,
    mention: str,
    *,
    tmpl_empty: Callable[..., str],
    tmpl_single: Callable[..., str],
    tmpl_part: Callable[..., str],
) -> bool:
    profile = rc.profile
    try:
        stagger = float(os.environ.get("ZAKUPKI_START_STAGGER_SEC", "0") or "0")
    except ValueError:
        stagger = 0.0
    if stagger <= 0:
        stagger = random.uniform(0.5, 6.0)
    print(f"⏳ Пауза старта {stagger:.1f}с (антибот ЕИС)")
    time.sleep(stagger)

    fetch_lock = ZakupkiFetchLock(rc.script_dir, profile.id)
    if not fetch_lock.acquire():
        err_msg = (
            f"⚠️ {mention}\n"
            f"<b>Парсер [{profile.id}] не запустился</b>\n"
            f"Другой процесс ещё ходит на zakupki.gov.ru (lock timeout)."
        )
        telegram_warmup(rc.bot_token)
        send_telegram(rc.bot_token, rc.chat_id, err_msg, rc.telegram_send_retries)
        return False
    try:
        return _run_parser_cycle_locked(
            rc, mention, tmpl_empty=tmpl_empty, tmpl_single=tmpl_single, tmpl_part=tmpl_part
        )
    finally:
        fetch_lock.release()


def _run_parser_cycle_locked(
    rc: RunnerConfig,
    mention: str,
    *,
    tmpl_empty: Callable[..., str],
    tmpl_single: Callable[..., str],
    tmpl_part: Callable[..., str],
) -> bool:
    cf, ct = get_date_range(rc.days_back)
    sent_path = os.path.join(rc.script_dir, rc.profile.sent_ids_filename)
    profile = rc.profile

    print(f"🚀 {profile.tmpl_run_title}")
    print(f"📅 Диапазон размещения: {cf} - {ct} (последние {rc.days_back} дней)")
    print(f"🔎 Запросы: {len(rc.search_queries)} × до {rc.max_pages} стр.")
    print(f"💾 Отправленные ID: {sent_path}")

    sent_ids = load_sent_ids(sent_path)
    print(f"📦 Уже отправлено тендеров: {len(sent_ids)}")

    all_new_tenders: list[dict] = []
    seen_in_session: set[str] = set()
    stopped_early_bad_page = False
    pages_ok = 0
    try:
        empty_stop = int(os.environ.get("ZAKUPKI_EMPTY_PAGE_STOP", "5"))
    except ValueError:
        empty_stop = 5

    for qi, query in enumerate(rc.search_queries, 1):
        print(f"\n===== Запрос {qi}/{len(rc.search_queries)}: «{query}» =====")
        empty_streak = 0
        for page in range(1, rc.max_pages + 1):
            print(f"\n--- «{query}» стр. {page} ---")
            params = build_search_params(query, page, cf, ct, rc.records_per_page)
            soup = get_page(params, page)
            if not soup:
                print("❌ ошибка загрузки, прерываем этот запрос")
                stopped_early_bad_page = True
                break

            pages_ok += 1
            tenders, n_cards = parse_tenders(soup, profile)
            if n_cards == 0:
                print("  ⏹ Карточек нет — конец выдачи или пустой ответ")
                break

            new_on_page = 0
            for t in tenders:
                tender_id = extract_tender_id(t.get("url"), t.get("registry"))
                if not tender_id:
                    continue
                if tender_id in sent_ids or tender_id in seen_in_session:
                    continue
                seen_in_session.add(tender_id)
                all_new_tenders.append(t)
                new_on_page += 1
                print(f"  ✅ НОВЫЙ: {tender_id} - {t['title'][:50]}")
            print(f"  Карточек: {n_cards}, новых после фильтра: {new_on_page}")

            if new_on_page == 0:
                empty_streak += 1
            else:
                empty_streak = 0
            if empty_stop > 0 and empty_streak >= empty_stop:
                print(
                    f"⏹ Стоп запроса: {empty_streak} стр. подряд без новых "
                    f"(ZAKUPKI_EMPTY_PAGE_STOP={empty_stop})"
                )
                break
            time.sleep(random.uniform(3.5, 6.0))

    xfer_new = len(all_new_tenders)
    print(f"\n📊 ИТОГО новых ({profile.tmpl_done_count_label}): {xfer_new}")

    if stopped_early_bad_page and pages_ok == 0:
        err_msg = (
            f"⚠️ {mention}\n"
            f"<b>Парсер [{profile.id}] не смог загрузить zakupki.gov.ru</b>\n"
            f"Ошибка сети/доступа (часто HTTP 403/429 / CAPTCHA). Лоты не проверялись."
        )
        telegram_warmup(rc.bot_token)
        send_telegram(rc.bot_token, rc.chat_id, err_msg, rc.telegram_send_retries)
        append_transfer_journal(
            rc.script_dir,
            "parser_cycle_summary",
            profile=profile.id,
            new_candidates=0,
            stopped_early_bad_page=True,
            pages_ok=0,
            telegram_ok=False,
            fetch_failed=True,
        )
        print("❌ Цикл прерван: ЕИС недоступна с первой страницы.")
        return False

    telegram_warmup(rc.bot_token)
    xfer_chunks_total: int | None = None
    xfer_saved_ids = 0
    xfer_delivery_ok = False

    if not all_new_tenders:
        note = ""
        if stopped_early_bad_page:
            note = (
                f"\n⚠️ Загрузка оборвалась после стр. {pages_ok} (сеть/403). "
                f"Ниже — только то, что успели проверить."
            )
        msg = tmpl_empty(mention=mention, days_back=rc.days_back) + note
        xfer_chunks_total = 1
        xfer_delivery_ok = send_telegram(rc.bot_token, rc.chat_id, msg, rc.telegram_send_retries)
        append_transfer_journal(
            rc.script_dir,
            "telegram_delivery",
            profile=profile.id,
            mode="empty",
            ok=xfer_delivery_ok,
            tg_chars=len(msg),
            stopped_early_bad_page=stopped_early_bad_page,
            pages_ok=pages_ok,
        )
        if xfer_delivery_ok:
            print("📭 Сообщение в Telegram доставлено (тендеров нет)")
        else:
            print("❌ Не удалось отправить сообщение в Telegram (тендеров нет). sent_* не менялся.")
    else:
        all_new_tenders.sort(key=lambda x: x.get("date_end") or "", reverse=False)
        chunks = build_telegram_chunks(
            rc, mention, all_new_tenders, tmpl_single=tmpl_single, tmpl_part=tmpl_part
        )
        xfer_chunks_total = len(chunks)
        if xfer_chunks_total == 0:
            print("⚠️ Нечего отправлять: чанков Telegram для новых лотов не сформировано.")
            xfer_delivery_ok = False
        else:
            for idx, (text, ids_in_chunk) in enumerate(chunks):
                ok_chunk = send_telegram(rc.bot_token, rc.chat_id, text, rc.telegram_send_retries)
                n_written_slot = sum(1 for x in ids_in_chunk if x)
                append_transfer_journal(
                    rc.script_dir,
                    "telegram_delivery",
                    profile=profile.id,
                    mode="chunk",
                    part=f"{idx + 1}/{len(chunks)}",
                    ok=ok_chunk,
                    tg_chars=len(text),
                    tg_new_ids_written=n_written_slot if ok_chunk else 0,
                )
                if not ok_chunk:
                    print(
                        f"❌ Часть {idx + 1}/{len(chunks)} не отправлена — остановка без записи "
                        "этой и следующих частей; повторите запуск позже."
                    )
                    xfer_delivery_ok = False
                    break
                for tid in ids_in_chunk:
                    if tid:
                        save_sent_id(sent_path, tid)
                        xfer_saved_ids += 1
            else:
                xfer_delivery_ok = True
        print(
            f"✅ Записано новых ID в {profile.sent_ids_filename}: {xfer_saved_ids} "
            f"(из {xfer_new} найденных)"
        )

    append_transfer_journal(
        rc.script_dir,
        "parser_cycle_summary",
        profile=profile.id,
        new_candidates=xfer_new,
        stopped_early_bad_page=stopped_early_bad_page,
        pages_ok=pages_ok,
        telegram_ok=xfer_delivery_ok,
        telegram_chunks=xfer_chunks_total if xfer_chunks_total is not None else 0,
        telegram_ids_saved=xfer_saved_ids,
        days_back=rc.days_back,
        max_pages=rc.max_pages,
    )
    return not stopped_early_bad_page or xfer_new > 0


def cli_main(profile: ZakupkiParserProfile) -> None:
    global _http_session
    script_dir = os.path.dirname(os.path.abspath(__file__))
    _load_dotenv_if_present(script_dir)
    _http_session = None

    mention = resolve_mention(profile)
    bot = resolve_bot_token()
    chat = resolve_chat_id()
    if not chat:
        print(
            "❌ Для профиля zakupki задайте ZAKUPKI_TELEGRAM_CHAT_ID "
            "(числовой id группы; бот должен быть участником). "
            "Ссылка-приглашение t.me/+… не подходит как chat_id."
        )
        sys.exit(1)
    if not bot:
        print("❌ Задайте ZAKUPKI_TELEGRAM_BOT_TOKEN или TELEGRAM_BOT_TOKEN.")
        sys.exit(1)

    try:
        days_back = int(os.environ.get("ZAKUPKI_DAYS_BACK") or os.environ.get("DAYS_BACK", "30"))
    except ValueError:
        days_back = 30
    try:
        max_pages = int(os.environ.get("ZAKUPKI_MAX_PAGES", "20"))
    except ValueError:
        max_pages = 20
    retries = int(os.environ.get("TELEGRAM_SEND_RETRIES", "6"))
    tel_limit = int(os.environ.get("TELEGRAM_SAFE_TEXT_LIMIT", "3800"))

    log_path = os.path.join(script_dir, profile.log_filename)
    if os.path.exists(log_path):
        os.remove(log_path)
    orig_out = sys.stdout
    orig_err = sys.stderr
    log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
    sys.stdout = Tee(orig_out, log_handle)
    sys.stderr = Tee(orig_err, log_handle)

    rc = RunnerConfig(
        profile=profile,
        script_dir=script_dir,
        bot_token=bot,
        chat_id=chat,
        days_back=days_back,
        max_pages=max_pages,
        telegram_send_retries=retries,
        telegram_safe_text_limit=tel_limit,
        header_reserve=HEADER_RESERVE_DEFAULT,
        search_queries=resolve_search_queries(),
        records_per_page=_records_per_page(),
    )

    def tmpl_empty(**kw: object) -> str:
        md = kw.get("mention", mention)
        db = kw.get("days_back", days_back)
        return profile.tmpl_empty_ok.format(mention=md, days_back=int(db))

    def tmpl_single(**kwargs: object) -> str:
        m = kwargs.get("mention", mention)
        db = kwargs.get("days_back", days_back)
        total = kwargs.get("total")
        return profile.tmpl_chunk_header_single.format(mention=m, days_back=db, total=total)

    def tmpl_part(**kwargs: object) -> str:
        m = kwargs.get("mention", mention)
        db = kwargs.get("days_back", days_back)
        total = kwargs.get("total")
        part = kwargs.get("part")
        parts = kwargs.get("parts")
        return profile.tmpl_chunk_header_part.format(
            mention=m, days_back=db, total=total, part=part, parts=parts
        )

    try:
        try:
            ok = run_parser_cycle(
                rc, mention, tmpl_empty=tmpl_empty, tmpl_single=tmpl_single, tmpl_part=tmpl_part
            )
        except Exception:
            print("\n❌ КРИТИЧЕСКАЯ ОШИБКА:")
            print(traceback.format_exc())
            ok = False
        print("\n✅ Готово!" if ok else "\n❌ Завершено с ошибкой загрузки zakupki.gov.ru.")
        if not ok:
            sys.exit(1)
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        sys.stdout = orig_out
        sys.stderr = orig_err
        log_handle.close()
