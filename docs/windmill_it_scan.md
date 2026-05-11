# Windmill: шаг «парсинг icetrade → Telegram»

Используется тот же код, что и `it_parser.py` на сервере. Переменная **`WINDMILL_USE_WMILL_STATE=1`** переключает хранение `sent_it_ids` на [persistent script state](https://www.windmill.dev/docs/advanced/persistent_sessions) через пакет `wmill`, чтобы между запусками в облаке не дублировать уведомления.

## 1. Переменные workspace (Secrets / Variables)

| Переменная | Обязательно | Комментарий |
|------------|-------------|-------------|
| `TELEGRAM_BOT_TOKEN` | да, лучше secret | То же значение, что бот Telegram |
| `TELEGRAM_CHAT_ID` | да | ID группы/чата |
| `WINDMILL_USE_WMILL_STATE` | да для облака | `1` |
| `IT_PARSER_DISABLE_FILE_LOG` | рекомендуется | `1` — без файла лога на воркере |
| `TELEGRAM_MENTION` | опционально | По умолчанию `@AndrPon` |
| `ICETRADE_INDUSTRIES` и др. | опционально | Как на сервере, см. `env.example` и docstring в `it_parser.py` |

Секреты не коммитьте; вносите только в Windmill UI.

## 2. Зависимости Python-скрипта

На шаге укажите **requirements**:

```text
requests>=2.28.0
beautifulsoup4>=4.12.0
urllib3>=2.0.0
```

Пакет `wmill` в облачном воркере обычно уже есть; если при импорте ошибка — добавьте `wmill` в requirements вашей версии.

## 3. Как подключить код

Удобные варианты:

**A.** [Git sync](https://www.windmill.dev/docs/advanced/git_sync) этого репозитория и скрипт‑обёртка в workspace, которая импортирует `it_parser`.

**B.** Один файл в UI: содержимое актуального `it_parser.py` из репозитория полностью, в конце публичное определение:

```python
def main(days_back: int = 30, max_pages: int = 120):
    return windmill_main(days_back=days_back, max_pages=max_pages)
```

Windmill будет вызывать `main` с аргументами из формы запуска / флоу.

## 4. Флоу (шаг один)

Тип: **Script** Python → указать интерпретируемый файл с `main` как выше, или добавить узел Flow «Run Script» со ссылкой на этот скрипт.

Расписание: **Schedule** (например 2 раза в сутки по Минску) на этот скрипт или на флоу из одного шага.

## 5. После успешного прогона

В логах job и возвращаемом значении есть поля наподобие:

`success`, `new_tenders_count`, `ids_saved`, `telegram_ok`, `telegram_chunks`.

Запуск с хоста (PowerShell/MCP): см. сообщения вида UUID job после `POST .../jobs/run/p/...` и `jobs_u/get/{uuid}`.

## 6. Важное

- **Self‑hosted воркер на Windows (`C:\tender_it`):** переменную `WINDMILL_USE_WMILL_STATE` можно **не ставить**, тогда снова используется файл `sent_it_ids.txt` рядом со скриптом (как при локальном запуске).
- Парсер ходит на icetrade с `verify_ssl=False`; в облаке воркер должен иметь исходящий доступ к `icetrade.by` и `api.telegram.org`.
