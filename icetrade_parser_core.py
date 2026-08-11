"""
Общее ядро парсера icetrade.by → Telegram (ИТ-профиль и профиль ангаров).

ИТ-профиль (id «it»): по умолчанию без industries — поиск по всему icetrade, отбор ключевыми словами
и чёрным списком. Рубрикатор опционально: ICETRADE_INDUSTRIES / icetrade_industry_params.json.

Профиль ангаров (id «angar»): рубрикатор по умолчанию не подставляется — запрос идёт без industries,
список тендеров сужается только ключевыми словами и чёрным списком. Позже рубрикатор можно включить
через ANGAR_ICETRADE_INDUSTRIES или icetrade_industry_params_angar.json.

Изначально: логика it_parser.py + совместимые параметры формы запроса (исправление устаревшего ParserAngar).
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    "Referer": "https://icetrade.by/",
    "Upgrade-Insecure-Requests": "1",
}

# По умолчанию для профиля it (рабочая строка с icetrade; совпадает с прежним it_parser.py)
DEFAULT_IT_INDUSTRIES = "16/17/18/105.106-115/116.117-122/179/370.371-387"

_extra_params_cache_by_profile_id: dict[str, dict[str, object]] | None = None  # keyed by env path string
_logged_industry_mode: dict[str, bool] = {}
_http_session: requests.Session | None = None


class IcetradeFetchLock:
    """Простой file-lock: на self-hosted один процесс ходит на icetrade (анти-WAF)."""

    def __init__(self, script_dir: str, profile_id: str):
        self.path = os.path.join(script_dir, "logs", "icetrade_fetch.lock")
        self.profile_id = profile_id
        self._fh = None

    def acquire(self, wait_sec: float = 900.0) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            try:
                # Windows: O_EXCL atomic create
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                self._fh = os.fdopen(fd, "w", encoding="utf-8")
                self._fh.write(f"{os.getpid()}\t{self.profile_id}\t{_utc_ts()}\n")
                self._fh.flush()
                return True
            except FileExistsError:
                # stale lock > 45 мин — снять
                try:
                    age = time.time() - os.path.getmtime(self.path)
                    if age > 45 * 60:
                        print(f"  ⚠️ Снимаем протухший lock ({age/60:.0f} мин): {self.path}")
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                print("  ⏳ Ждём освобождения icetrade lock (другой парсер ещё работает)…")
                time.sleep(15 + random.uniform(0, 5))
            except OSError as e:
                print(f"  ⚠️ Lock недоступен ({e}), продолжаем без блокировки")
                return True
        print("  ❌ Не дождались icetrade lock")
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
                # удаляем только свой, если содержимое наше — упрощённо: всегда remove после успешного acquire
                os.remove(self.path)
        except OSError:
            pass


def _request_timeout() -> float:
    try:
        return float(os.environ.get("ICETRADE_HTTP_TIMEOUT", "45"))
    except ValueError:
        return 45.0


def _request_retries() -> int:
    try:
        # Для 429 лучше меньше попыток, но с длинными паузами (см. _retry_after_seconds).
        return max(1, int(os.environ.get("ICETRADE_HTTP_RETRIES", "4")))
    except ValueError:
        return 4


def get_http_session() -> requests.Session:
    """Один Session на процесс: cookies + браузерные заголовки (WAF/антибот icetrade)."""
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
    """Вернуть причину блокировки/пустого ответа или None если страница похожа на выдачу."""
    low = (text or "").lower()
    if status_code == 403:
        return "HTTP 403 Forbidden"
    if status_code == 429:
        return "HTTP 429 Too Many Requests"
    if status_code >= 500:
        return f"HTTP {status_code}"
    if "access denied" in low or "доступ запрещ" in low:
        return "страница Access denied"
    if "cf-browser-verification" in low or "just a moment" in low:
        return "Cloudflare challenge"
    # Успешный поиск всегда содержит список аукционов (даже пустой).
    if "auctions-list" not in low and 'id="auctions"' not in low and "auctions_list" not in low:
        # короткие заглушки WAF часто без таблицы
        if len(text) < 8000 or "notfound" in low:
            return "нет #auctions-list в HTML (похоже на блок/заглушку)"
    return None


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
    telegram_append_keyword_roots_footer: bool = False  # блок корней ключевых слов в Telegram (ангары)
    # Мягкий стоп-лист: срабатывает только если в заголовке нет «сильного» корня (см. keywords_strong_roots).
    blacklist_soft: tuple[str, ...] = ()
    keywords_strong_roots: tuple[str, ...] = ()


def _env_first(*names: str) -> str:
    for name in names:
        v = os.environ.get(name)
        if v and str(v).strip():
            return str(v).strip()
    return ""


def resolve_bot_token(profile: IcetradeParserProfile) -> str:
    if profile.id == "angar":
        v = _env_first("ANGAR_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
    elif profile.id == "equipment":
        v = _env_first("EQUIPMENT_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
    else:
        v = _env_first("TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
    return (v or _DEFAULT_BOT_TOKEN_FALLBACK).strip()


def resolve_chat_id(profile: IcetradeParserProfile) -> str:
    if profile.id == "angar":
        v = _env_first("ANGAR_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID", "CHAT_ID")
        return (v or _DEFAULT_CHAT_ID_FALLBACK).strip()
    if profile.id == "equipment":
        return _env_first("EQUIPMENT_TELEGRAM_CHAT_ID")
    v = _env_first("TELEGRAM_CHAT_ID", "CHAT_ID")
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


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def transfer_log_disabled() -> bool:
    return os.environ.get("ICETRADE_DISABLE_TRANSFER_LOG", "").lower() in ("1", "true", "yes", "on")


def append_transfer_journal(script_dir: str, event: str, **kv: object) -> None:
    """Строковый журнал отправок Telegram (UTC, UTF-8), каталог logs/ создаётся при необходимости."""
    if transfer_log_disabled():
        return
    try:
        log_dir = os.path.join(script_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        default_path = os.path.join(log_dir, "telegram_transfer.log")
        path = os.environ.get("ICETRADE_TRANSFER_LOG") or default_path
        parts = [_utc_ts(), f"event={event}"]
        gh = os.environ.get("GITHUB_RUN_ID")
        if gh:
            parts.append(f"github_run={gh}")
        wf = os.environ.get("GITHUB_WORKFLOW")
        if wf:
            parts.append(f"github_workflow={wf}")
        for key in sorted(kv):
            val = kv[key]
            if val is None:
                continue
            parts.append(f"{key}={val}")
        line = "\t".join(parts) + "\n"
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        print("  ⚠️ Не удалось дописать журнал передач (checks logs/, права записи)")


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
    if profile.id == "equipment":
        p = os.environ.get("EQUIPMENT_ICETRADE_PARAMS_JSON")
        if p and p.strip():
            return os.path.abspath(p.strip())
        return os.path.join(script_dir, "icetrade_industry_params_equipment.json")
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
    elif profile.id == "equipment":
        ind_env = os.environ.get("EQUIPMENT_ICETRADE_INDUSTRIES")
        env_key_logged = "equipment"
        disable_key = os.environ.get("EQUIPMENT_ICETRADE_DISABLE_DEFAULT_INDUSTRY", "")
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
                which = {
                    "it": "ICETRADE_INDUSTRIES",
                    "angar": "ANGAR_ICETRADE_INDUSTRIES",
                    "equipment": "EQUIPMENT_ICETRADE_INDUSTRIES",
                }.get(profile.id, "ICETRADE_INDUSTRIES")
                print(f"  📎 icetrade [{profile.id}]: industries отключены ({which} пусто/off)")
                _logged_industry_mode[env_key_logged] = True
        else:
            extra["industries"] = s
            if not logged:
                which = {
                    "it": "ICETRADE_INDUSTRIES",
                    "angar": "ANGAR_ICETRADE_INDUSTRIES",
                    "equipment": "EQUIPMENT_ICETRADE_INDUSTRIES",
                }.get(profile.id, "ICETRADE_INDUSTRIES")
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
                if profile.id == "it":
                    print(
                        "  📎 icetrade [it]: industries не заданы — поиск по всему icetrade.by, "
                        "отбор ключевыми словами и чёрным списком. "
                        "Сузить выдачу: ICETRADE_INDUSTRIES или icetrade_industry_params.json."
                    )
                elif profile.id == "equipment":
                    print(
                        "  📎 icetrade [equipment]: industries не заданы — поиск по всему icetrade.by, "
                        "отбор ключевыми словами и чёрным списком. "
                        "Сузить: EQUIPMENT_ICETRADE_INDUSTRIES или icetrade_industry_params_equipment.json."
                    )
                else:
                    print(
                        "  📎 icetrade [angar]: industries не заданы — поиск по всему icetrade.by, "
                        "отбор только ключевыми словами и чёрным списком. "
                        "Позже можно задать рубрикатор: ANGAR_ICETRADE_INDUSTRIES или icetrade_industry_params_angar.json."
                    )
                _logged_industry_mode[env_key_logged] = True
    return extra


def build_base_search_params(created_from: str, created_to: str) -> dict[str, str]:
    # Больше лотов на страницу = меньше HTTP-запросов (реже 429 от icetrade).
    on_page = os.environ.get("ICETRADE_ON_PAGE", "50").strip() or "50"
    try:
        n = int(on_page)
        if n < 10:
            n = 10
        if n > 100:
            n = 100
        on_page = str(n)
    except ValueError:
        on_page = "50"
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
        "onPage": on_page,
        "created_from": created_from,
        "created_to": created_to,
    }


def _rate_limit_cooldown_path(script_dir: str) -> str:
    return os.path.join(script_dir, "logs", "icetrade_rate_limit_until.txt")


def _read_rate_limit_until(script_dir: str) -> float:
    path = _rate_limit_cooldown_path(script_dir)
    try:
        with open(path, encoding="utf-8") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0


def _write_rate_limit_cooldown(script_dir: str, seconds: float) -> None:
    until = time.time() + max(60.0, seconds)
    path = _rate_limit_cooldown_path(script_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{until:.0f}\n")
        print(f"  🧊 Rate-limit cooldown до UTC {datetime.fromtimestamp(until, timezone.utc).strftime('%H:%M:%S')} (~{seconds/60:.0f} мин)")
    except OSError as e:
        print(f"  ⚠️ Не записали cooldown: {e}")


def _wait_rate_limit_cooldown(script_dir: str) -> None:
    until = _read_rate_limit_until(script_dir)
    now = time.time()
    if until <= now:
        return
    wait = until - now
    # В GitHub Actions не ждём часами — если cooldown > 20 мин, выходим сразу (следующий schedule).
    if wait > 20 * 60:
        raise RuntimeError(
            f"icetrade rate-limit cooldown ещё {wait/60:.0f} мин "
            f"(до UTC {datetime.fromtimestamp(until, timezone.utc).strftime('%H:%M')}). "
            "Пропуск запуска, чтобы не усугублять 429."
        )
    print(f"  ⏳ Ждём снятие rate-limit ещё {wait:.0f}с…")
    time.sleep(wait + random.uniform(1.0, 3.0))


def _retry_after_seconds(response: requests.Response, attempt: int, is_429: bool) -> float:
    """Пауза перед повтором: для 429 — минуты, иначе короткий backoff."""
    if is_429:
        ra = response.headers.get("Retry-After") if response is not None else None
        if ra:
            try:
                return max(60.0, float(ra))
            except ValueError:
                pass
        # 2, 4, 8, 12 мин — короткие ретраи только раскачивают 429
        ladder = [120.0, 240.0, 480.0, 720.0, 900.0]
        return ladder[min(attempt, len(ladder) - 1)] + random.uniform(5.0, 20.0)
    return min(60.0, (2 ** attempt) + random.uniform(0.5, 2.0))


def get_page(rc: RunnerConfig, page_num: int) -> BeautifulSoup | None:
    cf, ct = get_date_range(rc.days_back)
    base = build_base_search_params(cf, ct)
    extra = icetrade_search_extra_params(rc.profile, rc.script_dir)
    params: dict[str, object] = {**base, **extra, "p": page_num}
    try:
        _wait_rate_limit_cooldown(rc.script_dir)
    except RuntimeError as e:
        print(f"  ❌ {e}")
        return None
    session = get_http_session()
    retries = _request_retries()
    timeout = _request_timeout()
    last_err = ""
    for attempt in range(retries):
        try:
            r = session.get(SEARCH_URL, params=params, timeout=timeout)
            blocked = _response_looks_blocked(r.status_code, r.text)
            if blocked:
                last_err = blocked
                is_429 = r.status_code == 429 or "429" in blocked
                if is_429:
                    _write_rate_limit_cooldown(rc.script_dir, _retry_after_seconds(r, attempt, True))
                if attempt + 1 < retries:
                    delay = _retry_after_seconds(r, attempt, is_429)
                    print(
                        f"  ⚠️ стр. {page_num}: {blocked}; повтор {attempt + 2}/{retries} через {delay:.0f}с"
                    )
                    time.sleep(delay)
                    # Сессию сбрасываем только на жёсткий 403/Access denied (не на 429).
                    if r.status_code == 403 or "access denied" in blocked.lower():
                        global _http_session
                        _http_session = None
                        session = get_http_session()
                    continue
                print(f"  ❌ Ошибка загрузки страницы {page_num}: {blocked}")
                return None
            r.raise_for_status()
            print(f"  стр. {page_num} загружена")
            return BeautifulSoup(r.text, "html.parser")
        except requests.exceptions.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt + 1 < retries:
                delay = min(90.0, (2 ** attempt) * 2 + random.uniform(1.0, 3.0))
                print(
                    f"  ⚠️ стр. {page_num}: {last_err}; повтор {attempt + 2}/{retries} через {delay:.1f}с"
                )
                time.sleep(delay)
                continue
            print(f"  ❌ Ошибка загрузки страницы {page_num}: {last_err}")
            return None
        except Exception as e:
            print(f"  ❌ Ошибка загрузки страницы {page_num}: {e}")
            return None
    print(f"  ❌ Ошибка загрузки страницы {page_num}: {last_err or 'unknown'}")
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


def _title_has_strong_signal(title_lower: str, strong_roots: tuple[str, ...]) -> bool:
    return bool(strong_roots) and any(root in title_lower for root in strong_roots)


def is_blacklisted(
    title: str,
    blacklist: tuple[str, ...],
    *,
    blacklist_soft: tuple[str, ...] = (),
    keywords_strong_roots: tuple[str, ...] = (),
) -> bool:
    title_lower = title.lower()
    for word in blacklist:
        if word in title_lower:
            return True
    if blacklist_soft and not _title_has_strong_signal(title_lower, keywords_strong_roots):
        for word in blacklist_soft:
            if word in title_lower:
                return True
    return False


def format_price(price_str: str) -> str:
    """
    Сайт может отдавать суммы вида «190 688.40 BYN» (пробелы тысяч + доли рубля/копеек по BYN).
    """
    if not price_str or price_str.strip() == "—":
        return "—"
    raw = price_str.strip().replace("\u00a0", " ").replace("\u202f", " ")
    m = re.search(
        r"([\d\s\.,]+)\s*(BYN|руб\.?)\s*$|([\d\s\.,]+)\s*(USD|EUR)\s*$",
        raw,
        re.I,
    )
    if not m:
        return price_str
    if m.group(1):
        num_raw = m.group(1)
        curr_raw = m.group(2)
        cur_disp = "руб" if curr_raw.lower().startswith("руб") else curr_raw.upper()
    else:
        num_raw = m.group(3)
        curr_raw = m.group(4)
        cur_disp = curr_raw.upper()

    nums = "".join(ch for ch in num_raw if not ch.isspace())
    if not nums or not re.fullmatch(r"[\d\.,]+", nums):
        return price_str

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
        val = float(nums)
        if math.isnan(val) or math.isinf(val):
            raise ValueError
    except ValueError:
        return price_str

    if abs(val - round(val)) < 1e-6:
        n = int(round(val))
        formatted = f"{n:,}".replace(",", " ")
    else:
        neg = val < 0
        av = abs(val)
        frac_s = f"{av:.2f}"
        integer_part_s, frac = frac_s.split(".", 1)
        int_sep = int(integer_part_s)
        pref = "-" if neg else ""
        head = pref + (f"{int_sep:,}".replace(",", " "))
        formatted = f"{head}.{frac}"
    return f"{formatted} {cur_disp}"


def _keyword_roots_footer_html(profile: IcetradeParserProfile) -> str:
    if not profile.telegram_append_keyword_roots_footer or not profile.keywords_roots:
        return ""
    bullets = "\n".join(f"   • {r}" for r in profile.keywords_roots)
    return (
        '\n\n🔎 <b>Семантическое ядро (поисковые корни):</b>\n'
        f"{bullets}"
    )


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


def parse_tenders(soup: BeautifulSoup, profile: IcetradeParserProfile):
    tenders = []
    rows = soup.select("#auctions-list tr")
    if not rows:
        # запасной селектор на случай смены разметки
        rows = soup.select("table.auctions tr, table#auctions tr, .auctions-list tr")
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
        if not matches_roots(title, profile.keywords_roots):
            continue
        if is_blacklisted(
            title,
            profile.blacklist,
            blacklist_soft=profile.blacklist_soft,
            keywords_strong_roots=profile.keywords_strong_roots,
        ):
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
) -> bool:
    """Вернуть True если цикл завершён штатно (в т.ч. реально 0 лотов), False при сбое загрузки."""
    profile = rc.profile

    # Не стартовать три профиля в одну секунду — WAF icetrade режет пачку запросов.
    try:
        stagger = float(os.environ.get("ICETRADE_START_STAGGER_SEC", "0") or "0")
    except ValueError:
        stagger = 0.0
    if stagger <= 0:
        stagger = random.uniform(0.5, 8.0)
    if stagger > 0:
        print(f"⏳ Пауза старта {stagger:.1f}с (анти-WAF)")
        time.sleep(stagger)

    fetch_lock = IcetradeFetchLock(rc.script_dir, profile.id)
    if not fetch_lock.acquire():
        err_msg = (
            f"⚠️ {mention}\n"
            f"<b>Парсер [{profile.id}] не запустился</b>\n"
            f"Другой профиль ещё ходит на icetrade (lock timeout)."
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
    print(f"📅 Диапазон: {cf} - {ct} (последние {rc.days_back} дней)")
    print(f"📄 Максимум страниц: {rc.max_pages}")
    print(f"💾 Отправленные ID: {sent_path}")

    sent_ids = load_sent_ids(sent_path)
    print(f"📦 Уже отправлено тендеров: {len(sent_ids)}")

    all_new_tenders = []
    seen_in_session = set()
    stopped_early_bad_page = False
    pages_ok = 0
    empty_streak = 0
    try:
        empty_stop = int(os.environ.get("ICETRADE_EMPTY_PAGE_STOP", "30"))
    except ValueError:
        empty_stop = 30

    for page in range(1, rc.max_pages + 1):
        print(f"\n--- Страница {page} ---")
        soup = get_page(rc, page)
        if not soup:
            print("❌ ошибка загрузки, прерываем")
            stopped_early_bad_page = True
            break

        pages_ok += 1
        tenders = parse_tenders(soup, profile)
        if tenders:
            empty_streak = 0
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
            if new_on_page == 0:
                empty_streak += 1
        else:
            # Отличить «страница без совпадений» от «разметка сломалась»
            n_rows = len(soup.select("#auctions-list tr"))
            if n_rows <= 1:
                n_rows = len(
                    soup.select("table.auctions tr, table#auctions tr, .auctions-list tr")
                )
            if n_rows <= 1:
                print("  ⚠️ В HTML нет строк аукционов — возможно блок/смена вёрстки")
            print("  ❌ Нет подходящих")
            empty_streak += 1

        if empty_stop > 0 and empty_streak >= empty_stop:
            print(
                f"⏹ Стоп: {empty_streak} страниц подряд без новых лотов профиля "
                f"(ICETRADE_EMPTY_PAGE_STOP={empty_stop}) — экономим лимит icetrade."
            )
            break

        time.sleep(random.uniform(3.5, 6.0))

    xfer_new = len(all_new_tenders)
    print(f"\n📊 ИТОГО новых ({profile.tmpl_done_count_label}): {xfer_new}")

    # Сбой сети/WAF на первой же странице — не врать «тендеров нет».
    if stopped_early_bad_page and pages_ok == 0:
        cooldown_left = max(0.0, _read_rate_limit_until(rc.script_dir) - time.time())
        rate_hint = ""
        if cooldown_left > 0:
            rate_hint = (
                f"\nСейчас активен rate-limit cooldown (~{cooldown_left/60:.0f} мин). "
                "Не перезапускайте вручную пачкой — подождите следующий schedule."
            )
        err_msg = (
            f"⚠️ {mention}\n"
            f"<b>Парсер [{profile.id}] не смог загрузить icetrade.by</b>\n"
            f"Ошибка сети/доступа (часто <b>HTTP 429</b> rate-limit / 403/WAF). "
            f"Лоты не проверялись.{rate_hint}"
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
        print("❌ Цикл прерван: icetrade недоступен с первой страницы — «пусто» в Telegram не считаем успехом.")
        return False

    telegram_warmup(rc.bot_token)

    kw_footer = _keyword_roots_footer_html(profile)
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
        msg = tmpl_empty(mention=mention, days_back=rc.days_back) + note + kw_footer
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
        all_new_tenders.sort(key=lambda x: x.get("date_end", ""), reverse=False)
        chunks = build_telegram_chunks(rc, mention, all_new_tenders, tmpl_single=tmpl_single, tmpl_part=tmpl_part)
        xfer_chunks_total = len(chunks)
        if kw_footer and chunks:
            last_txt, last_ids = chunks[-1]
            chunks[-1] = (last_txt + kw_footer, last_ids)
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
                        f"❌ Часть {idx + 1}/{len(chunks)} не отправлена — остановка без записи этой и следующих частей; "
                        "повторите запуск позже."
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


def _load_dotenv_if_present(script_dir: str) -> None:
    """Опционально: .env рядом со скриптами (в .gitignore), только если переменная ещё не задана."""
    path = os.path.join(script_dir, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                name, _, val = line.partition("=")
                name = name.strip()
                val = val.strip().strip('"').strip("'")
                # Пустая переменная из GitHub Actions (секрет не задан) не должна блокировать .env на сервере.
                if name and (name not in os.environ or not str(os.environ.get(name, "")).strip()):
                    os.environ[name] = val
    except OSError:
        print(f"  ⚠️ Не удалось прочитать {path}")


def cli_main(profile: IcetradeParserProfile) -> None:
    global _extra_params_cache_by_profile_id
    script_dir = os.path.dirname(os.path.abspath(__file__))

    _load_dotenv_if_present(script_dir)
    _extra_params_cache_by_profile_id = None
    mention = resolve_mention(profile)
    bot = resolve_bot_token(profile)
    chat = resolve_chat_id(profile)
    if profile.id == "equipment" and not chat:
        print(
            "❌ Для профиля equipment задайте EQUIPMENT_TELEGRAM_CHAT_ID "
            "(числовой id группы; бот должен быть участником). "
            "Ссылка-приглашение t.me/+… не подходит как chat_id."
        )
        sys.exit(1)
    days_back = int(os.environ.get("DAYS_BACK", "30"))
    # 50 лотов/стр × 40 стр ≈ 2000 лотов при ~40 запросах (раньше 20×120=2400 при 120 запросах → 429).
    max_pages = int(os.environ.get("MAX_PAGES", "40"))
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
            ok = run_parser_cycle(
                rc, mention, tmpl_empty=tmpl_empty, tmpl_single=tmpl_single, tmpl_part=tmpl_part
            )
        except Exception:
            print("\n❌ КРИТИЧЕСКАЯ ОШИБКА:")
            print(traceback.format_exc())
            ok = False
        print("\n✅ Готово!" if ok else "\n❌ Завершено с ошибкой загрузки icetrade.")
        if not ok:
            sys.exit(1)
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
