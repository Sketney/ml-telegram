"""Бот модерации: команды автора и кнопки под черновиками."""

from __future__ import annotations

import logging
import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app import db, generator, metrics, moderation
from app import transcribe as stt
from app.bot.cards import STATUS_LABELS, DraftAction, card_text, draft_keyboard, draft_placeholders, send_card
from app.bot.digest import ItemAction, send_digest, without_item
from app.config import Settings
from app.llm import LLM
from app.models import Draft, ItemStatus, SourceItem, Topic, TopicStatus
from app.rubrics import load_rubrics
from app.sources import service as sources
from app.sources.collectors import SourcesConfig

log = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"</?(b|strong|i|em|u|s|a|code|pre|blockquote)\b", re.IGNORECASE)


class EditText(StatesGroup):
    waiting = State()


class RegenComment(StatesGroup):
    waiting = State()


class FillPlaceholder(StatesGroup):
    waiting = State()


class NotesInput(StatesGroup):
    collecting = State()


MAX_NOTES_FILE = 200_000  # байт: заметки — это текст, а не архив логов
VOICE_RUBRIC = "practice"  # голосовое без команды — история с работы


def rubric_list(source: str = "backlog") -> str:
    return "\n".join(
        f"<code>{key}</code> — {escape(r.title)}" for key, r in load_rubrics().items() if r.source == source
    )


FEED_HINT = "Эта рубрика пишется по материалам из источников: выбери кандидата в /digest."
NOTES_HINT = "Эта рубрика пишется по твоим заметкам: /note {rubric}, затем текст, файл или голосовое."
GROUNDED_HINT = (
    "В этой рубрике у темы должны быть проверенные факты и ссылка на источник:\n"
    "/add {rubric} заголовок | факты своими словами: кто, когда, что произошло. https://ссылка-на-источник"
)


WEAK_SOURCE_HINT = (
    "Википедия как источник не принимается. Дай ссылку на первоисточник или издание с редакцией: статью самих "
    "авторов, блог компании или лаборатории, музей, научный журнал, профильное издание. "
    "И оставь в фактах только то, что есть на этой странице."
)
SERIES_HINT = (
    "Уроки курса идут по программе из data/course.yaml, свою тему сюда добавить нельзя.\n"
    "/gen {rubric} — черновик следующего урока."
)


def split_facts(text: str) -> tuple[str, str]:
    """«заголовок | факты и ссылка» → (заголовок, факты)."""
    title, _, facts = text.partition("|")
    return title.strip(), facts.strip()


def other_source_hint(rubric: str) -> str | None:
    """Подсказка для рубрик, которые не берут темы из бэклога."""
    source = load_rubrics()[rubric].source
    return FEED_HINT if source == "feed" else NOTES_HINT.format(rubric=rubric) if source == "notes" else None


HELP = (
    "Я готовлю черновики для канала и публикую только то, что ты одобрил.\n\n"
    "<b>Команды</b>\n"
    "/gen <code>рубрика</code> — черновик по следующей теме из бэклога\n"
    "/gen <code>рубрика тема</code> — черновик на свою тему\n"
    "/add <code>рубрика тема</code> — добавить тему в бэклог\n"
    "/topics — сколько тем осталось\n"
    "/queue — черновики в работе\n"
    "/show <code>номер</code> — показать черновик ещё раз\n"
    "/digest — кандидаты из источников: статьи, релизы, инструменты\n"
    "/fetch — собрать источники сейчас\n"
    "/note <code>рубрика</code> — пост по твоим заметкам: текст, файл или голосовое, затем /done\n"
    "/stats — просмотры постов по рубрикам\n"
    "/cancel — отменить ввод\n\n"
    "Голосовое без команды я расшифрую и превращу в «Историю с работы».\n\n"
    "<b>Рубрики по бэклогу</b>\n{rubrics}\n\n"
    "<b>Рубрики по источникам</b> (через /digest)\n{feed_rubrics}\n\n"
    "<b>Рубрики по заметкам</b> (через /note)\n{notes_rubrics}"
)


