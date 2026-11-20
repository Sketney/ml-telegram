"""Сквозные сценарии второго этапа: источники → дайджест → черновик по источнику → публикация."""

from __future__ import annotations

import asyncio
import copy

import pytest
from sqlalchemy import select

from app import db, generator, moderation
from app.models import Draft, DraftStatus, ItemStatus, SourceItem, Topic, TopicStatus
from app.scheduler import publish_slot_job, sources_job
from app.sources import service
from app.sources.collectors import Found
from tests.conftest import ADMIN, CHANNEL

FOUND = [
    Found("hf_papers", "paper", "https://huggingface.co/papers/1", "Статья про RAG", "Abstract", {"upvotes": 40}),
    Found(
        "github", "tool", "https://github.com/a/tool", "a/tool", "Inference server", {"stars": 1500, "repo": "a/tool"}
    ),
    Found("rss:Lab", "release", "https://lab.example/new-model", "New <Model> 7B", "Release notes", {"feed": "Lab"}),
]


@pytest.fixture
def collected(monkeypatch):
    """Сбор источников возвращает три заранее известных материала."""
    calls = []

    async def fake_collect(cfg, now=None):
        calls.append(1)
        return list(FOUND), ["RSS Сломанный: HTTP 500"]

    monkeypatch.setattr(service, "collect", fake_collect)
    return calls


async def fetch(tg) -> dict[str, dict]:
    """Шлёт /fetch и возвращает дайджесты по названию рубрики."""
    tg.user_message(ADMIN, "/fetch")
    await tg.wait_for(lambda: sum("Кандидаты:" in m["text"] for m in tg.sent("sendMessage", ADMIN)) == 3)
    cards = [copy.deepcopy(m) for m in tg.messages.values() if m.get("text", "").startswith("<b>Кандидаты:")]
    return {c["text"].splitlines()[0].removeprefix("<b>Кандидаты: ").removesuffix("</b>"): c for c in cards}


async def press_and_wait_card(tg, card: dict, button: str) -> dict:
    """Нажимает кнопку и ждёт карточку, которой до нажатия ещё не было."""
    mark = {"message_id": max(tg.messages, default=0)}
    tg.press(ADMIN, card, tg.buttons(card)[button])
    return await tg.next_card(ADMIN, mark)


async def say_and_wait_card(tg, text: str) -> dict:
    mark = {"message_id": max(tg.messages, default=0)}
    tg.user_message(ADMIN, text)
    return await tg.next_card(ADMIN, mark)


def last_text(tg) -> str:
    sent = tg.sent("sendMessage", ADMIN)
    return sent[-1]["text"] if sent else ""


async def test_fetch_reports_and_sends_digest(tg, collected):
    digests = await fetch(tg)
    assert set(digests) == {"Разбор статьи", "Релиз с разбором", "Инструмент недели"}
    report = next(m["text"] for m in tg.sent("sendMessage", ADMIN) if m["text"].startswith("Новых материалов"))
    assert "Новых материалов: 3, оценено: 3" in report and "RSS Сломанный: HTTP 500" in report

    sent = next(m for m in tg.sent("sendMessage", ADMIN) if m["text"].startswith("<b>Кандидаты: Релиз"))
    assert sent["parse_mode"] == "HTML" and sent["link_preview_options"] == {"is_disabled": True}
    assert '<a href="https://lab.example/new-model">New &lt;Model&gt; 7B</a>' in sent["text"]  # заголовок экранирован
    assert "7/10" in sent["text"] and "Lab" in sent["text"]
    assert list(tg.buttons(digests["Релиз с разбором"])) == ["✍️ 1", "🚫 1"]
    for _ in range(100):  # отметка «показан» ставится сразу после отправки последнего дайджеста
        async with db.session() as s:
            statuses = set((await s.scalars(select(SourceItem.status))).all())
        if statuses == {ItemStatus.OFFERED.value}:
            break
        await asyncio.sleep(0.02)
    assert statuses == {ItemStatus.OFFERED.value}

    # повторный сбор: материалы уже известны, новых кандидатов нет
    tg.user_message(ADMIN, "/fetch")
    await tg.wait_for(lambda: "Новых кандидатов выше порога нет" in tg.sent("sendMessage", ADMIN)[-1]["text"])
    assert len(collected) == 2
    tg.user_message(ADMIN, "/digest")  # а /digest показывает прежних
    await tg.wait_for(lambda: sum("Кандидаты:" in m["text"] for m in tg.sent("sendMessage", ADMIN)) == 6)


