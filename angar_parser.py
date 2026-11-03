"""
Парсер icetrade.by → Telegram: ангары и родственная номенклатура.

Тот же код запросов и отправки в Telegram, что и у ИТ (`icetrade_parser_core`), но своё семантическое ядро
и чёрный список. Рубрикатор icetrade **по умолчанию не используется** — отбор ключевыми словами по всей выдаче,
затем DeepSeek отсекает ложные совпадения (корень «навес» в «навесном оборудовании», надзор, где навес лишь в скобках).
Ключ: `DEEPSEEK_API_KEY` в `.env`. Позже см. env.example (`ANGAR_ICETRADE_INDUSTRIES`).
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
    telegram_append_keyword_roots_footer=True,
    keyword_false_suffixes=(("навес", "н"),),
    ai_filter=True,
    ai_system_prompt=(
        "Ты отсекаешь спам в закупках. Релевантна закупка, поставка, аренда, монтаж или "
        "строительство именно таких объектов: ангар, тентовый или каркасно-тентовый павильон, "
        "пневмокаркас, быстровозводимое или сборно-разборное здание-ангар, склад или укрытие, "
        "навес для техники, сельхозмашин или хранения, рампа как часть такого сооружения. "
        "Не релевантно: навесное оборудование и оснастка машин, программные и мебельные модули, "
        "медтехника, дорожные и пандусные рампы, технадзор, ПСД и реконструкция, где ангар или "
        "навес лишь попутно упомянуты в скобках или в адресе более крупного объекта."
    ),
)


if __name__ == "__main__":
    cli_main(ANGAR_PROFILE)
