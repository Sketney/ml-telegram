"""Картинки канала: шпаргалка и обложка поста. Данные → HTML → PNG через headless Chromium (Playwright).

Оформление повторяет аватарку канала: яркий синий фон, кремовые карточки с чёрной обводкой,
оранжевый и салатовый акценты. Все цвета и размеры — в блоке констант ниже.
"""

from __future__ import annotations

import base64
import logging
from functools import lru_cache
from html import escape
from pathlib import Path

log = logging.getLogger(__name__)

SCALE = 2  # картинка вдвое плотнее макета: текст остаётся резким после сжатия в Telegram
MAX_SIDES_SUM = 9500  # Telegram не принимает фото, у которого ширина + высота больше 10000 px

SHEET_WIDTH = 1080
COVER_WIDTH, COVER_HEIGHT = 1200, 630

# Меняешь оформление обложки — подними версию: у новых обложек будут новые адреса,
# а уже вышедшие посты сохранят прежнюю картинку.
COVER_VERSION = 1

BLUE = "#0024fd"
INK = "#0b0b14"
CREAM = "#fff3d9"
ORANGE = "#f68b1f"
LIME = "#b9f21b"

FONT = 'Inter, "Noto Sans", "DejaVu Sans", "Noto Color Emoji", sans-serif'


class RenderUnavailable(Exception):
    """Картинку собрать не удалось; текст — причина для автора."""


@lru_cache
def _mascot_uri(brand_dir: str) -> str:
    """Персонаж канала (brand/mascot.png, PNG с прозрачным фоном) как data-URI. Нет файла — пустая строка."""
    path = Path(brand_dir) / "mascot.png" if brand_dir else None
    if not path or not path.is_file():
        return ""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


BASE_CSS = f"""
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: {BLUE}; font-family: {FONT}; color: {INK}; }}
.kicker {{ display: inline-block; padding: 8px 20px 9px; background: {LIME}; color: {INK}; border: 4px solid {INK};
          border-radius: 14px; box-shadow: 6px 6px 0 {INK}; font-size: 24px; font-weight: 800; letter-spacing: .08em;
          text-transform: uppercase; }}
"""

SHEET_CSS = (
    BASE_CSS
    + f"""
#sheet {{ width: {SHEET_WIDTH}px; padding: 52px 52px 40px; background: {BLUE}; }}
h1 {{ font-size: 62px; line-height: 1.08; font-weight: 900; margin-top: 26px; letter-spacing: -.015em; color: #fff;
      text-shadow: 4px 4px 0 {INK}; }}
.subtitle {{ font-size: 30px; line-height: 1.32; font-weight: 500; color: #e3e8ff; margin-top: 18px; }}
.section {{ margin-top: 36px; background: {CREAM}; border: 4px solid {INK}; border-radius: 22px;
            box-shadow: 8px 8px 0 {INK}; overflow: hidden; }}
.section h2 {{ font-size: 30px; font-weight: 800; padding: 16px 26px 17px; background: {ORANGE};
               border-bottom: 4px solid {INK}; }}
.row {{ display: flex; gap: 24px; padding: 17px 26px; align-items: baseline; }}
.row + .row {{ border-top: 2px solid rgba(11, 11, 20, .16); }}
.term {{ flex: 0 0 290px; font-size: 28px; font-weight: 800; color: {BLUE}; line-height: 1.28;
         overflow-wrap: anywhere; }}
.text {{ flex: 1; font-size: 28px; line-height: 1.36; font-weight: 500; }}
.takeaway {{ margin-top: 36px; padding: 24px 28px; border-radius: 22px; background: {LIME}; border: 4px solid {INK};
             box-shadow: 8px 8px 0 {INK}; font-size: 30px; line-height: 1.34; font-weight: 700; }}
.footer {{ margin-top: 34px; display: flex; justify-content: space-between; align-items: center; font-size: 26px;
           font-weight: 700; color: #fff; }}
.footer .who {{ display: flex; align-items: center; gap: 16px; }}
.footer img {{ width: 76px; height: 76px; object-fit: cover; object-position: 50% 12%; border-radius: 50%;
               border: 4px solid {INK}; background: {CREAM}; }}
.footer .save {{ color: #cfd6ff; font-weight: 600; }}
"""
)