async def test_write_tool_fill_own_block_and_publish(tg, collected, llm):
    digest = (await fetch(tg))["Инструмент недели"]
    card = await press_and_wait_card(tg, digest, "✍️ 1")

    # модель получила текст источника и ссылку, а не только название
    prompt = next(user for _, user in llm.calls if "<source>" in user)
    assert "<material>" in llm.calls[-1][1]  # следом шёл фактчек по тому же материалу
    assert "ТЕКСТ ИСТОЧНИКА: a/tool" in prompt and "https://github.com/a/tool" in prompt and "<source>" in prompt
    assert "reply_markup" not in tg.messages[digest["message_id"]]  # кнопки использованного кандидата убраны

    # пустой блок автора не считается ошибкой модели: генерация обошлась одним вызовом
    assert sum("<source>" in user for _, user in llm.calls) == 1

    # блок «Мой опыт» не заполнен — публиковать нельзя ни сразу, ни в расписание
    assert "Инструмент недели" in card["text"] and "{Мой опыт}" in card["text"] and "🧩" in card["text"]
    assert set(tg.buttons(card)) == {
        "🧩 Вписать: Мой опыт", "✏️ Править", "🔄 Заново", "💬 С комментарием", "⏸ Отложить", "❌ Отклонить",
    }  # fmt: skip
    async with db.session() as s:
        draft = (await s.scalars(select(Draft))).one()
        item = (await s.scalars(select(SourceItem).where(SourceItem.rubric == "tool"))).one()
        assert draft.has_errors and draft.rubric == "tool" and draft.payload["html"].rstrip().endswith("#tool")
        assert item.status == ItemStatus.USED.value and item.topic_id == draft.topic_id

    tg.press(ADMIN, card, tg.buttons(card)["🧩 Вписать: Мой опыт"])
    await tg.wait_for(lambda: "Напиши текст для блока «Мой опыт»" in tg.sent("sendMessage", ADMIN)[-1]["text"])
    filled = await say_and_wait_card(tg, "Поднял на 4 ГБ RAM, throughput < 20 токенов/с")
    async with db.session() as s:
        draft = (await s.scalars(select(Draft))).one()
    assert "<b>Мой опыт:</b> Поднял на 4 ГБ RAM, throughput &lt; 20 токенов/с" in draft.payload["html"]
    assert not draft.has_errors and draft.version == 2 and "Твой блок вписан" in filled["text"]
    assert "✅ Сейчас" in tg.buttons(filled) and "🧩 Вписать: Мой опыт" not in tg.buttons(filled)

    tg.press(ADMIN, filled, tg.buttons(filled)["✅ Сейчас"])
    await tg.wait_for(lambda: tg.sent("sendMessage", CHANNEL))
    assert "Поднял на 4 ГБ RAM" in tg.sent("sendMessage", CHANNEL)[0]["text"]


async def test_hide_candidate_and_stale_button(tg, collected):
    digest = (await fetch(tg))["Разбор статьи"]
    tg.press(ADMIN, digest, tg.buttons(digest)["🚫 1"])
    await tg.wait_for(lambda: tg.sent("editMessageReplyMarkup"))
    async with db.session() as s:
        item = (await s.scalars(select(SourceItem).where(SourceItem.rubric == "paper"))).one()
        assert item.status == ItemStatus.HIDDEN.value
        assert await service.digest_items(s, tg.cfg, include_offered=True) is not None
        assert "paper" not in await service.digest_items(s, tg.cfg, include_offered=True)

    tg.press(ADMIN, digest, tg.buttons(digest)["✍️ 1"])  # нажатие по устаревшему сообщению
    await tg.wait_for(lambda: any("уже обработан" in c.get("text", "") for c in tg.sent("answerCallbackQuery")))
    async with db.session() as s:
        assert (await s.scalars(select(Draft))).all() == []


async def test_failed_generation_returns_candidate(tg, collected, monkeypatch):
    digest = (await fetch(tg))["Разбор статьи"]

    real, failures = generator._generate_payload, []

    async def flaky(*args, **kwargs):
        if not failures:
            failures.append(1)
            raise generator.GenerationFailed("модель недоступна")
        return await real(*args, **kwargs)

    monkeypatch.setattr(generator, "_generate_payload", flaky)
    tg.press(ADMIN, digest, tg.buttons(digest)["✍️ 1"])
    await tg.wait_for(lambda: "Не получилось подготовить черновик" in tg.sent("sendMessage", ADMIN)[-1]["text"])
    async with db.session() as s:
        item = (await s.scalars(select(SourceItem).where(SourceItem.rubric == "paper"))).one()
        topic = await s.get(Topic, item.topic_id)
        assert item.status == ItemStatus.OFFERED.value and topic.status == TopicStatus.NEW.value
        assert (await s.scalars(select(Draft))).all() == []
    assert tg.buttons(tg.messages[digest["message_id"]])  # кнопки остались: можно повторить

    await press_and_wait_card(tg, digest, "✍️ 1")  # вторая попытка проходит
    async with db.session() as s:
        assert len((await s.scalars(select(Draft))).all()) == 1


