"""Картинка-шпаргалка: данные → HTML → PNG через headless Chromium (Playwright)."""

from __future__ import annotations

import logging
from html import escape

log = logging.getLogger(__name__)

WIDTH = 1080
SCALE = 2  # итоговая ширина 2160 px: текст остаётся резким после сжатия в Telegram
MAX_SIDES_SUM = 9500  # Telegram не принимает фото, у которого ширина + высота больше 10000 px


class RenderUnavailable(Exception):
    """Картинку собрать не удалось; текст — причина для автора."""


CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0b1220; font-family: Inter, "Noto Sans", "DejaVu Sans", "Noto Color Emoji", sans-serif; }
#sheet { width: 1080px; padding: 56px 56px 44px; background: #0b1220; color: #e6edf7; }
.kicker { font-size: 24px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: #5eb1ff; }
h1 { font-size: 58px; line-height: 1.12; font-weight: 800; margin-top: 14px; letter-spacing: -.01em; }
.subtitle { font-size: 30px; line-height: 1.35; color: #9fb0c7; margin-top: 18px; }
.section { margin-top: 34px; background: #121c2f; border: 1px solid #1f2d47; border-radius: 22px; overflow: hidden; }
.section h2 { font-size: 30px; font-weight: 700; padding: 20px 28px; background: #16233b; color: #ffffff; }
.row { display: flex; gap: 24px; padding: 18px 28px; border-top: 1px solid #1f2d47; align-items: baseline; }
.term { flex: 0 0 300px; font-size: 28px; font-weight: 700; color: #7cc4ff; line-height: 1.3; overflow-wrap: anywhere; }
.text { flex: 1; font-size: 28px; line-height: 1.38; color: #d5deeb; }
.takeaway { margin-top: 34px; padding: 26px 28px; border-radius: 22px; background: #10304f; border: 1px solid #1d4d7c;
            font-size: 30px; line-height: 1.38; font-weight: 600; }
.footer { margin-top: 30px; display: flex; justify-content: space-between; font-size: 24px; color: #6f819b; }
"""


def sheet_html(sheet: dict, *, brand_emoji: str = "", channel: str = "") -> str:
    """Весь текст экранируется: в шаблон попадает только то, что вернула модель, как простой текст."""
    parts = [
        f"<!doctype html><html lang='ru'><head><meta charset='utf-8'><style>{CSS}</style></head><body><div id='sheet'>",
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
    parts.append(f"<div class='footer'><span>{escape(channel)}</span><span>сохрани, пригодится</span></div>")
    parts.append("</div></body></html>")
    return "".join(parts)


async def render_sheet(sheet: dict, *, brand_emoji: str = "", channel: str = "") -> bytes:
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError:
        raise RenderUnavailable("пакет playwright не установлен") from None
    html = sheet_html(sheet, brand_emoji=brand_emoji, channel=channel)
    try:
        async with async_playwright() as p:
            # --no-sandbox: в контейнере нет пользовательских пространств имён; рендерим только свой HTML
            browser = await p.chromium.launch(args=["--no-sandbox"])
            try:
                scale = SCALE
                while True:
                    page = await browser.new_page(viewport={"width": WIDTH, "height": 800}, device_scale_factor=scale)
                    await page.set_content(html, wait_until="load")
                    element = await page.query_selector("#sheet")
                    box = await element.bounding_box()
                    if scale > 1 and (box["width"] + box["height"]) * scale > MAX_SIDES_SUM:
                        await page.close()
                        scale = 1  # очень длинная шпаргалка: жертвуем резкостью, чтобы Telegram её принял
                        continue
                    return await element.screenshot(type="png")
            finally:
                await browser.close()
    except PlaywrightError as e:
        log.exception("рендер шпаргалки не удался")
        raise RenderUnavailable(str(e).splitlines()[0][:200]) from e
