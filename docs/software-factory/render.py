#!/usr/bin/env python3
"""Render the README images in ./img from index.html.

Each element marked with data-shot="<name>" becomes img/<name>-light.png and
img/<name>-dark.png at 2x scale with transparent corners.

Requires Playwright with Chromium:
    pip install playwright && python3 -m playwright install chromium
If Pillow is installed, each image is reduced to a 256-color palette, which
keeps the files about a quarter of their full-color size.
"""
from __future__ import annotations

import pathlib

from playwright.sync_api import sync_playwright

try:
    from PIL import Image
except ImportError:  # Optional: images stay full-color without Pillow.
    Image = None

HERE = pathlib.Path(__file__).resolve().parent
PAGE = HERE / "index.html"
OUT = HERE / "img"
SHOTS = ("banner", "line", "floor", "levels")


def shrink(path: pathlib.Path) -> None:
    if Image is None:
        return
    with Image.open(path) as image:
        palette = image.convert("RGBA").quantize(
            colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE
        )
    palette.save(path, optimize=True)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for theme in ("light", "dark"):
            page = browser.new_page(
                viewport={"width": 1200, "height": 900},
                device_scale_factor=2,
                color_scheme=theme,
            )
            page.goto(PAGE.as_uri(), wait_until="load")
            page.evaluate("document.fonts.ready")
            page.add_style_tag(content="html, body { background: transparent !important; }")
            for name in SHOTS:
                target = OUT / f"{name}-{theme}.png"
                page.locator(f'[data-shot="{name}"]').screenshot(path=str(target), omit_background=True)
                shrink(target)
                print(target.relative_to(HERE))
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
