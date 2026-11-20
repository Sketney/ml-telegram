from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app import db, generator, moderation
from app.generator import SHOW_ANSWER_OPTION, GenerationFailed, NoTopics, parse_json_object
from app.models import Draft, DraftStatus, Topic, TopicStatus
from app.rubrics import build_system_prompt, load_rubrics


class ScriptedLLM:
    """Отдаёт заранее заданные ответы по очереди и запоминает промпты."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def complete(self, system: str, user: str, *, cheap: bool = False) -> str:
        self.prompts.append(user)
        return self.answers.pop(0)


def test_parse_json_object_tolerates_fences_and_chatter():
    assert parse_json_object('```json\n{"html": "x"}\n```') == {"html": "x"}
    assert parse_json_object('Вот ответ:\n{"html": "a {b} c"}\nГотово.') == {"html": "a {b} c"}
    with pytest.raises(GenerationFailed):
        parse_json_object("просто текст")
    with pytest.raises(GenerationFailed):
        parse_json_object("{не json}")


def test_system_prompt_has_styleguide_rubric_and_brand_emoji():
    for key in load_rubrics():
        prompt = build_system_prompt(key, "🔹")
        assert "Стайлгайд" in prompt
        assert "Формат ответа" in prompt
        assert "{E}" not in prompt
        assert "<!--" not in prompt
    assert "🔹" in build_system_prompt("concept", "🔹")


def test_examples_are_included_when_present(tmp_path):
    (tmp_path / "rubrics").mkdir()
    (tmp_path / "examples" / "concept").mkdir(parents=True)
    (tmp_path / "styleguide.md").write_text("СТАЙЛ", encoding="utf-8")
    (tmp_path / "rubrics" / "concept.md").write_text("РУБРИКА {E}", encoding="utf-8")
    assert "Эталонные посты" not in build_system_prompt("concept", "*", tmp_path)
    (tmp_path / "examples" / "concept" / "01.md").write_text("<b>мой пост</b>", encoding="utf-8")
    prompt = build_system_prompt("concept", "*", tmp_path)
    assert "Эталонные посты" in prompt and "<b>мой пост</b>" in prompt and "РУБРИКА *" in prompt


async def test_backlog_sync_is_idempotent(database):
    async with db.session() as s:
        before = await generator.backlog_counts(s)
        assert all(n > 0 for n in before.values()), before
        assert await generator.sync_backlog(s) == 0
        assert await generator.backlog_counts(s) == before


async def test_generate_post_takes_oldest_topic_and_adds_hashtag(database, settings, llm):
    async with db.session() as s:
        first = await generator.next_topic(s, "concept")
        draft = await generator.generate_draft(s, llm, settings, "concept")
        assert draft.status == DraftStatus.PENDING.value
        assert draft.topic_id == first.id
        assert draft.payload["kind"] == "post"
        assert draft.payload["html"].rstrip().endswith("#concept")
        assert not draft.has_errors
        assert (await s.get(Topic, first.id)).status == TopicStatus.USED.value
        assert (await generator.next_topic(s, "concept")).id != first.id


async def test_signature_goes_after_hashtag(database, settings, llm):
    settings.channel_signature = '<a href="https://t.me/x">Канал</a>'
    async with db.session() as s:
        draft = await generator.generate_draft(s, llm, settings, "history")
    assert draft.payload["html"].endswith('#history\n<a href="https://t.me/x">Канал</a>')
    assert not draft.has_errors


async def test_generate_quiz_appends_show_answer_option(database, settings, llm):
    async with db.session() as s:
        draft = await generator.generate_draft(s, llm, settings, "quiz")
    p = draft.payload
    assert p["kind"] == "quiz" and p["options"][-1] == SHOW_ANSWER_OPTION and len(p["options"]) == 5
    assert p["options"][p["correct_index"]] != SHOW_ANSWER_OPTION
    assert not draft.has_errors

    settings.quiz_show_answer_option = False
    async with db.session() as s:
        draft = await generator.generate_draft(s, llm, settings, "quiz")
    assert len(draft.payload["options"]) == 4


async def test_validation_errors_trigger_one_retry_with_feedback(database, settings):
    bad = json.dumps({"html": "<p>плохой тег</p>"})
    good = json.dumps({"html": "<b>хорошо</b>"})
    scripted = ScriptedLLM(bad, good)
    async with db.session() as s:
        draft = await generator.generate_draft(s, scripted, settings, "concept")
    assert not draft.has_errors
    assert len(scripted.prompts) == 2
    assert "<p>" in scripted.prompts[1] and "исправь" in scripted.prompts[1]


async def test_errors_survive_if_retry_does_not_help(database, settings):
    bad = json.dumps({"html": "<p>плохой тег</p>"})
    async with db.session() as s:
        draft = await generator.generate_draft(s, ScriptedLLM(bad, bad), settings, "concept")
        assert draft.has_errors
        with pytest.raises(moderation.NotAllowed):
            await moderation.approve(s, draft)


async def test_unparseable_answer_raises_and_keeps_topic(database, settings):
    async with db.session() as s:
        topic_id = (await generator.next_topic(s, "concept")).id
        with pytest.raises(GenerationFailed):
            await generator.generate_draft(s, ScriptedLLM("чушь", "опять чушь"), settings, "concept")
    async with db.session() as s:  # как увидит базу следующий запрос
        assert (await s.get(Topic, topic_id)).status == TopicStatus.NEW.value
        assert (await s.scalars(select(Draft))).all() == []


async def test_no_topics(database, settings, llm):
    async with db.session() as s:
        for t in (await s.scalars(select(Topic).where(Topic.rubric == "history"))).all():
            t.status = TopicStatus.USED.value
        await s.commit()
        with pytest.raises(NoTopics):
            await generator.generate_draft(s, llm, settings, "history")


async def test_regenerate_with_comment_shows_previous_version_without_service_option(database, settings):
    quiz = {"question": "Вопрос?", "options": ["а", "б", "в", "г"], "correct_index": 1, "explanation": "потому что"}
    scripted = ScriptedLLM(json.dumps(quiz), json.dumps({**quiz, "question": "Новый вопрос?"}))
    async with db.session() as s:
        draft = await generator.generate_draft(s, scripted, settings, "quiz")
        await moderation.approve(s, draft)
        await generator.regenerate_draft(s, scripted, settings, draft, comment="сделай проще")
        assert draft.version == 2
        assert draft.status == DraftStatus.PENDING.value  # новая версия снова требует одобрения
        assert draft.payload["question"] == "Новый вопрос?"
    assert "сделай проще" in scripted.prompts[1]
    assert "Вопрос?" in scripted.prompts[1]
    assert SHOW_ANSWER_OPTION not in scripted.prompts[1]


async def test_add_topic_dedup(database):
    async with db.session() as s:
        assert await generator.add_topic(s, "concept", "  Совсем новая тема ") is not None
        assert await generator.add_topic(s, "concept", "Совсем новая тема") is None
        with pytest.raises(KeyError):
            await generator.add_topic(s, "nope", "x")
