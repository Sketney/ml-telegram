"""Сквозные сценарии: настоящий диспетчер aiogram против мок-сервера Telegram."""

from __future__ import annotations

from sqlalchemy import select

from app import db, moderation
from app.models import Draft, DraftStatus, Topic, TopicStatus
from app.scheduler import Schedule, Slot, prepare_job, publish_slot_job
from tests.conftest import ADMIN, CHANNEL

STRANGER = 777


async def only_draft() -> Draft:
    async with db.session() as s:
        drafts = (await s.scalars(select(Draft))).all()
    assert len(drafts) == 1, drafts
    return drafts[0]


async def gen(tg, rubric: str) -> dict:
    """Шлёт /gen и возвращает карточку с кнопками."""
    before = len(tg.sent("sendMessage", ADMIN))
    tg.user_message(ADMIN, f"/gen {rubric}")
    await tg.wait_for(lambda: len(tg.sent("sendMessage", ADMIN)) > before and tg.calls[-1][1].get("reply_markup"))
    return tg.last_card(ADMIN)


async def test_stranger_gets_no_reply(tg):
    tg.user_message(STRANGER, "/gen concept")
    tg.user_message(ADMIN, "/topics")
    await tg.wait_for(lambda: tg.sent("sendMessage", ADMIN))
    assert tg.sent("sendMessage", STRANGER) == []
    async with db.session() as s:
        assert (await s.scalars(select(Draft))).all() == []


async def test_post_approve_then_published_by_slot(tg, settings):
    card = await gen(tg, "concept")
    preview = tg.sent("sendMessage", ADMIN)[-2]
    assert preview["parse_mode"] == "HTML" and "#понятие" in preview["text"]
    assert "Черновик #1" in card["text"] and "Понятие недели" in card["text"]
    buttons = tg.buttons(card)
    assert set(buttons) == {
        "✅ Сейчас", "🕒 В расписание", "✏️ Править", "🔄 Заново", "💬 С комментарием", "⏸ Отложить", "❌ Отклонить",
    }  # fmt: skip
    assert tg.sent("sendMessage", CHANNEL) == []  # без решения автора в канал ничего не уходит

    tg.press(ADMIN, card, buttons["🕒 В расписание"])
    await tg.wait_for(lambda: tg.sent("editMessageText"))
    assert (await only_draft()).status == DraftStatus.APPROVED.value
    assert "в очереди" in tg.messages[card["message_id"]]["text"]
    assert "🕒 В расписание" not in tg.buttons(tg.messages[card["message_id"]])
    assert tg.sent("sendMessage", CHANNEL) == []

    await publish_slot_job(tg.bot, settings, "concept")
    posted = tg.sent("sendMessage", CHANNEL)
    assert len(posted) == 1 and posted[0]["text"] == preview["text"] and posted[0]["parse_mode"] == "HTML"
    assert posted[0]["link_preview_options"] == {"is_disabled": True}
    draft = await only_draft()
    assert draft.status == DraftStatus.PUBLISHED.value and draft.channel_message_id
    assert "Опубликован черновик #1" in tg.sent("sendMessage", ADMIN)[-1]["text"]

    # второй слот: одобренных больше нет — в канал ничего, автору уведомление
    await publish_slot_job(tg.bot, settings, "concept")
    assert len(tg.sent("sendMessage", CHANNEL)) == 1
    assert "пропущен" in tg.sent("sendMessage", ADMIN)[-1]["text"]


async def test_quiz_publish_now(tg):
    card = await gen(tg, "quiz")
    preview = tg.sent("sendPoll", ADMIN)[0]
    assert preview["type"] == "quiz" and len(preview["options"]) == 5
    assert "✏️ Править" not in tg.buttons(card)

    tg.press(ADMIN, card, tg.buttons(card)["✅ Сейчас"])
    await tg.wait_for(lambda: tg.sent("sendPoll", CHANNEL))
    poll = tg.sent("sendPoll", CHANNEL)[0]
    assert poll["question"] == preview["question"]
    assert poll["correct_option_ids"] == [0]
    assert poll["is_anonymous"] == "true" and poll["explanation"]
    await tg.wait_for(lambda: tg.sent("editMessageText"))
    assert (await only_draft()).status == DraftStatus.PUBLISHED.value
    assert "reply_markup" not in tg.messages[card["message_id"]]  # кнопок под опубликованным нет

    # повторное нажатие по устаревшей карточке не публикует второй раз
    tg.press(ADMIN, card, tg.buttons(card)["✅ Сейчас"])
    await tg.wait_for(lambda: len(tg.sent("answerCallbackQuery")) >= 2)
    assert len(tg.sent("sendPoll", CHANNEL)) == 1
    assert "Нельзя" in tg.sent("answerCallbackQuery")[-1]["text"]


