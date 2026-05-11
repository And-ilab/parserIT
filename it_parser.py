"""
Парсер icetrade.by -> Telegram.

Диагностика «ничего не уходит с сервера / машина засыпает» — пришлите вывод команд:

1) Кто запускает и как:
   - crontab -l
   - sudo crontab -l
   - systemctl list-timers --all | head -40
   - ls -la /etc/cron.* 2>/dev/null; grep -R . /etc/cron.d/ 2>/dev/null | head

2) Сон / энергия (Linux):
   - cat /sys/power/mem_sleep 2>/dev/null; cat /sys/power/state 2>/dev/null
   - loginctl show-session $(loginctl | awk '/seat0/ {print $1; exit}') -p IdleHint,IdleSinceHint 2>/dev/null
   - systemd-inhibit --list 2>/dev/null

3) Сеть до Telegram с самого сервера:
   - curl -sS -o /dev/null -w '%{http_code}\n' --max-time 15 https://api.telegram.org/
   - getent hosts api.telegram.org

4) Лог последнего запуска (если пишется в файл рядом со скриптом):
   - tail -n 80 /opt/parserIT/it_parser_log.txt   (путь поправьте)

Если это не VPS, а ПК/ноутбук с suspend — планировщик не сработает, пока машина спит:
внешний триггер (GitHub Actions, cron-job.org, Windmill) надёжнее, чем локальный cron.
"""
import requests
import urllib3
import sys
import traceback

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup
import time
import random
import re
import os
from datetime import datetime, timedelta


