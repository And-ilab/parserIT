"""
Парсер icetrade.by → Telegram: ангары и родственная номенклатура.

Тот же код запросов и отправки в Telegram, что и у ИТ (`icetrade_parser_core`), но своё семантическое ядро
и чёрный список. Рубрикатор icetrade **по умолчанию не используется** — отбор ключевыми словами по всей выдаче;
позже см. env.example (`ANGAR_ICETRADE_INDUSTRIES`).
"""
from icetrade_parser_core import IcetradeParserProfile, cli_main


# Из репозитория ParserAngar (anhary_parser.py); при необходимости расширяйте под свои задачи.
KEYWORDS_ROOTS = [
    "тентов",
    "ангар",
    "каркасно-тентов",
    "быстровозводим",
    "пневмокаркас",
    "модульн",
    "сборно-разборн",
    "навес",
    "рамп",
]

# Пока без стоп-слов (чёрный список пополняете по результатам работы парсера).
BLACKLIST = []


ANGAR_PROFILE = IcetradeParserProfile(
    id="angar",
    keywords_roots=tuple(KEYWORDS_ROOTS),
    blacklist=tuple(BLACKLIST),
    sent_ids_filename="sent_anhary_ids.txt",
    log_filename="angar_parser_log.txt",
    default_mention="@YesisGennady",
    use_it_default_industries_if_missing=False,
    tmpl_empty_ok=(
        "{{mention}}\n📭 За последние {{days_back}} дней новых тендеров по ангарам не найдено."
    ).replace("{{", "{").replace("}}", "}"),
    tmpl_chunk_header_single=(
        "{{mention}}\n📋 <b>Новые тендеры по ангарам за последние {{days_back}} дней: {{total}}</b>\n\n"
    ).replace("{{", "{").replace("}}", "}"),
    tmpl_chunk_header_part=(
        "{{mention}}\n📋 <b>Новые тендеры по ангарам: {{total}} шт.</b> "
        "(часть {{part}}/{{parts}}, последние {{days_back}} дн.)\n\n"
    ).replace("{{", "{").replace("}}", "}"),
    tmpl_run_title="Парсер тендеров по ангарам запущен",
    tmpl_done_count_label="тендеров по ангарам",
    telegram_mention_env="ANGAR_TELEGRAM_MENTION",
)


if __name__ == "__main__":
    cli_main(ANGAR_PROFILE)