async def test_edit_with_telegram_formatting(tg):
    card = await gen(tg, "history")
    tg.press(ADMIN, card, tg.buttons(card)["✏️ Править"])
    await tg.wait_for(lambda: "Пришли новый текст" in tg.sent("sendMessage", ADMIN)[-1]["text"])

    tg.user_message(ADMIN, "Мой заголовок\nи текст с x < y", entities=[{"type": "bold", "offset": 0, "length": 13}])
    await tg.next_card(ADMIN, card)
    draft = await only_draft()
    assert draft.payload["html"] == "<b>Мой заголовок</b>\nи текст с x &lt; y"
    assert draft.version == 2 and not draft.has_errors
    assert "Текст заменён" in tg.last_card(ADMIN)["text"]


async def test_edit_with_raw_html_and_placeholder_blocks_publication(tg):
    card = await gen(tg, "concept")
    tg.press(ADMIN, card, tg.buttons(card)["✏️ Править"])
    await tg.wait_for(lambda: "Пришли новый текст" in tg.sent("sendMessage", ADMIN)[-1]["text"])
    tg.user_message(ADMIN, "<b>Инструмент</b>\n\n<b>Мой опыт:</b> {Мой опыт}")
    await tg.next_card(ADMIN, card)

    new_card = tg.last_card(ADMIN)
    draft = await only_draft()
    assert draft.payload["html"].startswith("<b>Инструмент</b>") and draft.has_errors
    assert "⛔️" in new_card["text"] and "{Мой опыт}" in new_card["text"]
    assert "✅ Сейчас" not in tg.buttons(new_card) and "🕒 В расписание" not in tg.buttons(new_card)

    # даже если нажать «Сейчас» по старой карточке — в канал не уйдёт
    tg.press(ADMIN, card, tg.buttons(card)["✅ Сейчас"])
    await tg.wait_for(lambda: any("Нельзя" in c.get("text", "") for c in tg.sent("answerCallbackQuery")))
    assert tg.sent("sendMessage", CHANNEL) == []


async def test_regenerate_with_comment(tg, llm):
    card = await gen(tg, "concept")
    tg.press(ADMIN, card, tg.buttons(card)["💬 С комментарием"])
    await tg.wait_for(lambda: "Что поправить" in tg.sent("sendMessage", ADMIN)[-1]["text"])
    tg.user_message(ADMIN, "короче и с примером про nginx")
    await tg.next_card(ADMIN, card)
    assert (await only_draft()).version == 2
    assert "короче и с примером про nginx" in llm.calls[-1][1]
    assert "v2" in tg.last_card(ADMIN)["text"]


async def test_regenerate_retires_old_card(tg):
    card = await gen(tg, "concept")
    tg.press(ADMIN, card, tg.buttons(card)["🔄 Заново"])
    await tg.next_card(ADMIN, card)
    assert "reply_markup" not in tg.messages[card["message_id"]]
    assert "Заменён версией v2" in tg.messages[card["message_id"]]["text"]


async def test_cancel_leaves_draft_untouched(tg):
    card = await gen(tg, "concept")
    before = (await only_draft()).payload
    tg.press(ADMIN, card, tg.buttons(card)["✏️ Править"])
    await tg.wait_for(lambda: "Пришли новый текст" in tg.sent("sendMessage", ADMIN)[-1]["text"])
    tg.user_message(ADMIN, "/cancel")
    await tg.wait_for(lambda: tg.sent("sendMessage", ADMIN)[-1]["text"] == "Отменил.")
    tg.user_message(ADMIN, "случайный текст")
    await tg.wait_for(lambda: "Не понял" in tg.sent("sendMessage", ADMIN)[-1]["text"])
    assert (await only_draft()).payload == before