def _configure_stdio_utf8_windows():
    """NETWORK SERVICE + cp1251 breaks print() with emoji; Telegram/HTML stays unchanged."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, AttributeError, ValueError):
                pass


_configure_stdio_utf8_windows()

# ============================================================
# НАСТРОЙКИ
# Сначала переменные окружения (когда перейдёте на них), иначе — значения по умолчанию как раньше.
_DEFAULT_BOT_TOKEN = "8677357886:AAHWAq-EfNxlcR7XQaz8es5eGnXriUNSfGk"
_DEFAULT_CHAT_ID = "-1001872277668"
BOT_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or _DEFAULT_BOT_TOKEN
).strip()
CHAT_ID = (
    os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID") or _DEFAULT_CHAT_ID
).strip()
DAYS_BACK = int(os.environ.get("DAYS_BACK", "30"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "120"))
TELEGRAM_SEND_RETRIES = int(os.environ.get("TELEGRAM_SEND_RETRIES", "6"))
# Запас ниже лимита Telegram 4096 (учёт HTML и UTF-16 в редких случаях).
TELEGRAM_SAFE_TEXT_LIMIT = int(os.environ.get("TELEGRAM_SAFE_TEXT_LIMIT", "3800"))
HEADER_RESERVE = 280

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SENT_IDS_FILE = os.path.join(SCRIPT_DIR, "sent_it_ids.txt")
LOG_FILE = os.path.join(SCRIPT_DIR, "it_parser_log.txt")

# Перенаправляем вывод в файл
class Tee:
    def __init__(self, *files):
        self.files = files
    def write(self, obj):
        for f in self.files:
            f.write(obj)
            f.flush()
    def flush(self):
        for f in self.files:
            f.flush()

# Удаляем старый лог
if os.path.exists(LOG_FILE):
    os.remove(LOG_FILE)
log_handle = open(LOG_FILE, "w", encoding="utf-8", errors="replace")
sys.stdout = Tee(sys.stdout, log_handle)
sys.stderr = Tee(sys.stderr, log_handle)
# ============================================================

# ---------- СЕМАНТИЧЕСКОЕ ЯДРО ДЛЯ ИТ ----------
# Только явные IT-маркеры (короткие «разработк», «управлени», «сете», «техническ» давали стройку и закупки).
KEYWORDS_ROOTS = [
    # ИИ / ML / данные
    'искусственн', 'интеллектуал', 'нейросет', 'нейронн', 'когнитивн',
    'gpt', 'llm', 'nlp', 'bert', 'трансформер', 'семантическ',
    'big data', 'data science', 'machine learn', 'deep learn',
    'dnn', ' lstm', ' cnn', 'random forest', ' xgboost',
    'распознаван', 'кластеризац', 'сегментац', 'прогнозир', 'рекомендательн',
    'компьютерн зрени', 'обработк изображени', 'ocr',
    ' ai', ' ml', ' dl ', ' data ', 'аналитик данных', 'анализ данных',
    # ПО: общий префикс «программный», «программное», «программных» и т.д.
    'програм',
    'web-прилож', 'мобильн прилож',
    'информационные систем', 'информационных систем', 'информационным систем',
    'информационной систем', 'информационную систем', 'информационн систем',
    'информационн технолог', 'информационн ресурс',
    # «Автоматизация …» и «автоматизированная система …» — общий префикс в русском языке
    'автоматиз',
    'асу ', 'асу тп',
    'erp', 'crm', 'субд', 'sql', 'oracle', 'postgres', 'mongodb',
    '1с:', '1с ', '1c:', '1c ', 'bitrix', 'битрикс',
    'docker', 'kubernetes', 'k8s', 'devops', 'ci/cd', 'gitlab', 'jenkins',
    'сервер', 'виртуализаци', 'vmware', 'hyper-v', 'proxmox',
    'cloud', 'облач', 'saas', 'paas', 'iaas',
    'кибер', 'кибербезопас', 'информбезопас', 'siem', 'soc ',
    'api', 'backend', 'frontend', 'микросервис',
    'разработк по', 'доработк конфигурац', 'внедрени erp', 'внедрени crm',
    'внедрени информационн', 'лицензи по',
    'продлени прав на использование программ', 'обновлени версий программ',
    'it-аутсорс', ' it ', 'айти ', 'сайт', 'портал', 'хостинг', 'домен',
    'электронн документооборот', 'эдо ',
    'development', 'software',
    # Лицензии/обновления ПО (короткие «разработк»/«модернизаци» не используем — ловят проектную документацию и стройку)
    'лицензи', 'обновлени', 'продлени',
]

# ---------- ЧЁРНЫЙ СПИСОК ----------
# Отсекаем стройку, АПК, медицину, закупку «железа без ИТ», проектную документацию и т.п.
BLACKLIST = [
    # Проектно-сметная документация
    'проектно-сметной',
    'проектно-сметная',
    'проектно-сметную',
    'проектно-сметного',
    'сметной документации',
    'сметная документация',
    'проектной документации',
    'проектная документация',
    'проектно-изыскательск',
    'предпроектной',
    'предынвестиционной',
    'предпроектн',
    'предынвестиционн',
    'изыскательск работ',
    'разработк предпроектной',
    'разработк проектной',
    'разработка предпроектной',
    'разработка проектной',
    'разработка рабочей документации',
    # Строительство и объекты
    'строительно-монтажные',
    'строительно-монтажных',
    'объекта строительства',
    'объекте строительства',
    'объект строительства',
    'объекта: «строительство',
    'объекта: "строительство',
    'строительств',
    'реконструкц',
    'благоустройств',
    'капитальн строен',
    'многоквартирн',
    'жилого дома',
    'жилой дом',
    'школы на',
    'школы в',
    'средней школы',
    'технического надзора',
    'технического надзор',
    'инженерных услуг по комплексному управлению строительной',
    'комплексному управлению строительной',
    'управлению строительной деятельностью',
    'управлением строительной деятельностью',
    'функций заказчика',
    'функций заказчика (застройщика)',
    'субподрядн',
    'подрядной организации для выполнения работ',
    'подрядной организации для выполнения общестроительных',
    'выбор подряд',
    'выбор субподряд',
    'выполнение комплекса работ по',
    'выполнение работ по прокладке',
    'выполнение подрядных работ',
    'устройство покрытий из асфальтобетона',
    'монтажу лифт',
    'лифтового оборудования',
    'лифтов',
    'пусконаладочн работам лифт',
    'диспетчеризац',
    'канализац',
    'водоотведен',
    'водопровод',
    'тепловых сетей',
    'наружных сетей',
    'металлоконструкц',
    'кабельных изделий',
    'электротехнической продукции',
    'трансформатор',
    'вакуумный выключатель',
    'выключателей автоматических для собственного производства',
    'песка сухого',
    'асфальтобетон',
    'газоанализатор',
    'ремонт',
    'кровля',
    'фасад',
    'окон и дверей пвх',
    'зеркал для',
    'моющих средств',
    'моющие средства',
    'средств защиты растений',
    'средств для обработки копыт',
    'средств индивидуальной защиты органов дыхания',
    'микроудобрений',
    'противогаз',
    'шин согласно',
    'автомобильные шины',
    'доильн',
    'свинарник',
    'кормов и зерна',
    'ветеринарн',
    'иммунохемилюминесцентн',
    'реагентов для',
    'лекарственных средств',
    'наборов для экспресс-анализа',
    'спектрофотометра',
    'навоза',
    'зернохранилищ',
    'теплиц',
    'трактора',
    'переоборудование трактора',
    'строительной техники с экипажем',
    'гусеничный экскаватор',
    'арендодателя строительной техники',
    'маркетинговые исследования по определению стоимости',
    'маркетинговые исследования конъюнктуры',
    'маркетинговые исследования',
    'закупка транспортного средства',
    'закупка автобуса',
    'закупка пленки стретч',
    'закупка песка',
    'закупка металлоконструкций',
    'закупка электротехнической',
    'закупка газоанализаторов',
    'закупка средств защиты растений',
    'закупка средств для обработки',
    'закупка лекарственных',
    'закупка зеркал',
    'закупка шин',
    'закупка бытовок',
    'поставка моющих',
    'поставка, установку оборудования автоматизированной системы учета транспортных средств',
    'автоматизированной системы учета транспортных средств и взимания оплаты',
    'на кладбищ',
    'сценического комплекса',
    'декорационного оформления',
    'реалити-шоу',
    # «Программа» не про ПО (корень «програм» широкий)
    'телепрограм',
    'программа работ',
    'программы работ',
    'программе работ',
    'программ передач',
    'программ телепередач',
    'программа мероприят',
    'программы мероприят',
    'программа производства работ',
    # Промышленные роботы / весы / пищевое оборудование (не ИТ-услуги)
    'робототехническ сварочн',
    'сварочн комплекс',
    'взвешивающ',
    'bizerba',
    'этикетировочн комплекс',
    # Колл-центр без ИТ-разработки
    'аутсорсингов контакт-центр',
    'аутсорсингового контакт-центра',
]

BASE_URL = 'https://icetrade.by'
SEARCH_URL = 'https://icetrade.by/search/auctions'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def get_date_range(days_back=DAYS_BACK):
    today = datetime.now().date()
    from_date = today - timedelta(days=days_back)
    return from_date.strftime('%d.%m.%Y'), today.strftime('%d.%m.%Y')

created_from, created_to = get_date_range()

BASE_PARAMS = {
    'search': 'Найти',
    'zakup_type[1]': '1', 'zakup_type[2]': '1',
    'establishment': '0',
    't[Trade]': '1', 't[eTrade]': '1', 't[Request]': '1',
    't[singleSource]': '1', 't[Auction]': '1', 't[Other]': '1',
    't[contractingTrades]': '1', 't[socialOrder]': '1', 't[negotiations]': '1',
    'r[1]': '1', 'r[2]': '2', 'r[7]': '7',
    'r[3]': '3', 'r[4]': '4', 'r[6]': '6', 'r[5]': '5',
    'sort': 'num:desc', 'onPage': '20',
    'created_from': created_from,
    'created_to': created_to,
}

def load_sent_ids():
    if not os.path.exists(SENT_IDS_FILE):
        return set()
    with open(SENT_IDS_FILE, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f if line.strip())

def save_sent_id(tender_id):
    with open(SENT_IDS_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{tender_id}\n")
        f.flush()
        os.fsync(f.fileno())

def extract_tender_id(url):
    match = re.search(r'/view/(\d+)', url)
    return match.group(1) if match else None

def matches_roots(title, roots):
    title_lower = title.lower()
    for root in roots:
        if root in title_lower:
            return True
    return False

def is_blacklisted(title, blacklist):
    title_lower = title.lower()
    for word in blacklist:
        if word in title_lower:
            return True
    return False

def format_price(price_str):
    if not price_str or price_str == '—':
        return '—'
    match = re.search(r'([\d\s]+)\s*(BYN|руб|USD|EUR)', price_str, re.I)
    if not match:
        return price_str
    num_part = match.group(1).strip().replace(' ', '')
    currency = match.group(2).upper()
    try:
        num_int = int(num_part)
        formatted = f"{num_int:,}".replace(',', ' ')
        return f"{formatted} {currency}"
    except Exception:
        return f"{num_part} {currency}"

def _telegram_send_once(text):
    """Один запрос sendMessage. Возвращает (ok, detail)."""
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    data = {
        'chat_id': CHAT_ID,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
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

def send_telegram(text):
    """
    Отправка с экспоненциальной задержкой между попытками.
    Возвращает True только если Telegram подтвердил успех.
    """
    backoff = [1, 2, 4, 8, 16, 32, 32]
    last_err = ""
    for attempt in range(TELEGRAM_SEND_RETRIES):
        ok, err = _telegram_send_once(text)
        if ok:
            if attempt > 0:
                print(f"  ✅ Telegram: успех с попытки {attempt + 1}")
            return True
        last_err = err
        print(f"  ⚠️ Telegram попытка {attempt + 1}/{TELEGRAM_SEND_RETRIES} не удалась: {err}")
        if attempt + 1 < TELEGRAM_SEND_RETRIES:
            delay = backoff[min(attempt, len(backoff) - 1)]
            time.sleep(delay + random.uniform(0, 0.35))
    print(f"  ❌ Telegram: все попытки исчерпаны. Последняя ошибка: {last_err}")
    return False

def telegram_warmup():
    """Лёгкий запрос к API после пробуждения сети (DNS/TLS)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    try:
        r = requests.get(url, timeout=20)
        if not r.ok:
            print(f"  ⚠️ getMe не OK: HTTP {r.status_code}")
    except Exception as e:
        print(f"  ⚠️ getMe пропущен из-за ошибки: {e}")

