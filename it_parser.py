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

# ============================================================
# НАСТРОЙКИ
BOT_TOKEN = '8677357886:AAHWAq-EfNxlcR7XQaz8es5eGnXriUNSfGk'
CHAT_ID   = '-1001872277668'
DAYS_BACK = 30
MAX_PAGES = 120

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

if os.path.exists(LOG_FILE):
    os.remove(LOG_FILE)
log_handle = open(LOG_FILE, "w", encoding="utf-8")
sys.stdout = Tee(sys.stdout, log_handle)
sys.stderr = Tee(sys.stderr, log_handle)
# ============================================================

# ---------- СЕМАНТИЧЕСКОЕ ЯДРО ----------
KEYWORDS_ROOTS = [
    'программн', 'разработк', 'доработк', 'внедрени', 'сопровожд', 'модернизац',
    'информационн', 'информац систем',
    'erp', 'crm', 'scm', 'bpm', 'wms', 'биллинг',
    'сервер', 'cloud', 'виртуализац', 'облачн', 'сетевое оборуд',
    'нейросет', 'искусственн', 'машинн', 'big data', 'аналитика данных',
    'распознаван', 'nlp', 'llm', 'gpt',
    'лиценз', 'софт', 'программное обеспеч', 'поставк по',
    'планшет', 'ноутбук', 'рабоч станц', 'серверн обору',
]

# ---------- ЧЁРНЫЙ СПИСОК ----------
BLACKLIST = [
    'проектно-сметной', 'проектно-сметная', 'проектно-сметную', 'проектно-сметного',
    'сметной документации', 'сметная документация',
    'проектной документации', 'проектная документация',
    'строительно-монтажные', 'строительно-монтажных',
    'ремонт', 'кровля', 'фасад', 'павильон', 'сэндвич-панел',
    'кабель', 'провод', 'труб', 'бетон', 'кирпич', 'штукатурк',
    'модернизация электроосвещения', 'модернизация системы кондиционирования',
    'модернизация здания', 'модернизация горячего водоснабжения',
    'модернизация системы теплоснабжения', 'модернизация электрооборудования',
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
    except:
        return f"{num_part} {currency}"

def send_telegram(text):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    data = {'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
    try:
        r = requests.post(url, data=data, timeout=15)
        result = r.json()
        print(f"📤 Отправка в Telegram: статус {r.status_code}, ok={result.get('ok', False)}")
        if not result.get('ok'):
            print(f"   Ошибка: {result.get('description', 'неизвестно')}")
        return result.get('ok', False)
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")
        return False

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

def split_and_send(text, mention):
    max_len = 4000
    if len(text) <= max_len:
        send_telegram(text)
        return
    parts = []
    current = f"{mention}\n📋 <b>Новые ИТ-тендеры</b>\n\n"
    for line in text.split('\n'):
        if len(current + line + '\n') > max_len:
            parts.append(current)
            current = f"{mention}\n📋 <b>Новые ИТ-тендеры (продолжение)</b>\n\n"
        current += line + '\n'
    if current:
        parts.append(current)
    for part in parts:
        send_telegram(part)
        time.sleep(0.5)

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

        if not all_new_tenders:
            msg = f"{mention}\n📭 За последние {DAYS_BACK} дней новых ИТ-тендеров не найдено."
            send_telegram(msg)
            print("📭 Сообщение отправлено (тендеров нет)")
        else:
            all_new_tenders.sort(key=lambda x: x.get('date_end', ''), reverse=False)
            msg = f"{mention}\n📋 <b>Новые ИТ-тендеры за последние {DAYS_BACK} дней: {len(all_new_tenders)}</b>\n\n"
            for i, t in enumerate(all_new_tenders[:20], 1):
                price_fmt = format_price(t['price'])
                msg += (f"{i}. <b>{t['title']}</b>\n"
                        f"   🏢 {t['customer']}\n"
                        f"   💰 {price_fmt}\n"
                        f"   📅 Приём до: {t['date_end']}\n"
                        f"   🔗 <a href=\"{t['url']}\">Ссылка</a>\n\n")
            if len(all_new_tenders) > 20:
                msg += f"... и еще {len(all_new_tenders)-20}\n\n"
            
            print(f"📤 Формируем сообщение для отправки...")
            split_and_send(msg, mention)
            print(f"✅ Отправлено {len(all_new_tenders)} тендеров")

            for t in all_new_tenders:
                tender_id = extract_tender_id(t['url'])
                if tender_id:
                    save_sent_id(tender_id)

    except Exception as e:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА:")
        print(traceback.format_exc())
    
    print("\n✅ Готово!")

if __name__ == '__main__':
    main()
    log_handle.close()