async def test_reject_skips_topic_and_postpone_keeps_it(tg):
    card = await gen(tg, "concept")
    tg.press(ADMIN, card, tg.buttons(card)["⏸ Отложить"])
    await tg.wait_for(lambda: tg.sent("editMessageText"))
    draft = await only_draft()
    assert draft.status == DraftStatus.POSTPONED.value
    updated = tg.last_card(ADMIN)
    assert updated["message_id"] == card["message_id"] and "⏸ Отложить" not in tg.buttons(updated)

    tg.press(ADMIN, updated, tg.buttons(updated)["❌ Отклонить"])
    await tg.wait_for(lambda: len(tg.sent("editMessageText")) == 2)
    draft = await only_draft()
    assert draft.status == DraftStatus.REJECTED.value
    async with db.session() as s:
        assert (await s.get(Topic, draft.topic_id)).status == TopicStatus.SKIPPED.value
    assert "reply_markup" not in tg.messages[card["message_id"]]


async def test_gen_with_custom_topic_and_add_and_queue(tg):
    tg.user_message(ADMIN, "/gen concept Спекулятивное декодирование")
    await tg.wait_for(lambda: any(m.get("reply_markup") for m in tg.messages.values()))
    assert "Спекулятивное декодирование" in tg.last_card(ADMIN)["text"]

    tg.user_message(ADMIN, "/add history Как появился CUDA")
    await tg.wait_for(lambda: tg.sent("sendMessage", ADMIN)[-1]["text"] == "Добавил в бэклог.")
    tg.user_message(ADMIN, "/add history Как появился CUDA")
    await tg.wait_for(lambda: "уже есть" in tg.sent("sendMessage", ADMIN)[-1]["text"])
    tg.user_message(ADMIN, "/add nope тема")
    await tg.wait_for(lambda: "Формат: /add" in tg.sent("sendMessage", ADMIN)[-1]["text"])

    tg.user_message(ADMIN, "/queue")
    await tg.wait_for(lambda: "/show номер" in tg.sent("sendMessage", ADMIN)[-1]["text"])
    assert (
        "#1 · Понятие недели · Спекулятивное декодирование — ждёт решения" in tg.sent("sendMessage", ADMIN)[-1]["text"]
    )


async def test_publish_failure_keeps_draft_approved(tg, settings):
    card = await gen(tg, "concept")
    tg.fail_next["sendMessage"] = "Bad Request: need administrator rights in the channel chat"
    tg.press(ADMIN, card, tg.buttons(card)["✅ Сейчас"])
    await tg.wait_for(lambda: any("Telegram отклонил" in c.get("text", "") for c in tg.sent("answerCallbackQuery")))
    assert (await only_draft()).status == DraftStatus.PENDING.value  # не помечен опубликованным


async def test_prepare_job_keeps_buffer(tg, settings, llm):
    schedule = Schedule(18, 0, 1, (Slot("mon", 10, 0, "concept"), Slot("thu", 10, 0, "quiz")))
    await prepare_job(tg.bot, settings, llm, schedule)
    async with db.session() as s:
        assert await moderation.count_in_work(s, "concept") == 1
        assert await moderation.count_in_work(s, "quiz") == 1
    assert "Плановый черновик" in tg.last_card(ADMIN)["text"]

    await prepare_job(tg.bot, settings, llm, schedule)  # уже есть что модерировать — новых не делает
    async with db.session() as s:
        assert len((await s.scalars(select(Draft))).all()) == 2
    assert tg.sent("sendMessage", CHANNEL) == [] and tg.sent("sendPoll", CHANNEL) == []


async def test_prepare_job_reports_empty_backlog(tg, settings, llm):
    async with db.session() as s:
        for t in (await s.scalars(select(Topic).where(Topic.rubric == "history"))).all():
            t.status = TopicStatus.USED.value
        await s.commit()
    await prepare_job(tg.bot, settings, llm, Schedule(18, 0, 1, (Slot("fri", 10, 0, "history"),)))
    assert "закончились темы" in tg.sent("sendMessage", ADMIN)[-1]["text"]