def format_tender_block(index, t):
    price_fmt = format_price(t['price'])
    return (
        f"{index}. <b>{t['title']}</b>\n"
        f"   🏢 {t['customer']}\n"
        f"   💰 {price_fmt}\n"
        f"   📅 Приём до: {t['date_end']}\n"
        f"   🔗 <a href=\"{t['url']}\">Ссылка</a>\n\n"
    )

def build_telegram_chunks(mention, days_back, all_new_tenders):
    """
    Список пар (text, [tender_id, ...]) в пределах лимита длины.
    ID только у позиций с извлекаемым /view/ID.
    """
    total = len(all_new_tenders)
    blocks = []
    for i, t in enumerate(all_new_tenders, 1):
        tid = extract_tender_id(t['url'])
        blocks.append({"i": i, "t": t, "text": format_tender_block(i, t), "id": tid})

    limit_body = TELEGRAM_SAFE_TEXT_LIMIT - HEADER_RESERVE
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

    parts = len(chunk_block_lists)
    out = []
    for part_i, blist in enumerate(chunk_block_lists, 1):
        if parts == 1:
            header = (
                f"{mention}\n📋 <b>Новые ИТ-тендеры за последние {days_back} дней: {total}</b>\n\n"
            )
        else:
            header = (
                f"{mention}\n📋 <b>Новые ИТ-тендеры: {total} шт.</b> "
                f"(часть {part_i}/{parts}, последние {days_back} дн.)\n\n"
            )
        body = "".join(x["text"] for x in blist)
        ids = [x["id"] for x in blist if x["id"]]
        out.append((header + body, ids))
    return out

