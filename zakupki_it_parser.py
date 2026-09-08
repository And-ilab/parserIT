"""
Парсер ЕИС zakupki.gov.ru → Telegram (ИТ-тендеры).

Семантика как у it_parser.py (KEYWORDS_ROOTS + BLACKLIST).
Запросы, разбор карточек и отдельный чат — модуль zakupki_parser_core.py.

Перед первым запуском:
  ZAKUPKI_TELEGRAM_CHAT_ID=-100xxxxxxxxxx
  бот — участник группы (ссылка t.me/+… не подходит как chat_id).
"""
from it_parser import BLACKLIST, KEYWORDS_ROOTS
from zakupki_parser_core import ZakupkiParserProfile, cli_main


ZAKUPKI_IT_PROFILE = ZakupkiParserProfile(
    id="zakupki_it",
    keywords_roots=tuple(KEYWORDS_ROOTS),
    blacklist=tuple(BLACKLIST),
    sent_ids_filename="sent_zakupki_it_ids.txt",
    log_filename="zakupki_it_parser_log.txt",
    default_mention="",
    tmpl_empty_ok=(
        "{{mention}}\n📭 За последние {{days_back}} дней новых ИТ-тендеров на ЕИС не найдено."
    ).replace("{{", "{").replace("}}", "}"),
    tmpl_chunk_header_single=(
        "{{mention}}\n📋 <b>Новые ИТ-тендеры ЕИС за последние {{days_back}} дней: {{total}}</b>\n\n"
    ).replace("{{", "{").replace("}}", "}"),
    tmpl_chunk_header_part=(
        "{{mention}}\n📋 <b>Новые ИТ-тендеры ЕИС: {{total}} шт.</b> "
        "(часть {{part}}/{{parts}}, последние {{days_back}} дн.)\n\n"
    ).replace("{{", "{").replace("}}", "}"),
    tmpl_run_title="Парсер ИТ-тендеров ЕИС (zakupki.gov.ru) запущен",
    tmpl_done_count_label="ИТ-тендеров ЕИС",
    telegram_mention_env="ZAKUPKI_TELEGRAM_MENTION",
)


if __name__ == "__main__":
    cli_main(ZAKUPKI_IT_PROFILE)
