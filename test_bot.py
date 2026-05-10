import requests

BOT_TOKEN = '8677357886:AAHWAq-EfNxlcR7XQaz8es5eGnXriUNSfGk'
CHAT_ID = '-1001872277668'

# Формируем длинное сообщение (имитация отчета парсера)
msg = "<b>Тест: длинное сообщение</b>\n\n" + "Строка " * 500

url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
data = {
    'chat_id': CHAT_ID,
    'text': msg,
    'parse_mode': 'HTML',
    'disable_web_page_preview': True
}

r = requests.post(url, data=data)
print(f"Статус: {r.status_code}")
print(f"Ответ: {r.json()}")