def get_page(page_num):
    params = {**BASE_PARAMS, 'p': page_num}
    try:
        r = requests.get(SEARCH_URL, headers=HEADERS, params=params, timeout=20, verify=False)
        r.raise_for_status()
        print(f"  стр. {page_num} загружена")
        return BeautifulSoup(r.text, 'html.parser')
    except Exception as e:
        print(f"  ❌ Ошибка загрузки страницы {page_num}: {e}")
        return None

def parse_tenders(soup):
    tenders = []
    rows = soup.select('#auctions-list tr')
    if len(rows) <= 1:
        return tenders
    for row in rows[1:]:
        cols = row.find_all('td')
        if len(cols) < 6:
            continue
        link_tag = cols[0].find('a')
        if not link_tag:
            continue
        title = link_tag.get_text(strip=True)
        if not matches_roots(title, KEYWORDS_ROOTS):
            continue
        if is_blacklisted(title, BLACKLIST):
            print(f"   ⛔ Исключён по чёрному списку: {title[:60]}")
            continue
        url = link_tag.get('href')
        if url and not url.startswith('http'):
            url = BASE_URL + url
        customer = cols[1].get_text(strip=True) or 'Не указан'
        price_raw = cols[4].get_text(strip=True)
        date_end_raw = cols[5].get_text(strip=True)
        tenders.append({
            'title': title,
            'customer': customer,
            'price': price_raw,
            'date_end': date_end_raw,
            'url': url,
        })
    return tenders

