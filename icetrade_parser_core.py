"""
Общее ядро парсера icetrade.by → Telegram (ИТ-профиль и профиль ангаров).

ИТ-профиль: по умолчанию задаётся рубрикатор отраслей ИТ на icetrade; переопределение через JSON/env
(как в it_parser.py).

Профиль ангаров (id «angar»): рубрикатор по умолчанию не подставляется — запрос идёт без industries,
список тендеров сужается только ключевыми словами и чёрным списком. Позже рубрикатор можно включить
через ANGAR_ICETRADE_INDUSTRIES или icetrade_industry_params_angar.json.

Изначально: логика it_parser.py + совместимые параметры формы запроса (исправление устаревшего ParserAngar).
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _configure_stdio_utf8_windows():
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, AttributeError, ValueError):
                pass


_configure_stdio_utf8_windows()

_DEFAULT_BOT_TOKEN_FALLBACK = "8677357886:AAHWAq-EfNxlcR7XQaz8es5eGnXriUNSfGk"
_DEFAULT_CHAT_ID_FALLBACK = "-1001872277668"

BASE_URL = "https://icetrade.by"
SEARCH_URL = "https://icetrade.by/search/auctions"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# По умолчанию для профиля it (рабочая строка с icetrade; совпадает с прежним it_parser.py)
DEFAULT_IT_INDUSTRIES = "16/17/18/105.106-115/116.117-122/179/370.371-387"

_extra_params_cache_by_profile_id: dict[str, dict[str, object]] | None = None  # keyed by env path string
_logged_industry_mode: dict[str, bool] = {}


class Tee:
    """Дубль stdout/stderr в файл лога."""

    def __init__(self, *files):
        self.files = files

    def write(self, obj):
        for f in self.files:
            f.write(obj)
            f.flush()

    def flush(self):
        for f in self.files:
            f.flush()


@dataclass(frozen=True)
class IcetradeParserProfile:
    """Набор параметров одного профиля (ИТ или ангары и т.д.)."""

    id: str
    keywords_roots: tuple[str, ...]
    blacklist: tuple[str, ...]
    sent_ids_filename: str
    log_filename: str
    default_mention: str
    use_it_default_industries_if_missing: bool
    tmpl_empty_ok: str
    tmpl_chunk_header_single: str
    tmpl_chunk_header_part: str
    tmpl_run_title: str
    tmpl_done_count_label: str
    telegram_mention_env: str


def resolve_bot_token(profile: IcetradeParserProfile) -> str:
    if profile.id == "angar":
        v = (
            os.environ.get("ANGAR_TELEGRAM_BOT_TOKEN")
            or os.environ.get("TELEGRAM_BOT_TOKEN")
            or os.environ.get("BOT_TOKEN")
        )
    else:
        v = (
            os.environ.get("TELEGRAM_BOT_TOKEN")
            or os.environ.get("BOT_TOKEN")
        )
    return (v or _DEFAULT_BOT_TOKEN_FALLBACK).strip()


def resolve_chat_id(profile: IcetradeParserProfile) -> str:
    if profile.id == "angar":
        v = (
            os.environ.get("ANGAR_TELEGRAM_CHAT_ID")
            or os.environ.get("TELEGRAM_CHAT_ID")
            or os.environ.get("CHAT_ID")
        )
    else:
        v = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID")
    return (v or _DEFAULT_CHAT_ID_FALLBACK).strip()


def resolve_mention(profile: IcetradeParserProfile) -> str:
    return (
        os.environ.get(profile.telegram_mention_env, "").strip() or profile.default_mention
    ).strip()


@dataclass
class RunnerConfig:
    profile: IcetradeParserProfile
    script_dir: str
    bot_token: str
    chat_id: str
    days_back: int
    max_pages: int
    telegram_send_retries: int
    telegram_safe_text_limit: int
    header_reserve: int


HEADER_RESERVE_DEFAULT = 280


def get_date_range(days_back: int) -> tuple[str, str]:
    today = datetime.now().date()
    from_date = today - timedelta(days=days_back)
    return from_date.strftime("%d.%m.%Y"), today.strftime("%d.%m.%Y")


def _extra_params_json_path(profile: IcetradeParserProfile, script_dir: str) -> str:
    if profile.id == "angar":
        p = os.environ.get("ANGAR_ICETRADE_PARAMS_JSON")
        if p and p.strip():
            return os.path.abspath(p.strip())
        return os.path.join(script_dir, "icetrade_industry_params_angar.json")
    return os.environ.get(
        "ICETRADE_PARAMS_JSON", os.path.join(script_dir, "icetrade_industry_params.json")
    )


def load_icetrade_extra_params(profile: IcetradeParserProfile, script_dir: str) -> dict[str, object]:
    global _extra_params_cache_by_profile_id
    path = os.path.abspath(_extra_params_json_path(profile, script_dir))
    if _extra_params_cache_by_profile_id is None:
        _extra_params_cache_by_profile_id = {}
    if path in _extra_params_cache_by_profile_id:
        return _extra_params_cache_by_profile_id[path]  # type: ignore[return-value]

    out: dict[str, object] = {}
    if not os.path.isfile(path):
        _extra_params_cache_by_profile_id[path] = out
        return out
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"  ⚠️ Не удалось прочитать {path}: {e}")
        _extra_params_cache_by_profile_id[path] = out
        return out
    if not isinstance(raw, dict):
        _extra_params_cache_by_profile_id[path] = out
        return out
    for k, v in raw.items():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        if isinstance(v, (list, tuple)):
            out[k] = [str(x) for x in v]
        elif v is not None and str(v).strip() != "":
            out[k] = str(v)
    if out:
        print(f"  📎 icetrade [{profile.id}]: из {path} добавлено {len(out)} полей фильтра")
    _extra_params_cache_by_profile_id[path] = out
    return out


def icetrade_search_extra_params(profile: IcetradeParserProfile, script_dir: str) -> dict[str, object]:
    global _logged_industry_mode
    extra = dict(load_icetrade_extra_params(profile, script_dir))

    if profile.id == "it":
        ind_env = os.environ.get("ICETRADE_INDUSTRIES")
        env_key_logged = "it"
        disable_key = os.environ.get("ICETRADE_DISABLE_DEFAULT_INDUSTRY", "")
    else:
        ind_env = os.environ.get("ANGAR_ICETRADE_INDUSTRIES")
        env_key_logged = "angar"
        disable_key = os.environ.get("ANGAR_ICETRADE_DISABLE_DEFAULT_INDUSTRY", "")

    logged = _logged_industry_mode.setdefault(env_key_logged, False)

    if ind_env is not None:
        s = ind_env.strip()
        if s.lower() in ("", "none", "off", "0"):
            extra.pop("industries", None)
            if not logged:
                which = "ICETRADE_INDUSTRIES" if profile.id == "it" else "ANGAR_ICETRADE_INDUSTRIES"
                print(f"  📎 icetrade [{profile.id}]: industries отключены ({which} пусто/off)")
                _logged_industry_mode[env_key_logged] = True
        else:
            extra["industries"] = s
            if not logged:
                which = "ICETRADE_INDUSTRIES" if profile.id == "it" else "ANGAR_ICETRADE_INDUSTRIES"
                print(f"  📎 icetrade [{profile.id}]: industries из переменной {which}")
                _logged_industry_mode[env_key_logged] = True
    elif "industries" not in extra:
        if profile.use_it_default_industries_if_missing:
            if disable_key.lower() not in ("1", "true", "yes"):
                extra["industries"] = DEFAULT_IT_INDUSTRIES
                if not logged:
                    print(
                        "  📎 icetrade [it]: industries по умолчанию (ИТ + компьютеры); "
                        "отключить: ICETRADE_DISABLE_DEFAULT_INDUSTRY=1 или ICETRADE_INDUSTRIES=off"
                    )
                    _logged_industry_mode[env_key_logged] = True
        else:
            if not logged:
                print(
                    "  📎 icetrade [angar]: industries не заданы — поиск по всему icetrade.by, "
                    "отбор только ключевыми словами и чёрным списком. "
                    "Позже можно задать рубрикатор: ANGAR_ICETRADE_INDUSTRIES или icetrade_industry_params_angar.json."
                )
                _logged_industry_mode[env_key_logged] = True
    return extra


def build_base_search_params(created_from: str, created_to: str) -> dict[str, str]:
    return {
        "search_text": "",
        "sbm": "1",
        "zakup_type[1]": "1",
        "zakup_type[2]": "1",
        "auc_num": "",
        "okrb": "",
        "company_title": "",
        "establishment": "0",
        "period": "",
        "request_end_from": "",
        "request_end_to": "",
        "t[Trade]": "1",
        "t[eTrade]": "1",
        "t[socialOrder]": "1",
        "t[singleSource]": "1",
        "t[Auction]": "1",
        "t[Request]": "1",
        "t[contractingTrades]": "1",
        "t[negotiations]": "1",
        "t[Other]": "1",
        "r[1]": "1",
        "r[2]": "2",
        "r[7]": "7",
        "r[3]": "3",
        "r[4]": "4",
        "r[6]": "6",
        "r[5]": "5",
        "sort": "num:desc",
        "onPage": "20",
        "created_from": created_from,
        "created_to": created_to,
    }


def get_page(rc: RunnerConfig, page_num: int) -> BeautifulSoup | None:
    cf, ct = get_date_range(rc.days_back)
    base = build_base_search_params(cf, ct)
    extra = icetrade_search_extra_params(rc.profile, rc.script_dir)
    params: dict[str, object] = {**base, **extra, "p": page_num}
    try:
        r = requests.get(SEARCH_URL, headers=HEADERS, params=params, timeout=20, verify=False)
        r.raise_for_status()
        print(f"  стр. {page_num} загружена")
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  ❌ Ошибка загрузки страницы {page_num}: {e}")
        return None


def load_sent_ids(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_sent_id(path: str, tender_id: str):
    tid = str(tender_id).strip()
    if not tid:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{tid}\n")
        f.flush()
        os.fsync(f.fileno())


def extract_tender_id(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/view/(\d+)", url)
    return match.group(1) if match else None


def matches_roots(title: str, roots: tuple[str, ...]) -> bool:
    title_lower = title.lower()
    for root in roots:
        if root in title_lower:
            return True
    return False


def is_blacklisted(title: str, blacklist: tuple[str, ...]) -> bool:
    title_lower = title.lower()
    for word in blacklist:
        if word in title_lower:
            return True
    return False


def format_price(price_str: str) -> str:
    if not price_str or price_str == "—":
        return "—"
    match = re.search(r"([\d\s]+)\s*(BYN|руб|USD|EUR)", price_str, re.I)
    if not match:
        return price_str
    num_part = match.group(1).strip().replace(" ", "")
    currency = match.group(2).upper()
    try:
        num_int = int(num_part)
        formatted = f"{num_int:,}".replace(",", " ")
        return f"{formatted} {currency}"
    except Exception:
        return f"{num_part} {currency}"


def _telegram_send_once(bot_token: str, chat_id: str, text: str) -> tuple[bool, str]:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=data, timeout=30)
        if r.ok:
            return True, ""
        detail = r.text[:800]
        try:
            j = r.json()
            if isinstance(j, dict) and j.get("description"):
                detail = str(j.get("description"))
        except Exception:
            pass
        return False, f"HTTP {r.status_code}: {detail}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def send_telegram(bot_token: str, chat_id: str, text: str, retries: int) -> bool:
    backoff = [1, 2, 4, 8, 16, 32, 32]
    last_err = ""
    for attempt in range(retries):
        ok, err = _telegram_send_once(bot_token, chat_id, text)
        if ok:
            if attempt > 0:
                print(f"  ✅ Telegram: успех с попытки {attempt + 1}")
            return True
        last_err = err
        print(f"  ⚠️ Telegram попытка {attempt + 1}/{retries} не удалась: {err}")
        if attempt + 1 < retries:
            delay = backoff[min(attempt, len(backoff) - 1)]
            time.sleep(delay + random.uniform(0, 0.35))
    print(f"  ❌ Telegram: все попытки исчерпаны. Последняя ошибка: {last_err}")
    return False


def telegram_warmup(bot_token: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/getMe"
    try:
        r = requests.get(url, timeout=20)
        if not r.ok:
            print(f"  ⚠️ getMe не OK: HTTP {r.status_code}")
    except Exception as e:
        print(f"  ⚠️ getMe пропущен из-за ошибки: {e}")


def format_tender_block(index: int, t: dict) -> str:
    price_fmt = format_price(t["price"])
    return (
        f"{index}. <b>{t['title']}</b>\n"
        f"   🏢 {t['customer']}\n"
        f"   💰 {price_fmt}\n"
        f"   📅 Приём до: {t['date_end']}\n"
        f"   🔗 <a href=\"{t['url']}\">Ссылка</a>\n\n"
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
        tid = extract_tender_id(t.get("url"))
        blocks.append({"i": i, "t": t, "text": format_tender_block(i, t), "id": tid})

    limit_body = rc.telegram_safe_text_limit - rc.header_reserve
    chunk_block_lists = []
    current = []
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


def parse_tenders(soup: BeautifulSoup, keywords: tuple[str, ...], blacklist: tuple[str, ...]):
    tenders = []
    rows = soup.select("#auctions-list tr")
    if len(rows) <= 1:
        return tenders
    for row in rows[1:]:
        cols = row.find_all("td")
        if len(cols) < 6:
            continue
        link_tag = cols[0].find("a")
        if not link_tag:
            continue
        title = link_tag.get_text(strip=True)
        if not matches_roots(title, keywords):
            continue
        if is_blacklisted(title, blacklist):
            print(f"   ⛔ Исключён по чёрному списку: {title[:60]}")
            continue
        url = link_tag.get("href")
        if url and not url.startswith("http"):
            url = BASE_URL + url
        customer = cols[1].get_text(strip=True) or "Не указан"
        price_raw = cols[4].get_text(strip=True)
        date_end_raw = cols[5].get_text(strip=True)
        tenders.append(
            {
                "title": title,
                "customer": customer,
                "price": price_raw,
                "date_end": date_end_raw,
                "url": url,
            }
        )
    return tenders


def run_parser_cycle(
    rc: RunnerConfig,
    mention: str,
    *,
    tmpl_empty: Callable[..., str],
    tmpl_single: Callable[..., str],
    tmpl_part: Callable[..., str],
) -> None:
    cf, ct = get_date_range(rc.days_back)
    sent_path = os.path.join(rc.script_dir, rc.profile.sent_ids_filename)
    profile = rc.profile

    print(f"🚀 {profile.tmpl_run_title}")
    print(f"📅 Диапазон: {cf} - {ct} (последние {rc.days_back} дней)")
    print(f"📄 Максимум страниц: {rc.max_pages}")
    print(f"💾 Отправленные ID: {sent_path}")

    sent_ids = load_sent_ids(sent_path)
    print(f"📦 Уже отправлено тендеров: {len(sent_ids)}")

    all_new_tenders = []
    seen_in_session = set()

    for page in range(1, rc.max_pages + 1):
        print(f"\n--- Страница {page} ---")
        soup = get_page(rc, page)
        if not soup:
            print("❌ ошибка загрузки, прерываем")
            break

        tenders = parse_tenders(soup, profile.keywords_roots, profile.blacklist)
        if tenders:
            new_on_page = 0
            for t in tenders:
                tender_id = extract_tender_id(t["url"])
                if not tender_id:
                    continue
                if tender_id in sent_ids or tender_id in seen_in_session:
                    continue
                seen_in_session.add(tender_id)
                all_new_tenders.append(t)
                new_on_page += 1
                print(f"  ✅ НОВЫЙ: {tender_id} - {t['title'][:50]}")
            print(f"  Найдено новых: {new_on_page}")
        else:
            print("  ❌ Нет подходящих")

        time.sleep(random.uniform(1.5, 2.5))

    print(f"\n📊 ИТОГО новых ({profile.tmpl_done_count_label}): {len(all_new_tenders)}")

    telegram_warmup(rc.bot_token)

    if not all_new_tenders:
        msg = tmpl_empty(mention=mention, days_back=rc.days_back)
        if send_telegram(rc.bot_token, rc.chat_id, msg, rc.telegram_send_retries):
            print("📭 Сообщение в Telegram доставлено (тендеров нет)")
        else:
            print("❌ Не удалось отправить сообщение в Telegram (тендеров нет). sent_* не менялся.")
    else:
        all_new_tenders.sort(key=lambda x: x.get("date_end", ""), reverse=False)
        chunks = build_telegram_chunks(rc, mention, all_new_tenders, tmpl_single=tmpl_single, tmpl_part=tmpl_part)
        saved = 0
        for idx, (text, ids_in_chunk) in enumerate(chunks):
            if not send_telegram(rc.bot_token, rc.chat_id, text, rc.telegram_send_retries):
                print(
                    f"❌ Часть {idx + 1}/{len(chunks)} не отправлена — остановка без записи этой и следующих частей; "
                    "повторите запуск позже."
                )
                break
            for tid in ids_in_chunk:
                if tid:
                    save_sent_id(sent_path, tid)
                    saved += 1
        print(
            f"✅ Записано новых ID в {profile.sent_ids_filename}: {saved} (из {len(all_new_tenders)} найденных)"
        )


def cli_main(profile: IcetradeParserProfile) -> None:
    global _extra_params_cache_by_profile_id
    script_dir = os.path.dirname(os.path.abspath(__file__))

    _extra_params_cache_by_profile_id = None
    mention = resolve_mention(profile)
    bot = resolve_bot_token(profile)
    chat = resolve_chat_id(profile)
    days_back = int(os.environ.get("DAYS_BACK", "30"))
    max_pages = int(os.environ.get("MAX_PAGES", "120"))
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
            run_parser_cycle(rc, mention, tmpl_empty=tmpl_empty, tmpl_single=tmpl_single, tmpl_part=tmpl_part)
        except Exception:
            print("\n❌ КРИТИЧЕСКАЯ ОШИБКА:")
            print(traceback.format_exc())
        print("\n✅ Готово!")
    finally:
        # Вернуть stdout/stderr до закрытия лога — иначе при выходе из интерпретатора flush в закрытый файл.
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        sys.stdout = orig_out
        sys.stderr = orig_err
        log_handle.close()
