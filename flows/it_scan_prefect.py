"""
Prefect flow: один цикл it_parser.py (icetrade → фильтры → Telegram).

Запуск из корня репозитория: ``python flows/it_scan_prefect.py``

Переменные окружения (как для it_parser.py): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
WINDMILL_USE_WMILL_STATE, ICETRADE_*, см. env.example и docs/prefect_setup.md.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from prefect import flow, task
from prefect.logging import get_run_logger


@task(
    name="icetrade-it-parse-telegram",
    retries=int(os.environ.get("PREFECT_IT_PARSER_RETRIES", "2")),
    retry_delay_seconds=int(os.environ.get("PREFECT_IT_PARSER_RETRY_DELAY_SECONDS", "60")),
)
def it_parser_cycle(days_back: int, max_pages: int) -> dict:
    logger = get_run_logger()
    os.environ["DAYS_BACK"] = str(int(days_back))
    os.environ["MAX_PAGES"] = str(int(max_pages))
    cwd = Path.cwd().resolve()
    if cwd != _REPO_ROOT:
        logger.warning("cwd %s не совпадает с корнем репо %s — sent_it_ids.txt может оказаться не там", cwd, _REPO_ROOT)
    logger.info("DAYS_BACK=%s MAX_PAGES=%s", os.environ["DAYS_BACK"], os.environ["MAX_PAGES"])

    mod = sys.modules.get("it_parser")
    import it_parser

    if mod is not None:
        importlib.reload(it_parser)
    summary = it_parser.run_parser()

    logger.info(
        "готово: success=%s new=%s saved=%s",
        summary.get("success"),
        summary.get("new_tenders_count"),
        summary.get("ids_saved"),
    )
    return summary


@flow(
    name="icetrade-it-tenders-telegram",
    log_prints=True,
)
def icetrade_it_tenders_flow(days_back: int = 30, max_pages: int = 120) -> dict:
    """Планируйте в Prefect или вызывайте вручную."""
    return it_parser_cycle(days_back, max_pages)


if __name__ == "__main__":
    icetrade_it_tenders_flow()