async def test_feed_rubrics_are_not_generated_from_backlog(tg):
    for cmd in ("/gen paper", "/gen tool vLLM", "/add release Новая модель"):
        tg.user_message(ADMIN, cmd)
        await tg.wait_for(lambda: "по материалам из источников" in last_text(tg))
        tg.calls.clear()
    tg.user_message(ADMIN, "/topics")
    await tg.wait_for(lambda: "бэклоге" in last_text(tg))
    text = last_text(tg)
    assert "Понятие недели" in text and "Разбор статьи" not in text
    async with db.session() as s:
        assert (await s.scalars(select(Draft))).all() == []


async def test_rubric_without_slot_has_no_schedule_button(tg, collected):
    moderation.set_slotted_rubrics({"concept", "paper", "tool"})  # у release слота нет
    digests = await fetch(tg)
    release, paper = digests["Релиз с разбором"], digests["Разбор статьи"]

    paper_card = await press_and_wait_card(tg, paper, "✍️ 1")
    assert "🕒 В расписание" in tg.buttons(paper_card)

    release_card = await press_and_wait_card(tg, release, "✍️ 1")
    assert "🧩 Вписать: Моё мнение" in tg.buttons(release_card)
    tg.press(ADMIN, release_card, tg.buttons(release_card)["🧩 Вписать: Моё мнение"])
    await tg.wait_for(lambda: "Напиши текст для блока «Моё мнение»" in last_text(tg))
    filled = await say_and_wait_card(tg, "Брать стоит, если нужен длинный контекст.")
    assert "✅ Сейчас" in tg.buttons(filled) and "🕒 В расписание" not in tg.buttons(filled)

    async with db.session() as s:
        draft = (await s.scalars(select(Draft).where(Draft.rubric == "release"))).one()
        with pytest.raises(moderation.NotAllowed, match="нет слота"):
            await moderation.approve(s, draft)


async def test_slot_with_several_rubrics_publishes_oldest_approved(tg, collected, settings):
    paper = (await fetch(tg))["Разбор статьи"]
    card = await press_and_wait_card(tg, paper, "✍️ 1")
    tg.press(ADMIN, card, tg.buttons(card)["🕒 В расписание"])
    await tg.wait_for(lambda: tg.sent("editMessageText"))
    async with db.session() as s:
        concept = await generator.generate_draft(s, tg.llm, settings, "concept")
        await moderation.approve(s, concept)

    await publish_slot_job(tg.bot, settings, ("tool", "paper"))
    posted = tg.sent("sendMessage", CHANNEL)
    assert len(posted) == 1 and "#paper" in posted[0]["text"]
    assert "Разбор статьи" in tg.sent("sendMessage", ADMIN)[-1]["text"]
    async with db.session() as s:
        statuses = dict((await s.execute(select(Draft.rubric, Draft.status))).all())
    assert statuses == {"paper": DraftStatus.PUBLISHED.value, "concept": DraftStatus.APPROVED.value}

    await publish_slot_job(tg.bot, settings, ("tool", "paper"))  # больше одобренных в этих рубриках нет
    assert len(tg.sent("sendMessage", CHANNEL)) == 1
    assert "Инструмент недели / Разбор статьи" in tg.sent("sendMessage", ADMIN)[-1]["text"]


async def test_sources_job_sends_digest_and_reports_errors(tg, collected, settings):
    await sources_job(tg.bot, settings, tg.llm, tg.cfg)
    texts = [m["text"] for m in tg.sent("sendMessage", ADMIN)]
    assert any(t.startswith("Источники не ответили") and "HTTP 500" in t for t in texts)
    assert sum(t.startswith("<b>Кандидаты:") for t in texts) == 3
    assert tg.sent("sendMessage", CHANNEL) == []

    await sources_job(tg.bot, settings, tg.llm, tg.cfg)  # на следующий день тех же кандидатов не шлёт
    assert sum(m["text"].startswith("<b>Кандидаты:") for m in tg.sent("sendMessage", ADMIN)) == 3


async def test_thin_source_is_refused(tg, collected, monkeypatch):
    async def thin(item):
        return "Одна строка анонса.", "только краткое описание"

    monkeypatch.setattr(service, "grounding_for", thin)
    digest = (await fetch(tg))["Релиз с разбором"]
    tg.press(ADMIN, digest, tg.buttons(digest)["✍️ 1"])
    await tg.wait_for(lambda: "источник не отдал текст" in last_text(tg))
    async with db.session() as s:
        assert (await s.scalars(select(Draft))).all() == []
        item = (await s.scalars(select(SourceItem).where(SourceItem.rubric == "release"))).one()
        assert item.status == ItemStatus.OFFERED.value
    assert tg.llm.calls[-1][1].startswith("Рубрика:")  # до модели дошла только оценка, генерация не запускалась
