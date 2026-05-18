"""Generate app.ico. If app.png is present, convert it (preferred).
Otherwise draw a fallback waveform. Requires Pillow: pip install pillow.

Flaticon icon attribution: 'Transcription icons created by Freepik - Flaticon'
(https://www.flaticon.com/free-icons/transcription) — add to README before release.
"""

from pathlib import Path
from PIL import Image, ImageDraw

SIZES = [16, 32, 48, 64, 128, 256]


def from_png(src: Path) -> list[Image.Image]:
    img = Image.open(src).convert("RGBA")
    return [img.resize((s, s), Image.LANCZOS) for s in SIZES]


def waveform_fallback(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size // 8
    bar_w = max(1, (size - pad * 2) // 7)
    heights = [0.4, 0.65, 1.0, 0.65, 0.4]
    color = (99, 102, 241, 255)
    for i, h in enumerate(heights):
        x0 = pad + i * (bar_w + bar_w // 3)
        bar_h = int((size - pad * 2) * h)
        y0 = (size - bar_h) // 2
        d.rounded_rectangle(
            [x0, y0, x0 + bar_w, y0 + bar_h],
            radius=max(1, bar_w // 3),
            fill=color,
        )
    return img


png = Path(__file__).parent / "app.png"
if png.exists():
    frames = from_png(png)
    source = "app.png"
else:
    frames = [waveform_fallback(s) for s in SIZES]
    source = "waveform fallback"

# Pillow's ICO writer silently skips any requested size larger than the base image,
# so the BASE must be the largest frame. Smaller frames are appended.
frames[-1].save(
    Path(__file__).parent / "app.ico",
    format="ICO",
    sizes=[(s, s) for s in SIZES],
    append_images=frames[:-1],
)
print(f"app.ico written from {source} ({len(frames)} sizes: {SIZES}).")