def main():
    try:
        mention = "@AndrPon"
        print("🚀 Парсер ИТ-тендеров запущен")
        print(f"📅 Диапазон: {created_from} - {created_to} (последние {DAYS_BACK} дней)")
        print(f"📄 Максимум страниц: {MAX_PAGES}")

        sent_ids = load_sent_ids()
        print(f"📦 Уже отправлено тендеров: {len(sent_ids)}")

        all_new_tenders = []
        seen_in_session = set()

        for page in range(1, MAX_PAGES + 1):
            print(f"\n--- Страница {page} ---")
            soup = get_page(page)
            if not soup:
                print("❌ ошибка загрузки, прерываем")
                break

            tenders = parse_tenders(soup)
            if tenders:
                new_on_page = 0
                for t in tenders:
                    tender_id = extract_tender_id(t['url'])
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

        print(f"\n📊 ИТОГО новых ИТ-тендеров: {len(all_new_tenders)}")

        telegram_warmup()

        if not all_new_tenders:
            msg = f"{mention}\n📭 За последние {DAYS_BACK} дней новых ИТ-тендеров не найдено."
            if send_telegram(msg):
                print("📭 Сообщение в Telegram доставлено (тендеров нет)")
            else:
                print("❌ Не удалось отправить сообщение в Telegram (тендеров нет). sent_it_ids не менялся.")
        else:
            all_new_tenders.sort(key=lambda x: x.get('date_end', ''), reverse=False)
            chunks = build_telegram_chunks(mention, DAYS_BACK, all_new_tenders)
            saved = 0
            for idx, (text, ids_in_chunk) in enumerate(chunks):
                if not send_telegram(text):
                    print(
                        f"❌ Часть {idx + 1}/{len(chunks)} не отправлена — остановка без записи этой и следующих частей; "
                        "повторите запуск позже."
                    )
                    break
                for tid in ids_in_chunk:
                    save_sent_id(tid)
                    saved += 1
            print(f"✅ Записано новых ID в sent_it_ids: {saved} (из {len(all_new_tenders)} найденных)")

    except Exception:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА:")
        print(traceback.format_exc())

    print("\n✅ Готово!")

if __name__ == '__main__':
    main()
    log_handle.close()
