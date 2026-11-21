"""Мини-сервер, изображающий Telegram Bot API: чтобы гонять бота целиком без сети и токена."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import Callable
from typing import Any

from aiohttp import web

BOT_USER = {"id": 1000, "is_bot": True, "first_name": "TestBot", "username": "test_bot"}
CHANNEL_ID = -1001234567890


class MockTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._updates: asyncio.Queue[dict] = asyncio.Queue()
        self._update_id = 0
        self._message_id = 100
        self.messages: dict[int, dict] = {}  # всё, что бот отправил, по message_id
        self.fail_next: dict[str, str] = {}  # method -> описание ошибки для одного вызова
        self.files: dict[str, bytes] = {}  # file_id -> содержимое, которое «лежит» в Telegram
        self._runner: web.AppRunner | None = None
        self.base = ""

    # --- управление из тестов ---------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/bot{token}/{method}", self._handle)
        app.router.add_get("/file/bot{token}/{file_id}", self._download)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    def user_message(self, user_id: int, text: str, entities: list[dict] | None = None) -> None:
        self._message_id += 1
        msg = {
            "message_id": self._message_id,
            "date": int(time.time()),
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "User"},
            "text": text,
        }
        if entities:
            msg["entities"] = entities
        elif text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        self._push({"message": msg})

    def _user_media(self, user_id: int, **content: Any) -> None:
        self._message_id += 1
        self._push(
            {
                "message": {
                    "message_id": self._message_id,
                    "date": int(time.time()),
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": user_id, "is_bot": False, "first_name": "User"},
                    **content,
                }
            }
        )

    def user_voice(self, user_id: int, data: bytes = b"OggS-voice") -> None:
        file_id = f"voice{len(self.files)}"
        self.files[file_id] = data
        self._user_media(
            user_id, voice={"file_id": file_id, "file_unique_id": file_id, "duration": 30, "file_size": len(data)}
        )

    def user_document(self, user_id: int, name: str, data: bytes) -> None:
        file_id = f"doc{len(self.files)}"
        self.files[file_id] = data
        self._user_media(
            user_id, document={"file_id": file_id, "file_unique_id": file_id, "file_name": name, "file_size": len(data)}
        )

    def press(self, user_id: int, card: dict, data: str) -> None:
        self._push(
            {
                "callback_query": {
                    "id": str(self._update_id + 1),
                    "from": {"id": user_id, "is_bot": False, "first_name": "User"},
                    "chat_instance": "ci",
                    "data": data,
                    "message": card,
                }
            }
        )

    def _push(self, update: dict) -> None:
        self._update_id += 1
        self._updates.put_nowait({"update_id": self._update_id, **update})

    def sent(self, method: str, chat_id: int | str | None = None) -> list[dict]:
        return [p for m, p in self.calls if m == method and (chat_id is None or str(p.get("chat_id")) == str(chat_id))]

    def last_card(self, chat_id: int) -> dict:
        """Последнее сообщение с кнопками в этом чате — копия: как его видит клиент на момент вызова."""
        cards = [m for m in self.messages.values() if m["chat"]["id"] == chat_id and m.get("reply_markup")]
        assert cards, "бот не прислал ни одной карточки с кнопками"
        return copy.deepcopy(cards[-1])

    async def next_card(self, chat_id: int, after: dict) -> dict:
        """Ждёт карточку с кнопками, отправленную позже указанной."""
        await self.wait_for(
            lambda: any(
                m["message_id"] > after["message_id"] and m["chat"]["id"] == chat_id and m.get("reply_markup")
                for m in self.messages.values()
            )
        )
        return self.last_card(chat_id)

    @staticmethod
    def buttons(card: dict) -> dict[str, str]:
        """Текст кнопки → callback_data."""
        return {b["text"]: b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row}

    async def wait_for(self, cond: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > deadline:
                raise AssertionError(f"не дождался условия; вызовы: {[m for m, _ in self.calls]}")
            await asyncio.sleep(0.02)

    # --- сам «Telegram» ------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params: dict[str, Any] = dict(await request.post()) if request.can_read_body else {}
        for key, value in list(params.items()):
            if isinstance(value, web.FileField):  # загруженный файл: запоминаем содержимое
                params[key] = value.file.read()
        for key in (
            "reply_markup",
            "options",
            "link_preview_options",
            "correct_option_ids",
            "commands",
            "rich_message",
        ):
            if isinstance(params.get(key), str):
                params[key] = json.loads(params[key])

        if method == "getUpdates":
            try:
                first = await asyncio.wait_for(self._updates.get(), timeout=0.2)
            except asyncio.TimeoutError:
                return self._ok([])
            batch = [first]
            while not self._updates.empty():
                batch.append(self._updates.get_nowait())
            return self._ok(batch)

        self.calls.append((method, params))
        if method in self.fail_next:
            return web.json_response(
                {"ok": False, "error_code": 400, "description": self.fail_next.pop(method)}, status=400
            )
        if method == "getMe":
            return self._ok(BOT_USER)
        if method == "sendMessage":
            return self._ok(self._store(params, text=params["text"]))
        if method == "getFile":
            return self._ok(
                {"file_id": params["file_id"], "file_unique_id": params["file_id"], "file_path": params["file_id"]}
            )
        if method == "sendPhoto":
            ref = params.get("photo")
            if isinstance(ref, str) and ref.startswith("attach://"):  # файл лежит в соседнем поле формы
                params["photo"] = params.pop(ref.removeprefix("attach://"))
            photo = [{"file_id": "ph", "file_unique_id": "ph", "width": 2160, "height": 2400}]
            return self._ok(self._store(params, photo=photo, caption=params.get("caption", "")))
        if method == "sendRichMessage":
            return self._ok(self._store(params, text=_rich_text(params["rich_message"])))
        if method == "sendPoll":
            poll = {
                "id": "p1",
                "question": params["question"],
                "options": [
                    {"persistent_id": str(i), "text": o["text"], "voter_count": 0}
                    for i, o in enumerate(params["options"])
                ],
                "total_voter_count": 0,
                "is_closed": False,
                "is_anonymous": True,
                "type": params.get("type", "regular"),
                "allows_multiple_answers": False,
                "allows_revoting": False,
                "members_only": False,
            }
            return self._ok(self._store(params, poll=poll))
        if method == "editMessageText":
            msg = self.messages[int(params["message_id"])]
            msg["text"] = params["text"]
            if "reply_markup" in params:
                msg["reply_markup"] = params["reply_markup"]
            else:
                msg.pop("reply_markup", None)
            return self._ok(msg)
        if method == "editMessageReplyMarkup":
            msg = self.messages[int(params["message_id"])]
            if params.get("reply_markup"):
                msg["reply_markup"] = params["reply_markup"]
            else:
                msg.pop("reply_markup", None)
            return self._ok(msg)
        return self._ok(True)  # answerCallbackQuery, setMyCommands, deleteWebhook…

    async def _download(self, request: web.Request) -> web.Response:
        return web.Response(body=self.files[request.match_info["file_id"]])

    def _store(self, params: dict, **content: Any) -> dict:
        self._message_id += 1
        raw_chat = params["chat_id"]
        is_channel = str(raw_chat).startswith(("@", "-100"))
        msg = {
            "message_id": self._message_id,
            "date": int(time.time()),
            "chat": {"id": CHANNEL_ID, "type": "channel", "title": "Test"}
            if is_channel
            else {"id": int(raw_chat), "type": "private"},
            "from": BOT_USER,
            **content,
        }
        if params.get("reply_markup"):
            msg["reply_markup"] = params["reply_markup"]
        self.messages[msg["message_id"]] = msg
        return msg

    @staticmethod
    def _ok(result: Any) -> web.Response:
        return web.json_response({"ok": True, "result": result})


def _rich_text(node: Any) -> str:
    """Весь текст развёрнутого сообщения одной строкой — чтобы тесты могли искать в нём слова и ссылки."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(filter(None, (_rich_text(x) for x in node)))
    if isinstance(node, dict):
        keys = ("text", "summary", "blocks", "items", "cells", "url", "caption")
        head = (
            f"[{node['type']}]"
            if isinstance(node.get("type"), str) and "blocks" not in node and "text" not in node
            else ""
        )
        return " ".join(filter(None, [head, *(_rich_text(node[k]) for k in keys if k in node)]))
    return ""
