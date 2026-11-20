"""Вырезает персонажа с однотонного фона аватарки и кладёт его в brand/mascot.png.

    pip install pillow numpy
    python scripts/make_mascot.py аватарка.png

Фон определяется по левому верхнему углу картинки. Результат — PNG с прозрачным фоном;
бот ставит его на обложки постов и в подвал шпаргалок. Папка brand/ в git не попадает.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

NEAR, FAR = 28.0, 110.0  # ближе NEAR к цвету фона — прозрачно, дальше FAR — непрозрачно
MAX_SIDE = 900


def cut_out(src: Path, dst: Path) -> tuple[int, int]:
    im = np.asarray(Image.open(src).convert("RGB")).astype(np.float32)
    bg = im[5:25, 5:25].reshape(-1, 3).mean(axis=0)
    dist = np.sqrt(((im - bg) ** 2).sum(axis=2))
    alpha = np.clip((dist - NEAR) / (FAR - NEAR), 0, 1)[..., None]
    # на полупрозрачных краях вычитаем цвет фона, иначе вокруг персонажа останется кайма
    rgb = np.clip(np.where(alpha > 0.02, (im - (1 - alpha) * bg) / np.maximum(alpha, 0.02), 0), 0, 255)
    out = Image.fromarray(np.dstack([rgb, alpha * 255]).astype(np.uint8), "RGBA")
    out = out.crop(out.getbbox())
    out.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, optimize=True)
    return out.size


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    target = Path(__file__).resolve().parent.parent / "brand" / "mascot.png"
    print(f"{target}: {cut_out(Path(sys.argv[1]), target)}")
