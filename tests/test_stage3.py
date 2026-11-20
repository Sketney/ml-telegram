"""Рубрики по заметкам, шпаргалки, фактчек, статистика."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app import db, factcheck, generator, metrics, moderation, render
from app import transcribe as stt
from app.models import Draft, DraftImage, DraftStatus, PostStats, Topic, TopicStatus
from app.sources import service
from app.sources.collectors import Found
from app.validator import validate_sheet
from tests.conftest import ADMIN, CHANNEL, FAKE_PNG
from tests.test_sources_flow import last_text, press_and_wait_card, say_and_wait_card

NOTES = (
    "Проверял, влезет ли Qwen 7B в 8 ГБ VRAM. Стенд: RTX 3070, Ubuntu 24.04, Ollama. "
    "q4_K_M влезла, 41 токен в секунду. q8 не влезла, упала в своп, 3 токена в секунду. Вывод: брать q4."
)


def levels(issues, level):
    return [i["text"] for i in issues if i["level"] == level]


# --- вопрос с собеса ---------------------------------------------------------------


async def test_interview_is_a_backlog_rubric(tg):
    card = await say_and_wait_card(tg, "/gen interview")
    assert "Вопрос с ML-собеса" in card["text"]
    async with db.session() as s:
        draft = (await s.scalars(select(Draft))).one()
    assert draft.payload["html"].rstrip().endswith("#EML_interview") and not draft.has_errors


# --- заметки автора -------------------------------------------------------------------


async def test_experiment_from_notes_in_several_messages(tg, llm):
    tg.user_message(ADMIN, "/note experiment")
    await tg.wait_for(lambda: "Присылай заметки для «Эксперимент»" in last_text(tg))
    tg.user_message(ADMIN, NOTES)
    await tg.wait_for(lambda: f"Всего {len(NOTES)} символов" in last_text(tg))
    tg.user_document(ADMIN, "log.md", "Команда: ollama run qwen:7b-q4_K_M".encode())
    await tg.wait_for(lambda: f"Всего {len(NOTES) + 34} символов" in last_text(tg))
    card = await say_and_wait_card(tg, "/done")

    assert "Эксперимент" in card["text"]
    prompt = next(user for _, user in llm.calls if "<notes>" in user)
    assert "41 токен в секунду" in prompt and "ollama run qwen:7b-q4_K_M" in prompt
    assert "<material>" in llm.calls[-1][1] and "41 токен в секунду" in llm.calls[-1][1]  # фактчек по заметкам
    async with db.session() as s:
        draft = (await s.scalars(select(Draft))).one()
        topic = await s.get(Topic, draft.topic_id)
    assert draft.rubric == "experiment" and draft.payload["html"].rstrip().endswith("#EML_experiment")
    assert topic.notes.startswith("Проверял, влезет ли") and topic.status == TopicStatus.USED.value
    assert topic.title == "Проверял, влезет ли Qwen 7B в 8 ГБ VRAM. Стенд: RTX 3070, Ubuntu 24.04"

    # перегенерация опирается на те же заметки
    await press_and_wait_card(tg, card, "🔄 Заново")
    assert sum("<notes>" in user for _, user in llm.calls) == 2


async def test_voice_without_command_becomes_practice_story(tg, llm):
    mark = {"message_id": max(tg.messages, default=0)}
    tg.user_voice(ADMIN)
    card = await tg.next_card(ADMIN, mark)
    assert "История с работы" in card["text"]
    assert any("Расшифровываю" in m["text"] for m in tg.sent("sendMessage", ADMIN))
    prompt = next(user for _, user in llm.calls if "<notes>" in user)
    assert "балансировка не работает" in prompt
    async with db.session() as s:
        assert (await s.scalars(select(Draft.rubric))).all() == ["practice"]


async def test_voice_inside_note_mode_is_added_to_notes(tg, llm):
    tg.user_message(ADMIN, "/note practice")
    await tg.wait_for(lambda: "Присылай заметки" in last_text(tg))
    tg.user_voice(ADMIN)
    await tg.wait_for(lambda: "Принял. Всего" in last_text(tg))
    first = last_text(tg)
    tg.user_message(ADMIN, "Дело было в port-forward.")
    await tg.wait_for(lambda: last_text(tg).startswith("Принял") and last_text(tg) != first)
    await say_and_wait_card(tg, "/done")
    prompt = next(user for _, user in llm.calls if "<notes>" in user)
    assert "балансировка не работает" in prompt and "Дело было в port-forward." in prompt


async def test_voice_failure_and_empty_and_short_notes(tg, monkeypatch):
    async def broken(data, settings):
        raise stt.TranscribeUnavailable("пакет faster-whisper не установлен")

    monkeypatch.setattr(stt, "transcribe", broken)
    tg.user_voice(ADMIN)
    await tg.wait_for(lambda: "Не получилось расшифровать: пакет faster-whisper не установлен" in last_text(tg))

    tg.user_message(ADMIN, "/note experiment")
    await tg.wait_for(lambda: "Присылай заметки" in last_text(tg))
    tg.user_message(ADMIN, "/done")
    await tg.wait_for(lambda: "Заметок пока нет" in last_text(tg))
    tg.user_document(ADMIN, "dump.bin", b"\xff\xfe\x00\x01" * 10)
    await tg.wait_for(lambda: "Не могу прочитать файл как текст" in last_text(tg))
    tg.user_message(ADMIN, "слишком коротко")
    await tg.wait_for(lambda: "Принял" in last_text(tg))
    tg.user_message(ADMIN, "/done")
    await tg.wait_for(lambda: "заметок слишком мало" in last_text(tg))
    async with db.session() as s:
        assert (await s.scalars(select(Draft))).all() == []
        assert (await s.scalars(select(Topic.status).where(Topic.rubric == "experiment"))).all() == ["new"]

    for cmd, hint in (("/gen experiment", "/note experiment"), ("/add practice история", "/note practice")):
        tg.user_message(ADMIN, cmd)
        await tg.wait_for(lambda hint=hint: hint in last_text(tg))
    tg.user_message(ADMIN, "/note concept")
    await tg.wait_for(lambda: "Формат: /note" in last_text(tg))


# --- фактчек ------------------------------------------------------------------------------


class Scripted:
    def __init__(self, *answers: str) -> None:
        self.answers, self.calls = list(answers), []

    async def complete(self, system: str, user: str, *, cheap: bool = False) -> str:
        self.calls.append((system, user, cheap))
        return self.answers.pop(0)


async def test_factcheck_findings_and_failures():
    found = json.dumps({"unsupported": [{"claim": "в 10 раз быстрее", "why": "в источнике 2.03 раза"}, {"claim": ""}]})
    llm = Scripted(found, "не JSON")
    issues = await factcheck.check(llm, "<b>пост</b>", "материал")
    assert issues == [
        {"level": "warn", "text": "фактчек: «в 10 раз быстрее» — в источнике 2.03 раза", "factcheck": "claim"}
    ]
    assert llm.calls[0][2] is True and "<material>\nматериал" in llm.calls[0][1]  # дешёвая модель
    (failed,) = await factcheck.check(llm, "<b>пост</b>", "материал")
    assert failed["level"] == "warn" and "фактчек не выполнен" in failed["text"]


async def test_factcheck_warning_reaches_the_card_but_does_not_block(tg, settings, database):
    post = json.dumps({"html": "<b>Эксперимент</b>\n\nМодель выдала 500 токенов в секунду."})
    check = json.dumps({"unsupported": [{"claim": "500 токенов в секунду", "why": "в заметках 41"}]})
    async with db.session() as s:
        topic = await generator.topic_from_notes(s, "experiment", NOTES)
        draft = await generator.generate_draft(s, Scripted(post, check), settings, "experiment", topic)
        warnings = [w for w in levels(draft.issues, "warn") if w.startswith("фактчек")]
        assert warnings == ["фактчек: «500 токенов в секунду» — в заметках 41"]
        assert not draft.has_errors
        await moderation.approve(s, draft)  # решение за автором: предупреждение публикацию не закрывает


async def test_backlog_rubrics_are_not_factchecked(database, settings, llm):
    async with db.session() as s:
        await generator.generate_draft(s, llm, settings, "concept")
    assert len(llm.calls) == 1


# --- при оценке кандидатов учитываются уже написанные темы -----------------------------------


async def test_ranking_prompt_lists_topics_already_covered(database, settings, llm):
    async with db.session() as s:
        await generator.generate_draft(s, llm, settings, "concept")  # тема «Токен и токенизация» ушла в работу
        await service.store_found(s, [Found("test", "tool", "https://example.com/t", "Токенизатор на Rust")])
        await service.rank_new(s, llm)
    prompt = llm.calls[-1][1]
    assert "Темы, о которых канал уже писал или пишет:\n- Токен и токенизация" in prompt
    assert prompt.index("уже писал") < prompt.index("Материалы:")


# --- шпаргалка ------------------------------------------------------------------------------

SHEET = {
    "kind": "sheet",
    "html": "<b>Шпаргалка: квантизации</b>\n\n" + "Зачем она нужна и когда пригодится. " * 6 + "\n\n#EML_cheatsheet",
    "sheet": {
        "title": "Квантизации GGUF",
        "subtitle": "Что значат буквы в названии файла модели",
        "sections": [
            {"heading": "Размер", "items": [{"term": "Q4_K_M", "text": "примерно 4,8 бита на вес"}, {"term": "Q8_0", "text": "8 бит"}]},
            {"heading": "Когда брать", "items": [{"term": "Ноутбук", "text": "Q4_K_M"}, {"term": "Сервер", "text": "Q8_0 или FP16"}]},
        ],
        "takeaway": "Смотри на размер файла, а не на тег.",
    },
}  # fmt: skip


def test_validate_sheet():
    assert validate_sheet(SHEET, min_len=200, max_len=700) == []
    long_caption = {**SHEET, "html": "я" * 1025}
    assert any("лимит Telegram — 1024" in e for e in levels(validate_sheet(long_caption), "error"))
    one_block = {**SHEET, "sheet": {**SHEET["sheet"], "sections": SHEET["sheet"]["sections"][:1]}}
    assert any("нужно от 2 до 6" in e for e in levels(validate_sheet(one_block), "error"))
    no_title = {**SHEET, "sheet": {**SHEET["sheet"], "title": ""}}
    assert any("нет заголовка" in e for e in levels(validate_sheet(no_title), "error"))
    empty_row = {**SHEET, "sheet": {**SHEET["sheet"], "sections": [
        {"heading": "A", "items": [{"term": "x", "text": ""}]}, {"heading": "B", "items": [{"term": "y", "text": "z"}]},
    ]}}  # fmt: skip
    assert any("пустая строка" in e for e in levels(validate_sheet(empty_row), "error"))
    wide = {**SHEET, "sheet": {**SHEET["sheet"], "takeaway": "я" * 200}}
    assert any("может не поместиться" in w for w in levels(validate_sheet(wide), "warn"))


def test_sheet_html_escapes_model_text():
    html = render.sheet_html(
        {
            "title": "<script>alert(1)</script>",
            "sections": [{"heading": "A & B", "items": [{"term": "x<y", "text": "t"}]}],
        },
        brand_emoji="🔹",
        channel="@Easy_ML",
    )
    assert "<script>alert" not in html and "&lt;script&gt;" in html and "A &amp; B" in html and "x&lt;y" in html


async def test_cheatsheet_preview_edit_caption_and_publish(tg):
    card = await say_and_wait_card(tg, "/gen cheatsheet")
    preview = tg.sent("sendPhoto", ADMIN)[0]
    assert preview["photo"] == FAKE_PNG and preview["parse_mode"] == "HTML" and "#EML_cheatsheet" in preview["caption"]
    assert "Шпаргалка" in card["text"]
    assert set(tg.buttons(card)) == {
        "✅ Сейчас", "🕒 В расписание", "✏️ Подпись", "🔄 Заново", "💬 С комментарием", "⏸ Отложить", "❌ Отклонить",
    }  # fmt: skip

    tg.press(ADMIN, card, tg.buttons(card)["✏️ Подпись"])
    await tg.wait_for(lambda: "Пришли новый текст" in last_text(tg))
    edited = await say_and_wait_card(tg, "Новая подпись к шпаргалке")
    async with db.session() as s:
        draft = (await s.scalars(select(Draft))).one()
        assert draft.payload["html"] == "Новая подпись к шпаргалке" and draft.payload["sheet"]["sections"]
        assert (await s.get(DraftImage, draft.id)).png == FAKE_PNG  # картинка осталась

    tg.press(ADMIN, edited, tg.buttons(edited)["✅ Сейчас"])
    await tg.wait_for(lambda: tg.sent("sendPhoto", CHANNEL))
    posted = tg.sent("sendPhoto", CHANNEL)[0]
    assert posted["photo"] == FAKE_PNG and posted["caption"] == "Новая подпись к шпаргалке"
    await tg.wait_for(lambda: tg.sent("editMessageText"))
    async with db.session() as s:
        assert (await s.scalars(select(Draft.status))).all() == [DraftStatus.PUBLISHED.value]


async def test_cheatsheet_without_picture_cannot_be_published(tg, monkeypatch):
    async def broken(sheet, **kwargs):
        raise render.RenderUnavailable("пакет playwright не установлен")

    monkeypatch.setattr(render, "render_sheet", broken)
    card = await say_and_wait_card(tg, "/gen cheatsheet")
    assert "картинка не собрана: пакет playwright не установлен" in card["text"]
    assert "✅ Сейчас" not in tg.buttons(card) and tg.sent("sendPhoto") == []
    async with db.session() as s:
        draft = (await s.scalars(select(Draft))).one()
        assert draft.has_errors
        with pytest.raises(moderation.NotAllowed):
            await moderation.publish(s, tg.bot, tg.settings, draft)


async def test_real_render_produces_png():
    pytest.importorskip("playwright")
    try:
        png = await render.__dict__["render_sheet_real"](SHEET["sheet"], brand_emoji="🔹", channel="@Easy_ML")
    except render.RenderUnavailable as e:
        pytest.skip(f"браузер недоступен: {e}")
    assert png.startswith(b"\x89PNG") and len(png) > 20_000


# --- статистика ---------------------------------------------------------------------------------

PAGE = """
<div class="tgme_widget_message" data-post="Easy_ML/101"><span class="tgme_widget_message_views">2.32K</span></div>
<div class="tgme_widget_message service_message" data-post="Easy_ML/102"></div>
<div class="tgme_widget_message" data-post="Easy_ML/103"><span class="tgme_widget_message_views">845</span></div>
"""


def test_parse_counts_and_page():
    assert [metrics.parse_count(x) for x in ("845", "2.32K", "1.2M", " 3K ", "n/a", "")] == [
        845, 2320, 1_200_000, 3000, None, None,
    ]  # fmt: skip
    assert metrics.parse_channel_page(PAGE) == {101: 2320, 103: 845}


async def test_stats_refresh_and_command(tg, settings, monkeypatch):
    fetched = []

    async def fake_fetch(url, **kwargs):
        fetched.append(url)
        return PAGE if "before" not in url else ""

    monkeypatch.setattr(metrics, "fetch_text", fake_fetch)
    async with db.session() as s:
        for rubric, message_id in (("concept", 101), ("history", 103), ("quiz", 999)):
            draft = await generator.generate_draft(s, tg.llm, settings, rubric)
            draft.status, draft.channel_message_id = DraftStatus.PUBLISHED.value, message_id
        await s.commit()
        assert await metrics.refresh(s, settings) == 2
        assert sorted((await s.scalars(select(PostStats.views))).all()) == [845, 2320]
    assert fetched[0] == "https://t.me/s/test_channel"

    tg.user_message(ADMIN, "/stats")
    await tg.wait_for(lambda: "Просмотры по рубрикам" in last_text(tg))
    text = last_text(tg)
    assert "Понятие недели: в среднем 2320 · постов 1" in text and "История ML: в среднем 845" in text
    assert text.index("Понятие недели") < text.index("История ML")  # по убыванию просмотров
    assert "2320 — Понятие недели: Токен и токенизация" in text and "Без данных постов: 1" in text


async def test_stats_need_public_username(database, settings):
    settings.channel_id = "-1001234567890"
    async with db.session() as s:
        with pytest.raises(metrics.StatsUnavailable, match="@username"):
            await metrics.refresh(s, settings)


# --- «А знали ли вы?»: пост только по фактам со ссылкой ------------------------------------------

FACTS = (
    "Tay — чат-бот компании Microsoft, запущенный в Twitter 23 марта 2016 года. Его отключили через 16 часов "
    "после запуска. https://en.wikipedia.org/wiki/Tay_(chatbot)"
)


async def test_fact_post_is_written_from_backlog_facts(tg, llm):
    card = await say_and_wait_card(tg, "/gen fact")
    assert "А знали ли вы?" in card["text"]
    prompt = next(user for _, user in llm.calls if "<facts>" in user)
    assert "обыграл профессионального игрока" in prompt and "https://en.wikipedia.org/wiki/OpenAI_Five" in prompt
    assert "<material>" in llm.calls[-1][1]  # и сверен с теми же фактами
    async with db.session() as s:
        draft = (await s.scalars(select(Draft))).one()
    assert draft.rubric == "fact" and draft.payload["html"].rstrip().endswith("#EML_didyouknow")
    # заглушка FakeLLM ссылку не ставит — автор видит это в карточке
    assert any(
        "нет ссылки на источник: https://en.wikipedia.org/wiki/OpenAI_Five" in w for w in levels(draft.issues, "warn")
    )
    assert not draft.has_errors


async def test_fact_topics_need_facts_and_a_link(tg, database):
    tg.user_message(ADMIN, "/add fact Просто интересный заголовок")
    await tg.wait_for(lambda: "должны быть проверенные факты и ссылка" in last_text(tg))
    tg.user_message(
        ADMIN,
        "/add fact Заголовок | факты есть, а ссылки нет, хотя текста тут достаточно много для проверки длины, честное слово, очень много текста",
    )
    await tg.wait_for(
        lambda: sum("должны быть проверенные факты" in m["text"] for m in tg.sent("sendMessage", ADMIN)) == 2
    )
    tg.user_message(ADMIN, f"/add fact Чат-бот прожил 16 часов | {FACTS}")
    await tg.wait_for(lambda: last_text(tg) == "Добавил в бэклог.")
    async with db.session() as s:
        topic = await s.scalar(select(Topic).where(Topic.title == "Чат-бот прожил 16 часов"))
        assert topic.rubric == "fact" and topic.notes == FACTS
        with pytest.raises(ValueError):
            await generator.add_topic(s, "fact", "Без фактов")
    card = await say_and_wait_card(tg, f"/gen fact Свой факт | {FACTS}")
    assert "Свой факт" in card["text"]


async def test_backlog_sync_skips_fact_topics_without_source(database, tmp_path):
    path = tmp_path / "backlog.yaml"
    path.write_text(
        "fact:\n  - Голый заголовок без фактов\n"
        "  - {title: С фактами, facts: 'Кто, когда и что произошло, подробно и по делу.', source: 'https://example.com/a'}\n",
        encoding="utf-8",
    )
    async with db.session() as s:
        before = len((await s.scalars(select(Topic).where(Topic.rubric == "fact"))).all())
        assert await generator.sync_backlog(s, path) == 1
        topic = await s.scalar(select(Topic).where(Topic.title == "С фактами"))
        assert topic.notes.endswith("Источник: https://example.com/a")
        assert len((await s.scalars(select(Topic).where(Topic.rubric == "fact"))).all()) == before + 1


# --- автоисправление по фактчеку ---------------------------------------------------------------

BAD = json.dumps({"html": "<b>Эксперимент</b>\n\nМодель выдала 500 токенов в секунду."})
GOOD = json.dumps({"html": "<b>Эксперимент</b>\n\nМодель выдала 41 токен в секунду."})
FOUND = json.dumps({"unsupported": [{"claim": "500 токенов в секунду", "why": "в заметках 41"}]})
CLEAN = json.dumps({"unsupported": []})


async def experiment_draft(settings, llm) -> Draft:
    async with db.session() as s:
        topic = await generator.topic_from_notes(s, "experiment", NOTES)
        return await generator.generate_draft(s, llm, settings, "experiment", topic)


async def test_post_is_rewritten_once_by_factcheck_findings(database, settings):
    llm = Scripted(BAD, FOUND, GOOD, CLEAN)
    draft = await experiment_draft(settings, llm)
    assert "41 токен в секунду" in draft.payload["html"] and "500" not in draft.payload["html"]
    assert draft.payload["html"].count("#EML_experiment") == 1  # хештег не задвоился при пересборке
    assert levels(draft.issues, "info") == ["исправлено по фактчеку: «500 токенов в секунду» — в заметках 41"]
    assert not [w for w in levels(draft.issues, "warn") if w.startswith("фактчек")]
    assert [cheap for _, _, cheap in llm.calls] == [False, True, False, True]  # пост, проверка, правка, перепроверка
    revise_prompt = llm.calls[2][1]
    assert "<notes>" in revise_prompt and "500 токенов в секунду" in revise_prompt and "<post>" in revise_prompt
    assert "#EML_experiment" not in revise_prompt


async def test_remaining_findings_stay_as_warnings_and_no_second_rewrite(database, settings):
    llm = Scripted(BAD, FOUND, BAD, FOUND)  # правка не помогла
    draft = await experiment_draft(settings, llm)
    assert len(llm.calls) == 4  # второй раз не переписываем: решение за автором
    assert [w for w in levels(draft.issues, "warn") if w.startswith("фактчек")] == [
        "фактчек: «500 токенов в секунду» — в заметках 41"
    ]


async def test_broken_rewrite_keeps_the_original_post(database, settings):
    for broken in ("не JSON", json.dumps({"html": "<p>сломанная разметка</p>"})):
        llm = Scripted(BAD, FOUND, broken, broken, CLEAN)
        async with db.session() as s:
            topic = await generator.topic_from_notes(s, "experiment", NOTES + " " + broken[:5])
            draft = await generator.generate_draft(s, llm, settings, "experiment", topic)
        assert "500 токенов в секунду" in draft.payload["html"]
        assert levels(draft.issues, "info") == []
        assert any(w.startswith("фактчек: «500") for w in levels(draft.issues, "warn"))


async def test_rewrite_keeps_the_author_block(database, settings, monkeypatch):
    with_block = json.dumps({"html": "<b>Инструмент</b>\n\nУмеет всё.\n\n<b>Мой опыт:</b> {Мой опыт}"})
    fixed = json.dumps({"html": "<b>Инструмент</b>\n\nУмеет кое-что.\n\n<b>Мой опыт:</b> {Мой опыт}"})
    found = json.dumps({"unsupported": [{"claim": "Умеет всё", "why": "в README этого нет"}]})
    llm = Scripted(with_block, found, fixed, CLEAN)
    async with db.session() as s:
        await service.store_found(
            s, [Found("github", "tool", "https://github.com/a/t", "a/t", "desc", {"repo": "a/t"})]
        )
        item = await s.scalar(select(service.SourceItem))
        topic = await generator.topic_from_item(s, item)
        draft = await generator.generate_draft(s, llm, settings, "tool", topic)
    assert "Умеет кое-что" in draft.payload["html"] and "{Мой опыт}" in draft.payload["html"]
    assert len(llm.calls) == 4 and draft.has_errors  # блок автора по-прежнему ждёт заполнения
