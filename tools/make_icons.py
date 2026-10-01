"""Sortroom app icon: an arched doorway (the room) with sorted mail inside.

Two variants on a 64-unit grid:
  full  - as in the artboard: thin arch, three bars (for 48 px and up)
  small - thicker strokes, two bars (for favicon and sidebar, 16-32 px)
Writes SVGs and PNGs into email_sorter/web/static; needs Pillow (not a runtime dependency):

    python tools/make_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

PETROL = "#0E766E"
WHITE = "#FFFFFF"

VARIANTS = {
    # arch: centre x, centre y of the arc, radius, bottom of the legs, stroke; bars: x0, x1, ys, stroke
    "full": dict(rx=15, cx=32, cy=28.6, r=11.2, bottom=46.0, w=3.0, bx0=26.5, bx1=37.5, bys=(34.2, 39.9, 45.4), bw=2.9),
    "small": dict(rx=14, cx=32, cy=29, r=14, bottom=49, w=6.0, bx0=25.5, bx1=38.5, bys=(37.5, 46.5), bw=5.5),
}


def svg(v: dict) -> str:
    left, right = v["cx"] - v["r"], v["cx"] + v["r"]
    bars = "\n".join(f'    <path d="M{v["bx0"]:g} {y:g} H{v["bx1"]:g}"/>' for y in v["bys"])
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="{v["rx"]:g}" fill="{PETROL}"/>
  <g fill="none" stroke="{WHITE}" stroke-linecap="round">
    <path d="M{left:g} {v["bottom"]:g} V{v["cy"]:g} A{v["r"]:g} {v["r"]:g} 0 0 1 {right:g} {v["cy"]:g} V{v["bottom"]:g}" stroke-width="{v["w"]:g}"/>
    <g stroke-width="{v["bw"]:g}">
{bars}
    </g>
  </g>
</svg>
'''


def png(v: dict, size: int, rounded: bool = True) -> Image.Image:
    k = 8                          # supersampling
    S = size * k / 64

    def s(u):
        return u * S
    img = Image.new("RGBA", (size * k, size * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle((0, 0, s(64) - 1, s(64) - 1), radius=s(v["rx"]), fill=PETROL)
    else:
        d.rectangle((0, 0, s(64), s(64)), fill=PETROL)

    def dot(x, y, w):
        d.ellipse((s(x) - s(w) / 2, s(y) - s(w) / 2, s(x) + s(w) / 2, s(y) + s(w) / 2), fill=WHITE)

    def line(x0, y0, x1, y1, w):
        d.line((s(x0), s(y0), s(x1), s(y1)), fill=WHITE, width=round(s(w)))
        dot(x0, y0, w)
        dot(x1, y1, w)

    cx, cy, r, w = v["cx"], v["cy"], v["r"], v["w"]
    outer = r + w / 2
    d.arc((s(cx - outer), s(cy - outer), s(cx + outer), s(cy + outer)), 180, 360, fill=WHITE, width=round(s(w)))
    line(cx - r, cy, cx - r, v["bottom"], w)
    line(cx + r, cy, cx + r, v["bottom"], w)
    for y in v["bys"]:
        line(v["bx0"], y, v["bx1"], y, v["bw"])
    img = img.resize((size, size), Image.LANCZOS)
    return img if rounded else img.convert("RGB")


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "email_sorter" / "web" / "static"
    (out / "icon.svg").write_text(svg(VARIANTS["full"]), encoding="utf-8")
    (out / "icon-small.svg").write_text(svg(VARIANTS["small"]), encoding="utf-8")
    png(VARIANTS["small"], 32).save(out / "icon-32.png")
    png(VARIANTS["full"], 192).save(out / "icon-192.png")
    png(VARIANTS["full"], 512).save(out / "icon-512.png")
    png(VARIANTS["full"], 180, rounded=False).save(out / "apple-touch-icon.png")  # iOS rounds the corners itself
    print("icons written to", out)
