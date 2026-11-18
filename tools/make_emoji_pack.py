"""Builds assets/emoji/*.png from the Noto Color Emoji font (SIL OFL, free).

Run once on a machine that has the font (Linux: fonts-noto-color-emoji); the PNGs are
committed so Windows never needs an emoji font. Studio lays these pictures over the clip,
which works the same on every ffmpeg build.
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from clipper.emoji import EMOJI_SET, emoji_filename  # noqa: E402

FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
OUT = os.path.join(os.path.dirname(__file__), "..", "assets", "emoji")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    font = ImageFont.truetype(FONT, 109)
    for ch in sorted(EMOJI_SET):
        img = Image.new("RGBA", (136, 128), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((0, 0), ch, font=font, embedded_color=True)
        img = img.crop(img.getbbox() or (0, 0, 136, 128))
        side = max(img.size)
        sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        sq.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
        sq = sq.resize((160, 160), Image.LANCZOS)
        sq.save(os.path.join(OUT, emoji_filename(ch)))
    print(f"wrote {len(EMOJI_SET)} emoji to {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
