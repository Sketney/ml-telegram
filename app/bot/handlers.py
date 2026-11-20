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

from app import db, generator, moderation
from app.bot.cards import STATUS_LABELS, DraftAction, card_text, draft_keyboard, send_card
from app.config import Settings
from app.llm import LLM
from app.models import Draft, Topic, TopicStatus
from app.rubrics import load_rubrics

log = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"</?(b|strong|i|em|u|s|a|code|pre|blockquote)\b", re.IGNORECASE)


class EditText(StatesGroup):
    waiting = State()


class RegenComment(StatesGroup):
    waiting = State()


def rubric_list() -> str:
    return "\n".join(f"<code>{key}</code> — {escape(r.title)}" for key, r in load_rubrics().items())


HELP = (
    "Я готовлю черновики для канала и публикую только то, что ты одобрил.\n\n"
    "<b>Команды</b>\n"
    "/gen <code>рубрика</code> — черновик по следующей теме из бэклога\n"
    "/gen <code>рубрика тема</code> — черновик на свою тему\n"
    "/add <code>рубрика тема</code> — добавить тему в бэклог\n"
    "/topics — сколько тем осталось\n"
    "/queue — черновики в работе\n"
    "/show <code>номер</code> — показать черновик ещё раз\n"
    "/cancel — отменить ввод\n\n"
    "<b>Рубрики</b>\n{rubrics}"
)


def build_router(admin_id: int) -> Router:
    router = Router(name="moderation")
    router.message.filter(F.from_user.id == admin_id)
    router.callback_query.filter(F.from_user.id == admin_id)

    # --- команды -----------------------------------------------------------

    @router.message(Command("start", "help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP.format(rubrics=rubric_list()), parse_mode="HTML")

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
        async with db.session() as s:
            topic = await generator.add_topic(s, rubric, title)
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
        await message.answer("Готовлю черновик…")
        async with db.session() as s:
            try:
                topic = None
                if title:
                    topic = await generator.add_topic(s, rubric, title)
                    if topic is None:  # тема уже в бэклоге — берём её, если по ней ещё не писали
                        topic = await s.scalar(select(Topic).where(Topic.rubric == rubric, Topic.title == title))
                        if topic.status != TopicStatus.NEW.value:
                            await message.answer("По этой теме черновик уже был. Смотри /queue.")
                            return
                draft = await generator.generate_draft(s, llm, settings, rubric, topic)
            except generator.NoTopics:
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

    @router.message(StateFilter(EditText.waiting, RegenComment.waiting))
    async def on_not_text(message: Message) -> None:
        await message.answer("Жду текст одним сообщением. /cancel — отмена.")

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
