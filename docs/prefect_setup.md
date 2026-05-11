# Prefect: оркестрация парсера icetrade → Telegram

Файл **`flows/it_scan_prefect.py`** задаёт один **flow** `icetrade_it_tenders_flow` и задачу `it_parser_cycle`, которая вызывает `it_parser.run_parser()` (перед этим задаётся `DAYS_BACK` / `MAX_PAGES` и выполняется `reload` модуля, чтобы Telegram‑настройки из env подтягивались повторно на одном воркере).

## 1. Установка

Из корня репозитория:

```bash
python -m pip install -r requirements-prefect.txt
```

(На Windows можно `py -3 -m pip ...`.)

## 2. Быстрый локальный запуск без сервера

Проверка, что код и секреты в env работают:

```bash
cd /path/to/parserIT
python flows/it_scan_prefect.py
```

Или один раз через CLI:

```bash
prefect deploy flows/it_scan_prefect.py:icetrade_it_tenders_flow --name local-test
```

(Для Prefect Cloud 3 см. официальный `prefect deploy` — мастер подскажет work pool.)

## 3. Prefect Cloud (рекомендуется для эксперимента)

1. Зарегистрируйтесь на [https://app.prefect.cloud](https://app.prefect.cloud).
2. Создайте **Workspace** и **API key**.
3. Локально:

   ```bash
   prefect cloud login
   prefect config set PREFECT_API_URL=https://api.prefect.cloud/api/account/<ACCOUNT_WORKSPACE_HINT>
   ```

   Уточняйте URL в UI Prefect («API URL» после создания workspace).

4. Задайте **переменные окружения** на агенте/воркере, где будет выполняться flow (бот, chat id, при необходимости `ICETRADE_*`), как в `env.example`.
5. Разверните flow:

   ```bash
   prefect deploy flows/it_scan_prefect.py:icetrade_it_tenders_flow -n icetrade-prod
   ```

   Дальше в UI задайте **schedule** (cron) на deployment.

6. Поднимите **worker** (раньше «agent»), который слушает ваш work pool — по [документации Prefect 3 Workers](https://docs.prefect.io/). На Windows‑сервере с `C:\tender_it` worker должен запускаться с `cwd`, где лежат `it_parser.py` и `flows/`, чтобы `sent_it_ids.txt` писался в репозиторий (или задайте `WINDMILL_USE_WMILL_STATE=1` только если скрипт крутится в Windmill, не смешивайте случайно).

## 4. Свой Prefect сервер (`prefect server start`)

Для офлайна / Docker:

```bash
prefect server start
prefect config set PREFECT_API_URL=http://127.0.0.1:4200/api
```

Затем `prefect worker start` или `prefect deployment run` по гайдам Prefect.

## 5. Повторы при сетевых сбоях

Через env (необязательно):

| Переменная | По умолчанию |
|------------|----------------|
| `PREFECT_IT_PARSER_RETRIES` | `2` |
| `PREFECT_IT_PARSER_RETRY_DELAY_SECONDS` | `60` |

## 6. Сравнение с Windmill

| | Prefect | Windmill |
|--|---------|---------|
| UI | Runs, блокировки | Flow editor, триггеры |
| Где живёт код | Git + ваш воркер | Часто sync в Git или UI |
| Секреты | Prefect Blocks / инфра | Variables workspace |

Вы можете использовать **только Prefect**, только **Windmill**, или Prefect как верхний планировщик и отдельные шаги — по необходимости.