def build_router(admin_id: int) -> Router:
    router = Router(name="moderation")
    router.message.filter(F.from_user.id == admin_id)
    router.callback_query.filter(F.from_user.id == admin_id)

    # --- команды -----------------------------------------------------------

    @router.message(Command("start", "help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            HELP.format(rubrics=rubric_list(), feed_rubrics=rubric_list("feed"), notes_rubrics=rubric_list("notes")),
            parse_mode="HTML",
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        was = await state.get_state()
        await state.clear()
        await message.answer("Отменил." if was else "Отменять нечего.")

    @router.message(Command("topics"))
    async def cmd_topics(message: Message) -> None:
        async with db.session() as s:
            counts = await generator.backlog_counts(s)
        rubrics = load_rubrics()
        lines = [f"{escape(rubrics[k].title)}: {n}" for k, n in counts.items()]
        await message.answer("<b>Новых тем в бэклоге</b>\n" + "\n".join(lines), parse_mode="HTML")

    @router.message(Command("add"))
    async def cmd_add(message: Message, command: CommandObject) -> None:
        rubric, _, title = (command.args or "").partition(" ")
        if rubric not in load_rubrics() or not title.strip():
            await message.answer("Формат: /add <code>рубрика тема</code>\n\n" + rubric_list(), parse_mode="HTML")
            return
        if hint := other_source_hint(rubric):
            await message.answer(hint)
            return
        title, facts = split_facts(title) if load_rubrics()[rubric].grounded else (title, "")
        async with db.session() as s:
            try:
                topic = await generator.add_topic(s, rubric, title, facts)
            except generator.SeriesTopic:
                await message.answer(SERIES_HINT.format(rubric=rubric))
                return
            except generator.WeakSource:
                await message.answer(WEAK_SOURCE_HINT)
                return
            except ValueError:
                await message.answer(GROUNDED_HINT.format(rubric=rubric))
                return
        await message.answer("Добавил в бэклог." if topic else "Такая тема в этой рубрике уже есть.")

    @router.message(Command("queue"))
    async def cmd_queue(message: Message) -> None:
        async with db.session() as s:
            drafts = await moderation.list_queue(s)
            if not drafts:
                await message.answer("В работе пусто. /gen рубрика — сделать черновик.")
                return
            rubrics = load_rubrics()
            lines = []
            for d in drafts:
                topic = await s.get(Topic, d.topic_id) if d.topic_id else None
                title = f" · {escape(topic.title)}" if topic else ""
                lines.append(
                    f"#{d.id} · {escape(rubrics[d.rubric].title)}{title} — {STATUS_LABELS.get(d.status, d.status)}"
                )
        await message.answer("\n".join(lines) + "\n\n/show номер — открыть черновик", parse_mode="HTML")

    @router.message(Command("show"))
    async def cmd_show(message: Message, command: CommandObject, bot: Bot) -> None:
        arg = (command.args or "").strip().lstrip("#")
        if not arg.isdigit():
            await message.answer("Формат: /show номер")
            return
        async with db.session() as s:
            draft = await s.get(Draft, int(arg))
            if draft is None:
                await message.answer("Такого черновика нет.")
                return
            await send_card(bot, message.chat.id, s, draft)

    @router.message(Command("gen"))
    async def cmd_gen(message: Message, command: CommandObject, bot: Bot, settings: Settings, llm: LLM) -> None:
        rubric, _, title = (command.args or "").partition(" ")
        title = title.strip()
        if rubric not in load_rubrics():
            await message.answer("Формат: /gen <code>рубрика [тема]</code>\n\n" + rubric_list(), parse_mode="HTML")
            return
        if hint := other_source_hint(rubric):
            await message.answer(hint)
            return
        await message.answer("Готовлю черновик…")
        async with db.session() as s:
            try:
                topic = None
                if title:
                    title, facts = split_facts(title) if load_rubrics()[rubric].grounded else (title, "")
                    try:
                        topic = await generator.add_topic(s, rubric, title, facts)
                    except generator.SeriesTopic:
                        await message.answer(SERIES_HINT.format(rubric=rubric))
                        return
                    except generator.WeakSource:
                        await message.answer(WEAK_SOURCE_HINT)
                        return
                    except ValueError:
                        await message.answer(GROUNDED_HINT.format(rubric=rubric))
                        return
                    if topic is None:  # тема уже в бэклоге — берём её, если по ней ещё не писали
                        topic = await s.scalar(select(Topic).where(Topic.rubric == rubric, Topic.title == title))
                        if topic.status != TopicStatus.NEW.value:
                            await message.answer("По этой теме черновик уже был. Смотри /queue.")
                            return
                draft = await generator.generate_draft(s, llm, settings, rubric, topic)
            except generator.NoTopics:
                if load_rubrics()[rubric].series:
                    await message.answer("Все уроки программы уже написаны. Новые добавляются в data/course.yaml.")
                else:
                    await message.answer(f"В бэклоге рубрики закончились темы. Добавь: /add {rubric} тема")
                return
            except generator.TopicTaken:
                await message.answer("По этой теме черновик уже готовится или готов. Смотри /queue.")
                return
            except Exception as e:  # сеть, ключ, лимиты, ответ модели — автору нужна причина
                log.exception("генерация не удалась")
                await message.answer(f"Не получилось сгенерировать: {e}")
                return
            await send_card(bot, message.chat.id, s, draft)

    # --- заметки автора: эксперимент, история с работы ------------------------

    async def draft_from_notes(
        message: Message, bot: Bot, settings: Settings, llm: LLM, rubric: str, notes: str
    ) -> None:
        title = load_rubrics()[rubric].title
        await message.answer(f"Готовлю «{title}» по твоим заметкам…")
        async with db.session() as s:
            try:
                topic = await generator.topic_from_notes(s, rubric, notes)
                draft = await generator.generate_draft(s, llm, settings, rubric, topic)
            except Exception as e:
                log.exception("черновик по заметкам не удался")
                await message.answer(f"Не получилось подготовить черновик: {e}")
                return
            await send_card(bot, message.chat.id, s, draft)

    async def voice_text(message: Message, bot: Bot, settings: Settings) -> str | None:
        """Расшифровка голосового или аудио; None — не получилось, автору уже отвечено."""
        media = message.voice or message.audio
        await message.answer("Расшифровываю запись, это может занять до минуты…")
        try:
            buffer = await bot.download(media.file_id)
            return await stt.transcribe(buffer.read(), settings)
        except stt.TranscribeUnavailable as e:
            await message.answer(f"Не получилось расшифровать: {e}. Можно прислать то же самое текстом.")
        except TelegramAPIError as e:
            await message.answer(f"Не получилось скачать запись: {e}")
        return None

    async def document_text(message: Message, bot: Bot) -> str | None:
        doc = message.document
        if (doc.file_size or 0) > MAX_NOTES_FILE:
            await message.answer(f"Файл больше {MAX_NOTES_FILE // 1000} КБ. Пришли заметки, а не полный лог.")
            return None
        try:
            return (await bot.download(doc.file_id)).read().decode("utf-8")
        except UnicodeDecodeError:
            await message.answer("Не могу прочитать файл как текст. Подойдут .txt и .md в UTF-8.")
        except TelegramAPIError as e:
            await message.answer(f"Не получилось скачать файл: {e}")
        return None

    @router.message(Command("note"))
    async def cmd_note(message: Message, command: CommandObject, state: FSMContext) -> None:
        rubric = (command.args or "").strip().split(" ")[0]
        if rubric not in load_rubrics() or load_rubrics()[rubric].source != "notes":
            await message.answer("Формат: /note <code>рубрика</code>\n\n" + rubric_list("notes"), parse_mode="HTML")
            return
        await state.set_state(NotesInput.collecting)
        await state.update_data(rubric=rubric, parts=[])
        await message.answer(
            f"Присылай заметки для «{load_rubrics()[rubric].title}»: текст, файл .txt/.md или голосовые, "
            "можно несколькими сообщениями. /done — готовить черновик, /cancel — отмена."
        )

    async def add_part(message: Message, state: FSMContext, text: str) -> None:
        data = await state.get_data()
        parts = [*data.get("parts", []), text.strip()]
        await state.update_data(parts=parts)
        await message.answer(f"Принял. Всего {sum(len(p) for p in parts)} символов. /done — готовить черновик.")

    @router.message(Command("done"), StateFilter(NotesInput.collecting))
    async def cmd_done(message: Message, state: FSMContext, bot: Bot, settings: Settings, llm: LLM) -> None:
        data = await state.get_data()
        notes = "\n\n".join(data.get("parts", []))
        if not notes.strip():
            await message.answer("Заметок пока нет: пришли текст, файл или голосовое. /cancel — отмена.")
            return
        await state.clear()
        await draft_from_notes(message, bot, settings, llm, data["rubric"], notes)

    @router.message(StateFilter(NotesInput.collecting), F.text, ~F.text.startswith("/"))
    async def on_notes_text(message: Message, state: FSMContext) -> None:
        await add_part(message, state, message.text)

    @router.message(StateFilter(NotesInput.collecting), F.voice | F.audio)
    async def on_notes_voice(message: Message, state: FSMContext, bot: Bot, settings: Settings) -> None:
        if text := await voice_text(message, bot, settings):
            await add_part(message, state, text)

    @router.message(StateFilter(NotesInput.collecting), F.document)
    async def on_notes_file(message: Message, state: FSMContext, bot: Bot) -> None:
        if text := await document_text(message, bot):
            await add_part(message, state, text)

    @router.message(StateFilter(None), F.voice | F.audio)
    async def on_voice(message: Message, bot: Bot, settings: Settings, llm: LLM) -> None:
        if text := await voice_text(message, bot, settings):
            await draft_from_notes(message, bot, settings, llm, VOICE_RUBRIC, text)

    # --- статистика -----------------------------------------------------------

    @router.message(Command("stats"))
    async def cmd_stats(message: Message, settings: Settings) -> None:
        async with db.session() as s:
            note = ""
            try:
                await metrics.refresh(s, settings)
            except metrics.StatsUnavailable as e:
                note = f"\n\nСвежие просмотры получить не удалось: {escape(str(e))}"
            by_rubric, top_posts, missing = await metrics.summary(s)
        if not by_rubric:
            await message.answer("Данных о просмотрах пока нет: нужен хотя бы один опубликованный пост." + note)
            return
        lines = ["<b>Просмотры по рубрикам</b>"]
        lines += [f"{escape(r.title)}: в среднем {r.avg_views:.0f} · постов {r.posts}" for r in by_rubric]
        lines.append("\n<b>Лучшие посты</b>")
        lines += [f"{views} — {escape(rubric)}: {escape(title[:60])}" for rubric, title, views in top_posts]
        if missing:
            lines.append(f"\nБез данных постов: {missing} (слишком старые или ещё не попали на страницу канала).")
        await message.answer("\n".join(lines) + note, parse_mode="HTML")

    # --- источники ----------------------------------------------------------

    @router.message(Command("digest"))
    async def cmd_digest(message: Message, bot: Bot, sources_cfg: SourcesConfig) -> None:
        async with db.session() as s:
            shown = await send_digest(bot, message.chat.id, s, sources_cfg, include_offered=True)
        if not shown:
            await message.answer("Кандидатов пока нет. /fetch — собрать источники сейчас.")

    @router.message(Command("fetch"))
    async def cmd_fetch(message: Message, bot: Bot, llm: LLM, sources_cfg: SourcesConfig) -> None:
        if not sources_cfg.enabled:
            await message.answer("Источники выключены: проверь data/sources.yaml.")
            return
        await message.answer("Собираю и оцениваю источники, это около минуты…")
        async with db.session() as s:
            try:
                report = await sources.collect_and_rank(s, llm, sources_cfg)
            except Exception as e:
                log.exception("сбор источников не удался")
                await message.answer(f"Не получилось собрать источники: {e}")
                return
            text = f"Новых материалов: {report.new}, оценено: {report.ranked}."
            if report.errors:
                text += "\nНе ответили:\n" + "\n".join(f"• {e}" for e in report.errors)
            await message.answer(text[:4000])
            shown = await send_digest(bot, message.chat.id, s, sources_cfg, include_offered=False)
        if not shown:
            await message.answer("Новых кандидатов выше порога нет. /digest — показать прежних.")

    @router.callback_query(ItemAction.filter())
    async def on_item(cb: CallbackQuery, callback_data: ItemAction, bot: Bot, settings: Settings, llm: LLM) -> None:
        async with db.session() as s:
            item = await s.get(SourceItem, callback_data.item_id)
            if item is None:
                await cb.answer("Материал не найден.", show_alert=True)
                return
            if item.status in (ItemStatus.USED.value, ItemStatus.HIDDEN.value):
                await cb.answer("Этот кандидат уже обработан.", show_alert=True)
                return
            remaining = without_item(cb.message.reply_markup, item.id)
            if callback_data.action == "hide":
                item.status = ItemStatus.HIDDEN.value
                await s.commit()
                await cb.answer("Скрыл, больше не предложу")
                await cb.message.edit_reply_markup(reply_markup=remaining)
                return
            if callback_data.action != "write":
                await cb.answer("Неизвестное действие.")
                return
            await cb.answer("Читаю источник и готовлю черновик…")
            item_id, rubric = item.id, item.rubric
            try:
                topic = await generator.topic_from_item(s, item)
                draft = await generator.generate_draft(s, llm, settings, rubric, topic)
            except Exception as e:
                log.exception("черновик по материалу %s не удался", item_id)
                await s.rollback()
                item = await s.get(SourceItem, item_id)
                if item and item.status == ItemStatus.USED.value:  # вернуть кандидата, чтобы можно было повторить
                    item.status = ItemStatus.OFFERED.value
                    await s.commit()
                reason = "по нему уже есть черновик, смотри /queue" if isinstance(e, generator.TopicTaken) else e
                await cb.message.answer(f"Не получилось подготовить черновик: {reason}")
                return
            await cb.message.edit_reply_markup(reply_markup=remaining)
            await send_card(bot, cb.message.chat.id, s, draft)

    # --- кнопки под черновиком ---------------------------------------------

    @router.callback_query(DraftAction.filter())
    async def on_action(
        cb: CallbackQuery,
        callback_data: DraftAction,
        state: FSMContext,
        bot: Bot,
        settings: Settings,
        llm: LLM,
    ) -> None:
        action = callback_data.action
        async with db.session() as s:
            draft = await s.get(Draft, callback_data.draft_id)
            if draft is None:
                await cb.answer("Черновик не найден.", show_alert=True)
                return
            try:
                if action == "pub":
                    await moderation.publish(s, bot, settings, draft)
                    await cb.answer("Опубликовано")
                elif action == "ok":
                    await moderation.approve(s, draft)
                    await cb.answer("В очереди: выйдет в ближайший слот рубрики")
                elif action == "later":
                    await moderation.postpone(s, draft)
                    await cb.answer("Отложил")
                elif action == "no":
                    await moderation.reject(s, draft)
                    await cb.answer("Отклонил, тема больше не всплывёт")
                elif action == "fill":
                    moderation.ensure_open(draft)
                    placeholders = draft_placeholders(draft)
                    if not placeholders:
                        await cb.answer("Все блоки уже вписаны.", show_alert=True)
                        return
                    await state.set_state(FillPlaceholder.waiting)
                    await state.update_data(draft_id=draft.id, placeholder=placeholders[0])
                    await cb.answer()
                    await cb.message.answer(
                        f"Напиши текст для блока «{placeholders[0].strip('{}')}» в черновике #{draft.id}: "
                        "2–3 строки своими словами. /cancel — отмена."
                    )
                    return
                elif action == "edit":
                    moderation.ensure_open(draft)
                    await state.set_state(EditText.waiting)
                    await state.update_data(draft_id=draft.id)
                    await cb.answer()
                    await cb.message.answer(
                        f"Пришли новый текст для черновика #{draft.id} одним сообщением. "
                        "Форматирование Telegram сохранится, HTML-теги тоже понимаю. /cancel — отмена."
                    )
                    return
                elif action == "regenc":
                    moderation.ensure_open(draft)
                    await state.set_state(RegenComment.waiting)
                    await state.update_data(draft_id=draft.id)
                    await cb.answer()
                    await cb.message.answer(
                        f"Что поправить в черновике #{draft.id}? Например: «короче», «больше примеров из DevOps». "
                        "/cancel — отмена."
                    )
                    return
                elif action == "regen":
                    moderation.ensure_open(draft)
                    await cb.answer("Генерирую заново…")
                    try:
                        await generator.regenerate_draft(s, llm, settings, draft)
                    except Exception as e:  # на колбэк уже ответили, поэтому причину шлём сообщением
                        log.exception("перегенерация не удалась")
                        await cb.message.answer(f"Не получилось перегенерировать: {e}")
                        return
                    await _retire_card(cb, f"Заменён версией v{draft.version} ниже.")
                    await send_card(bot, cb.message.chat.id, s, draft)
                    return
                else:
                    await cb.answer("Неизвестное действие.")
                    return
            except moderation.NotAllowed as e:
                await cb.answer(f"Нельзя: {e}"[:200], show_alert=True)
                return
            except TelegramAPIError as e:
                log.exception("Telegram отклонил действие %s", action)
                await cb.answer(f"Telegram отклонил: {e}"[:200], show_alert=True)
                return
            except Exception as e:
                log.exception("действие %s не удалось", action)
                await cb.answer(f"Ошибка: {e}"[:200], show_alert=True)
                return
            # действие изменило статус — обновляем карточку
            await cb.message.edit_text(await card_text(s, draft), parse_mode="HTML", reply_markup=draft_keyboard(draft))

    # --- ввод после «Править» и «С комментарием» -----------------------------

    @router.message(StateFilter(EditText.waiting), F.text, ~F.text.startswith("/"))
    async def on_new_text(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        await state.clear()
        # Текст с форматированием Telegram берём как HTML; если автор сам набрал теги — оставляем как есть.
        raw = message.text or ""
        html = raw if (not message.entities and _HTML_TAG_RE.search(raw)) else message.html_text
        async with db.session() as s:
            draft = await s.get(Draft, data.get("draft_id"))
            if draft is None:
                await message.answer("Черновик не найден.")
                return
            try:
                await moderation.set_text(s, draft, html)
            except moderation.NotAllowed as e:
                await message.answer(f"Нельзя: {e}")
                return
            await send_card(bot, message.chat.id, s, draft, note="Текст заменён твоей версией.")

    @router.message(StateFilter(RegenComment.waiting), F.text, ~F.text.startswith("/"))
    async def on_comment(message: Message, state: FSMContext, bot: Bot, settings: Settings, llm: LLM) -> None:
        data = await state.get_data()
        await state.clear()
        async with db.session() as s:
            draft = await s.get(Draft, data.get("draft_id"))
            if draft is None:
                await message.answer("Черновик не найден.")
                return
            await message.answer("Переделываю с учётом комментария…")
            try:
                moderation.ensure_open(draft)
                await generator.regenerate_draft(s, llm, settings, draft, comment=message.text or "")
            except moderation.NotAllowed as e:
                await message.answer(f"Нельзя: {e}")
                return
            except Exception as e:
                log.exception("перегенерация не удалась")
                await message.answer(f"Не получилось перегенерировать: {e}")
                return
            await send_card(bot, message.chat.id, s, draft)

    @router.message(StateFilter(FillPlaceholder.waiting), F.text, ~F.text.startswith("/"))
    async def on_fill(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        await state.clear()
        async with db.session() as s:
            draft = await s.get(Draft, data.get("draft_id"))
            if draft is None:
                await message.answer("Черновик не найден.")
                return
            try:
                await moderation.fill_placeholder(s, draft, data.get("placeholder") or "", message.html_text)
            except moderation.NotAllowed as e:
                await message.answer(f"Нельзя: {e}")
                return
            await send_card(bot, message.chat.id, s, draft, note="Твой блок вписан.")

    @router.message(StateFilter(EditText.waiting, RegenComment.waiting, FillPlaceholder.waiting))
    async def on_not_text(message: Message) -> None:
        await message.answer("Жду текст одним сообщением. /cancel — отмена.")

    @router.message(StateFilter(NotesInput.collecting))
    async def on_notes_other(message: Message) -> None:
        await message.answer("Жду заметки: текст, файл .txt/.md или голосовое. /done — готовить, /cancel — отмена.")

    @router.message()
    async def fallback(message: Message) -> None:
        await message.answer("Не понял. /help — список команд.")

    return router


async def _retire_card(cb: CallbackQuery, note: str) -> None:
    """Снимает кнопки со старой карточки, чтобы по устаревшей версии нельзя было нажать."""
    try:
        await cb.message.edit_text(f"{cb.message.html_text}\n\n{escape(note)}", parse_mode="HTML", reply_markup=None)
    except TelegramAPIError:
        log.debug("не удалось обновить старую карточку", exc_info=True)