COVER_CSS = (
    BASE_CSS
    + f"""
#cover {{ position: relative; width: {COVER_WIDTH}px; height: {COVER_HEIGHT}px; overflow: hidden; background: {BLUE}; }}
.dots {{ position: absolute; inset: 0;
         background-image: radial-gradient(rgba(255, 255, 255, .16) 3px, transparent 3.6px);
         background-size: 26px 26px;
         -webkit-mask-image: radial-gradient(circle at 100% 0%, #000 0%, transparent 46%); }}
.disc {{ position: absolute; right: -90px; bottom: -150px; width: 600px; height: 600px; border-radius: 50%;
         background: {ORANGE}; border: 5px solid {INK}; }}
.mascot {{ position: absolute; right: 8px; bottom: -64px; height: 590px; }}
.left {{ position: absolute; left: 60px; top: 54px; bottom: 50px; display: flex; flex-direction: column; }}
.with-mascot .left {{ width: 640px; }}
.no-mascot .left {{ width: 700px; }}
.titlebox {{ flex: 1; display: flex; align-items: center; min-height: 0; margin: 26px 0 30px; }}
#title {{ width: 100%; font-weight: 900; line-height: 1.06; letter-spacing: -.02em; color: #fff;
          text-shadow: 5px 5px 0 {INK}; }}
.brand {{ display: flex; align-items: center; gap: 16px; font-size: 26px; font-weight: 700; color: #fff; }}
.brand .bar {{ width: 46px; height: 10px; background: {ORANGE}; border: 3px solid {INK}; border-radius: 6px; }}
.brand .tag {{ color: #cfd6ff; font-weight: 500; }}
"""
)

# Подбирает самый крупный кегль, при котором заголовок помещается в свой блок и ни одно слово не вылезает.
_FIT_TITLE_JS = """
() => {
  const title = document.getElementById('title');
  const box = title.parentElement;
  let size = 128;
  for (; size > 40; size -= 4) {
    title.style.fontSize = size + 'px';
    if (title.scrollWidth <= title.clientWidth + 1 && title.offsetHeight <= box.clientHeight) break;
  }
  title.style.fontSize = size + 'px';
}
"""


