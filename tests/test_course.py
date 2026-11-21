"""Курс «ML с нуля»: программа, порядок уроков, ссылка на предыдущий урок и оглавление в канале."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import course, db, generator, moderation
from app.models import Draft, DraftStatus, KeyValue, Topic, TopicStatus
from app.rubrics import build_system_prompt
from app.scheduler import Schedule, Slot, prepare_job, publish_slot_job
from tests.conftest import ADMIN, CHANNEL
from tests.test_sources_flow import last_text, say_and_wait_card

LESSON_1 = "Урок 1. Что такое машинное обучение"
LESSON_2 = "Урок 2. Данные, признаки и разметка"


async def course_topics(status: str | None = None) -> list[str]:
    async with db.session() as s:
        query = select(Topic).where(Topic.rubric == "course")
        topics = (await s.scalars(query if status is None else query.where(Topic.status == status))).all()
    return sorted((t.title for t in topics), key=course.lesson_number)


def test_program_is_consistent():
    program = course.load_course()
    lessons = program.lessons
    assert [x.n for x in lessons] == list(range(1, len(lessons) + 1)) and len(lessons) >= 20
    assert all(x.covers.startswith("- ") and len(x.title) <= course.TITLE_LIMIT for x in lessons)
    assert course.lesson_number(lessons[6].topic_title) == 7 and course.lesson_number("Эмбеддинг") is None
    block = course.prompt_block(program, lessons[1])
    assert f"Уже пройдено: {LESSON_1}" in block and "Следующий урок: Урок 3." in block and "Впереди: Урок 4." in block
    assert "ничего, это первый урок" in course.prompt_block(program, lessons[0])
    assert "это последний урок курса" in course.prompt_block(program, lessons[-1])


def test_program_rejects_broken_numbering(tmp_path):
    path = tmp_path / "course.yaml"
    path.write_text(
        "blocks:\n  - title: A\n    lessons:\n      - {n: 2, title: Б, covers: '- x'}\n      - {n: 2, title: В, covers: '- y'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="номера должны возрастать"):
        course.load_course(path)


async def test_sync_follows_the_program(database):
    assert (await course_topics())[:2] == [LESSON_1, LESSON_2]
    program = course.load_course()
    shorter = course.Course(program.title, program.intro, (program.blocks[0],))
    async with db.session() as s:
        used = await s.scalar(select(Topic).where(Topic.title == "Урок 7. Линейная регрессия"))
        used.status = TopicStatus.USED.value  # по уроку уже есть черновик: его программа не трогает
        await s.commit()
        assert await course.sync(s, "course", shorter) == 0
    assert len(await course_topics(TopicStatus.NEW.value)) == 6  # остались уроки первого блока
    assert "Урок 7. Линейная регрессия" in await course_topics(TopicStatus.USED.value)
    async with db.session() as s:
        await course.sync(s, "course")  # уроки вернули в программу — они снова в очереди
    assert len(await course_topics(TopicStatus.NEW.value)) == len(program.lessons) - 1


def test_toc_links_published_lessons_only():
    html = course.toc_html(
        course.load_course(), {1: "https://t.me/Easy_ML/10"}, brand_emoji="🧠", hashtag="#EML_course"
    )
    assert html.startswith("<b>🧠 ML с нуля: все уроки</b>")
    assert '1. <a href="https://t.me/Easy_ML/10">Что такое машинное обучение</a>' in html
    assert "\n2. Данные, признаки и разметка" in html and "<b>Нейросети</b>" in html
    assert html.endswith("#EML_course") and len(html) < 4096


async def test_lessons_go_in_order_with_link_to_previous_and_toc(tg, llm):
    card = await say_and_wait_card(tg, "/gen course")
    assert LESSON_1 in card["text"] and "ML с нуля" in card["text"] and "ещё не опубликован" not in card["text"]
    prompt = llm.calls[-1][1]
    assert (
        f"Тема: {LESSON_1}" in prompt and "<lesson>" in prompt and "Обычная программа: правила пишет человек" in prompt
    )
    assert f"Следующий урок: {LESSON_2}" in prompt
    assert "<material>" not in prompt  # фактчек для уроков не запускается
    async with db.session() as s:
        first = (await s.scalars(select(Draft))).one()
    assert first.payload["cover"] == LESSON_1 and "Предыдущий урок" not in first.payload["html"]
    assert first.payload["html"].rstrip().endswith("#EML_course")

    tg.press(ADMIN, card, tg.buttons(card)["✅ Сейчас"])
    await tg.wait_for(lambda: tg.sent("pinChatMessage"))
    toc = tg.sent("sendMessage", CHANNEL)[1]  # первым ушёл сам урок, вторым — оглавление
    async with db.session() as s:
        lesson_id = (await s.get(Draft, first.id)).channel_message_id
        toc_id = (await s.get(KeyValue, "toc:course")).value
    lesson_url = f"https://t.me/{CHANNEL[1:]}/{lesson_id}"
    assert f'1. <a href="{lesson_url}">Что такое машинное обучение</a>' in toc["text"]
    assert tg.messages[int(toc_id)]["text"] == toc["text"] and int(toc_id) != lesson_id
    assert tg.sent("pinChatMessage")[0]["message_id"] == toc_id

    second_card = await say_and_wait_card(tg, "/gen course")
    assert LESSON_2 in second_card["text"]
    async with db.session() as s:
        second = (await s.scalars(select(Draft).order_by(Draft.id.desc()))).first()
    assert f'Предыдущий урок: <a href="{lesson_url}">{LESSON_1}</a>\n#EML_course' in second.payload["html"]

    tg.press(ADMIN, second_card, tg.buttons(second_card)["✅ Сейчас"])
    await tg.wait_for(lambda: tg.sent("editMessageText", CHANNEL))
    edited = tg.sent("editMessageText", CHANNEL)[-1]
    assert edited["message_id"] == toc_id and "2. <a href=" in edited["text"]
    assert len(tg.sent("pinChatMessage")) == 1  # оглавление одно: оно правится, а не публикуется заново


async def test_lesson_without_published_predecessor_gets_a_warning(tg):
    await say_and_wait_card(tg, "/gen course")
    card = await say_and_wait_card(tg, "/gen course")  # первый урок ещё не вышел, а второй уже пишется
    assert LESSON_2 in card["text"] and "ещё не опубликован" in card["text"]
    assert "✅ Сейчас" in tg.buttons(card)  # это предупреждение, а не запрет


async def test_rejected_draft_returns_the_lesson_to_the_queue(tg):
    card = await say_and_wait_card(tg, "/gen course")
    tg.press(ADMIN, card, tg.buttons(card)["❌ Отклонить"])
    await tg.wait_for(lambda: "отклонён" in tg.messages[card["message_id"]]["text"])
    assert LESSON_1 in await course_topics(TopicStatus.NEW.value)
    again = await say_and_wait_card(tg, "/gen course")
    assert LESSON_1 in again["text"]
    async with db.session() as s:
        statuses = [d.status for d in (await s.scalars(select(Draft).order_by(Draft.id))).all()]
    assert statuses == [DraftStatus.REJECTED.value, DraftStatus.PENDING.value]


async def test_own_topics_are_not_accepted_into_the_course(tg):
    for command in ("/add course Квантовые нейросети", "/gen course Квантовые нейросети"):
        tg.user_message(ADMIN, command)
        await tg.wait_for(lambda: "идут по программе" in last_text(tg))
    assert "Квантовые нейросети" not in " ".join(await course_topics())
    async with db.session() as s:
        assert (await s.scalars(select(Draft))).all() == []


async def test_course_is_finished_when_the_program_runs_out(database, llm, settings):
    async with db.session() as s:
        for topic in (await s.scalars(select(Topic).where(Topic.rubric == "course"))).all():
            topic.status = TopicStatus.USED.value
        await s.commit()
        with pytest.raises(generator.NoTopics):
            await generator.generate_draft(s, llm, settings, "course")


# --- блок автора выключен -------------------------------------------------------------------


def test_author_block_is_off_by_default_and_can_be_enabled(author_blocks_off_then_on):
    off, on = author_blocks_off_then_on
    assert "{Мой опыт}" not in off["tool"] and "{Моё мнение}" not in off["release"]
    assert "<b>Мой опыт:</b> {Мой опыт}" in on["tool"] and "<b>Моё мнение:</b> {Моё мнение}" in on["release"]


@pytest.fixture
def author_blocks_off_then_on(request):
    off = {key: build_system_prompt(key, "🧠") for key in ("tool", "release")}
    request.getfixturevalue("author_blocks")
    return off, {key: build_system_prompt(key, "🧠") for key in ("tool", "release")}


# --- квиз «Проверь себя» к уроку -------------------------------------------------------------


async def quiz_drafts() -> list[Draft]:
    async with db.session() as s:
        return list((await s.scalars(select(Draft).where(Draft.rubric == "course_quiz").order_by(Draft.id))).all())


async def test_approved_lesson_gets_a_quiz_that_waits_for_the_lesson(tg, llm, settings):
    moderation.set_slotted_rubrics({"course", "course_quiz"})
    lesson_card = await say_and_wait_card(tg, "/gen course")
    tg.press(ADMIN, lesson_card, tg.buttons(lesson_card)["🕒 В расписание"])
    await tg.wait_for(
        lambda: any("Квиз к уроку: выйдет вечером" in m.get("text", "") for m in tg.sent("sendMessage", ADMIN))
    )
    assert any("Готовлю квиз к этому уроку" in m["text"] for m in tg.sent("sendMessage", ADMIN))

    (quiz,) = await quiz_drafts()
    assert quiz.payload["kind"] == "quiz" and quiz.payload["question"].startswith("Урок 1, проверь себя. ")
    prompt = next(user for system, user in llm.calls if "Рубрика «Проверь себя»" in system)
    assert f"Урок: {LESSON_1}" in prompt and "Обычная программа: правила пишет человек" in prompt
    assert "Текст урока, который прочитал читатель:" in prompt and "<lesson>" in prompt
    async with db.session() as s:
        assert (await s.get(Topic, quiz.topic_id)).title == f"Проверь себя: {LESSON_1}"

    quiz_card = tg.last_card(ADMIN)
    tg.press(ADMIN, quiz_card, tg.buttons(quiz_card)["🕒 В расписание"])
    await tg.wait_for(lambda: "в очереди на публикацию" in tg.messages[quiz_card["message_id"]]["text"])

    # вечерний слот квиза пришёл раньше, чем вышел урок: квиз ждёт
    await publish_slot_job(tg.bot, settings, "course_quiz")
    assert tg.sent("sendPoll", CHANNEL) == []
    await tg.wait_for(lambda: any("Слот «Проверь себя» пропущен" in m["text"] for m in tg.sent("sendMessage", ADMIN)))

    await publish_slot_job(tg.bot, settings, "course")  # утренний слот: вышел урок
    await publish_slot_job(tg.bot, settings, "course_quiz")  # вечерний: теперь выходит и квиз
    (poll,) = tg.sent("sendPoll", CHANNEL)
    assert poll["question"].startswith("Урок 1, проверь себя. ")
    assert len(await quiz_drafts()) == 1  # публикация урока второй квиз не породила


async def test_prepare_job_makes_the_quiz_if_it_was_missed(tg, llm, settings):
    schedule = Schedule(18, 0, 1, (Slot("tue", 19, 0, ("course_quiz",)),))
    await prepare_job(tg.bot, settings, llm, schedule)  # урока ещё нет — квиза тоже, и бот об этом молчит
    assert await quiz_drafts() == [] and tg.sent("sendMessage", ADMIN) == []

    async with db.session() as s:
        lesson = await generator.generate_draft(s, llm, settings, "course")
        lesson.status = DraftStatus.APPROVED.value
        await s.commit()
    await prepare_job(tg.bot, settings, llm, schedule)
    (quiz,) = await quiz_drafts()
    assert quiz.status == DraftStatus.PENDING.value
    await prepare_job(tg.bot, settings, llm, schedule)  # повторно не готовит
    assert len(await quiz_drafts()) == 1


async def test_rejected_quiz_is_not_made_again_and_cannot_be_requested(tg, llm, settings):
    schedule = Schedule(18, 0, 1, (Slot("tue", 19, 0, ("course_quiz",)),))
    async with db.session() as s:
        lesson = await generator.generate_draft(s, llm, settings, "course")
        lesson.status = DraftStatus.APPROVED.value
        await s.commit()
    await prepare_job(tg.bot, settings, llm, schedule)
    card = tg.last_card(ADMIN)
    tg.press(ADMIN, card, tg.buttons(card)["❌ Отклонить"])
    await tg.wait_for(lambda: "отклонён" in tg.messages[card["message_id"]]["text"])
    await prepare_job(tg.bot, settings, llm, schedule)
    assert len(await quiz_drafts()) == 1  # к этому уроку квиза не будет

    tg.user_message(ADMIN, "/gen course_quiz")
    await tg.wait_for(lambda: "готовится сам" in last_text(tg))
