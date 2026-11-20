"""Обложки постов: надпись, файл с картинкой и два способа показать её в Telegram."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app import covers, db, render
from app.generator import build_payload
from app.models import Draft
from app.rubrics import get_rubric
from app.validator import validate_payload
from tests.conftest import ADMIN, CHANNEL, FAKE_COVER
from tests.test_sources_flow import say_and_wait_card

BASE = "https://img.example.org/m"


@pytest.fixture
def with_covers(settings, tmp_path):
    settings.media_dir = str(tmp_path / "media")
    settings.media_base_url = BASE + "/"
    settings.brand_dir = str(tmp_path / "brand")
    covers.configure(settings)
    return settings


def post(html: str, rubric: str = "concept", **extra) -> Draft:
    return Draft(id=7, rubric=rubric, payload={"kind": "post", "html": html, **extra}, issues=[])


def test_cover_text_prefers_model_field_then_headline():
    assert (
        covers.cover_text({"html": "<b>Понятие недели: эмбеддинги</b>\n\nтекст", "cover": " Эмбеддинги. "})
        == "Эмбеддинги"
    )
    # ярлык рубрики в заголовке на обложке не нужен: рубрика подписана отдельно
    assert covers.cover_text({"html": "<b>Понятие недели: эмбеддинги</b>\n\nтекст"}) == "Эмбеддинги"
    assert (
        covers.cover_text({"html": "<b>А знали ли вы, что AlexNet учили на двух GTX 580?</b>"})
        == "AlexNet учили на двух GTX 580"
    )
    assert covers.cover_text({"html": "<b>🧠 Почему <i>RAG</i> &amp; поиск</b>"}, "🧠") == "Почему RAG & поиск"
    two = "<b>Газеты 1958 года обещали думающую машину. Она умела отличать левое от правого</b>"
    assert covers.cover_text({"html": two}) == "Газеты 1958 года обещали думающую машину"
    assert covers.cover_text({"html": "текст без заголовка"}) == ""
    long = covers.cover_text({"cover": "слово " * 30})
    assert len(long) <= covers.COVER_TEXT_LIMIT + 1 and long.endswith("…")


def test_payload_keeps_cover_and_validator_warns_on_long_one(settings):
    rubric = get_rubric("concept")
    payload = build_payload({"html": "<b>Заголовок</b>\n\nтекст", "cover": "  Градиентный\nспуск "}, rubric, settings)
    assert payload["cover"] == "Градиентный спуск"
    assert "cover" not in build_payload({"html": "<b>Заголовок</b>"}, rubric, settings)
    warns = [i["text"] for i in validate_payload({**payload, "cover": "я" * 61}) if i["level"] == "warn"]
    assert any("надпись для обложки" in w for w in warns)


async def test_cover_file_is_reused_and_renamed_when_text_changes(with_covers, monkeypatch):
    rendered = []

    async def counting(title, **kwargs):
        rendered.append((title, kwargs["kicker"]))
        return FAKE_COVER

    monkeypatch.setattr(render, "render_cover", counting)
    first = await covers.for_draft(post("<b>Заголовок</b>", cover="Эмбеддинги"))
    again = await covers.for_draft(post("<b>Заголовок</b>", cover="Эмбеддинги"))
    other = await covers.for_draft(post("<b>Заголовок</b>", cover="Токены"))

    assert rendered == [("Эмбеддинги", "Понятие недели"), ("Токены", "Понятие недели")]  # второй раз — из файла
    assert first.url == again.url and first.url.startswith(BASE + "/") and first.url.endswith(".jpg")
    assert other.url != first.url  # Telegram помнит картинку по адресу: новый текст — новый адрес
    files = sorted(p.name for p in Path(with_covers.media_dir).iterdir())
    assert files == sorted(u.url.rsplit("/", 1)[1] for u in (first, other))


async def test_no_cover_when_off_or_not_a_post_or_render_fails(with_covers, monkeypatch):
    quiz = Draft(id=1, rubric="quiz", payload={"kind": "quiz", "question": "?"}, issues=[])
    assert await covers.for_draft(quiz) is None
    assert await covers.for_draft(post("текст без заголовка")) is None

    async def broken(title, **kwargs):
        raise render.RenderUnavailable("нет браузера")

    monkeypatch.setattr(render, "render_cover", broken)
    assert await covers.for_draft(post("<b>Заголовок</b>")) is None  # обложка — украшение, пост выйдет и без неё
    covers.configure(None)
    assert await covers.for_draft(post("<b>Заголовок</b>")) is None


def test_cover_html_escapes_text_and_works_without_mascot(tmp_path):
    html = render.cover_html("<script>x</script>", kicker="A & B", channel="@Easy_ML", brand_dir=str(tmp_path))
    assert "<script>x" not in html and "&lt;script&gt;" in html and "A &amp; B" in html
    assert "no-mascot" in html and "<img" not in html
    (tmp_path / "mascot.png").write_bytes(b"png-bytes")
    render._mascot_uri.cache_clear()
    assert "data:image/png;base64," in render.cover_html("Заголовок", brand_dir=str(tmp_path))
    render._mascot_uri.cache_clear()


async def test_short_post_goes_out_as_photo_with_caption(tg, with_covers):
    card = await say_and_wait_card(tg, "/gen concept")
    preview = tg.sent("sendPhoto", ADMIN)[-1]
    assert preview["photo"].startswith(FAKE_COVER) and "#EML_concept" in preview["caption"]
    assert preview["parse_mode"] == "HTML"

    tg.press(ADMIN, card, tg.buttons(card)["✅ Сейчас"])
    await tg.wait_for(lambda: tg.sent("sendPhoto", CHANNEL))
    posted = tg.sent("sendPhoto", CHANNEL)[0]
    assert posted["caption"] == preview["caption"] and posted["photo"] == preview["photo"]
    assert tg.sent("sendMessage", CHANNEL) == []


async def test_long_post_gets_cover_through_link_preview(tg, with_covers):
    card = await say_and_wait_card(tg, "/gen concept")
    tg.press(ADMIN, card, tg.buttons(card)["✏️ Править"])
    await tg.wait_for(lambda: "Пришли новый текст" in tg.sent("sendMessage", ADMIN)[-1]["text"])
    long_text = "<b>Длинный пост</b>\n\n" + "Подпись к фото ограничена 1024 символами. " * 30
    edited = await say_and_wait_card(tg, long_text)

    preview = tg.sent("sendMessage", ADMIN)[-2]
    options = preview["link_preview_options"]
    assert options["url"].startswith(BASE + "/") and options["url"].endswith(".jpg")
    assert options["prefer_large_media"] is True and options["show_above_text"] is True
    assert (Path(with_covers.media_dir) / options["url"].rsplit("/", 1)[1]).read_bytes().startswith(FAKE_COVER)

    tg.press(ADMIN, edited, tg.buttons(edited)["✅ Сейчас"])
    await tg.wait_for(lambda: tg.sent("sendMessage", CHANNEL))
    assert tg.sent("sendMessage", CHANNEL)[0]["link_preview_options"] == options
    assert tg.sent("sendPhoto", CHANNEL) == []


async def test_long_post_without_public_address_goes_out_plain(tg, with_covers):
    with_covers.media_base_url = ""
    card = await say_and_wait_card(tg, "/gen concept")
    tg.press(ADMIN, card, tg.buttons(card)["✏️ Править"])
    await tg.wait_for(lambda: "Пришли новый текст" in tg.sent("sendMessage", ADMIN)[-1]["text"])
    await say_and_wait_card(tg, "<b>Длинный пост</b>\n\n" + "Текст. " * 200)
    assert tg.sent("sendMessage", ADMIN)[-2]["link_preview_options"] == {"is_disabled": True}
    async with db.session() as s:
        assert (await s.scalars(select(Draft))).one().payload["cover"]  # надпись от модели правку текста пережила


async def test_real_render_produces_jpeg_of_cover_size():
    pytest.importorskip("playwright")
    try:
        jpg = await render.__dict__["render_cover_real"](
            "Как AlexNet в 2012 году перевернул компьютерное зрение", kicker="История ML", channel="@Easy_ML"
        )
    except render.RenderUnavailable as e:
        pytest.skip(f"браузер недоступен: {e}")
    assert jpg.startswith(b"\xff\xd8\xff") and len(jpg) > 20_000
    width, height = _jpeg_size(jpg)
    assert (width, height) == (render.COVER_WIDTH * render.SCALE, render.COVER_HEIGHT * render.SCALE)


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Размер из заголовка SOF: тесту не нужна библиотека для картинок."""
    i = 2
    while i < len(data):
        marker, length = data[i + 1], int.from_bytes(data[i + 2 : i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(data[i + 7 : i + 9], "big"), int.from_bytes(data[i + 5 : i + 7], "big")
        i += 2 + length
    raise AssertionError("в JPEG нет заголовка кадра")