def sheet_html(sheet: dict, *, brand_emoji: str = "", channel: str = "", brand_dir: str = "") -> str:
    """Весь текст экранируется: в шаблон попадает только то, что вернула модель, как простой текст."""
    parts = [
        f"<!doctype html><html lang='ru'><head><meta charset='utf-8'><style>{SHEET_CSS}</style></head><body>",
        "<div id='sheet'>",
        "<div class='kicker'>Шпаргалка</div>",
        f"<h1>{escape(str(sheet.get('title') or ''))}</h1>",
    ]
    if sheet.get("subtitle"):
        parts.append(f"<div class='subtitle'>{escape(str(sheet['subtitle']))}</div>")
    for section in sheet.get("sections") or []:
        parts.append(f"<div class='section'><h2>{escape(str(section.get('heading') or ''))}</h2>")
        for item in section.get("items") or []:
            parts.append(
                f"<div class='row'><div class='term'>{escape(str(item.get('term') or ''))}</div>"
                f"<div class='text'>{escape(str(item.get('text') or ''))}</div></div>"
            )
        parts.append("</div>")
    if sheet.get("takeaway"):
        mark = f"{escape(brand_emoji)} " if brand_emoji else ""
        parts.append(f"<div class='takeaway'>{mark}{escape(str(sheet['takeaway']))}</div>")
    mascot = _mascot_uri(brand_dir)
    face = f"<img src='{mascot}' alt=''>" if mascot else ""
    parts.append(
        f"<div class='footer'><span class='who'>{face}<span>{escape(channel)}</span></span>"
        "<span class='save'>сохрани, пригодится</span></div>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)


def cover_html(title: str, *, kicker: str = "", channel: str = "", tagline: str = "", brand_dir: str = "") -> str:
    """Обложка поста: рубрика, крупный заголовок, подпись канала и персонаж справа."""
    mascot = _mascot_uri(brand_dir)
    art = (
        f"<div class='disc'></div><img class='mascot' src='{mascot}' alt=''>" if mascot else "<div class='disc'></div>"
    )
    brand = escape(channel)
    if tagline:
        brand += (
            f" <span class='tag'>· {escape(tagline)}</span>" if brand else f"<span class='tag'>{escape(tagline)}</span>"
        )
    return "".join(
        [
            f"<!doctype html><html lang='ru'><head><meta charset='utf-8'><style>{COVER_CSS}</style></head><body>",
            f"<div id='cover' class='{'with-mascot' if mascot else 'no-mascot'}'><div class='dots'></div>{art}",
            "<div class='left'>",
            f"<div><span class='kicker'>{escape(kicker)}</span></div>" if kicker else "",
            f"<div class='titlebox'><div id='title'>{escape(title)}</div></div>",
            f"<div class='brand'><span class='bar'></span><span>{brand}</span></div>" if brand else "",
            "</div></div></body></html>",
        ]
    )


async def _shoot(
    html: str, selector: str, width: int, height: int, prepare_js: str = "", jpeg_quality: int | None = None
) -> bytes:
    """Снимок элемента страницы. jpeg_quality задан — JPEG (для обложек с рисунком), иначе PNG."""
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError:
        raise RenderUnavailable("пакет playwright не установлен") from None
    try:
        async with async_playwright() as p:
            # --no-sandbox: в контейнере нет пользовательских пространств имён; рендерим только свой HTML
            browser = await p.chromium.launch(args=["--no-sandbox"])
            try:
                scale = SCALE
                while True:
                    page = await browser.new_page(
                        viewport={"width": width, "height": height}, device_scale_factor=scale
                    )
                    await page.set_content(html, wait_until="load")
                    await page.evaluate("() => document.fonts.ready.then(() => true)")
                    if prepare_js:
                        await page.evaluate(prepare_js)
                    element = await page.query_selector(selector)
                    box = await element.bounding_box()
                    if scale > 1 and (box["width"] + box["height"]) * scale > MAX_SIDES_SUM:
                        await page.close()
                        scale = 1  # очень длинная картинка: жертвуем резкостью, чтобы Telegram её принял
                        continue
                    if jpeg_quality:
                        return await element.screenshot(type="jpeg", quality=jpeg_quality)
                    return await element.screenshot(type="png")
            finally:
                await browser.close()
    except PlaywrightError as e:
        log.exception("рендер картинки не удался")
        raise RenderUnavailable(str(e).splitlines()[0][:200]) from e


async def render_sheet(sheet: dict, *, brand_emoji: str = "", channel: str = "", brand_dir: str = "") -> bytes:
    html = sheet_html(sheet, brand_emoji=brand_emoji, channel=channel, brand_dir=brand_dir)
    return await _shoot(html, "#sheet", SHEET_WIDTH, 800)


async def render_cover(
    title: str, *, kicker: str = "", channel: str = "", tagline: str = "", brand_dir: str = ""
) -> bytes:
    html = cover_html(title, kicker=kicker, channel=channel, tagline=tagline, brand_dir=brand_dir)
    # JPEG: с рисунком персонажа PNG весит около 1,5 МБ, JPEG — в несколько раз меньше при том же виде
    return await _shoot(html, "#cover", COVER_WIDTH, COVER_HEIGHT, _FIT_TITLE_JS, jpeg_quality=90)
