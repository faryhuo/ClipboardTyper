"""Shared palette and scalable, code-drawn brand artwork (no GUI dependencies)."""
from pathlib import Path

BG = "#F3F6FA"
INK = "#192B45"
MUTED = "#65758B"
BLUE = "#2864E8"
NAVY = "#12243D"
ASSETS = Path(__file__).resolve().parent.parent / "assets"

# A clipboard with text lines and a mint insertion cursor, on a blue keycap.
# Coordinates use a 64 x 64 viewBox, shared by SVG, PNG, ICO and Tk.
LOGO_RECTS = [
    (2, 2, 62, 62, 16, BLUE),
    (17, 15, 46, 51, 5, "#FFFFFF"),
    (20, 18, 43, 48, 3, BLUE),
    (25, 11, 38, 21, 3, "#FFFFFF"),
    (25, 28, 37, 31, 1.5, "#FFFFFF"),
    (25, 35, 34, 38, 1.5, "#FFFFFF"),
    (39, 34, 42, 49, 1, "#78E2CA"),
    (36, 33, 45, 36, 1, "#78E2CA"),
    (36, 47, 45, 50, 1, "#78E2CA"),
]


def rounded_rect(canvas, x1, y1, x2, y2, radius, color, **kwargs):
    points = [x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
              x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
              x1, y2, x1, y2-radius, x1, y1+radius, x1, y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, fill=color, outline="", **kwargs)


def draw_logo(canvas, x=0, y=0, size=64):
    scale = size / 64
    for x1, y1, x2, y2, radius, color in LOGO_RECTS:
        rounded_rect(canvas, x+x1*scale, y+y1*scale, x+x2*scale, y+y2*scale, radius*scale, color)


def draw_icon(canvas, name, color, size=22):
    """Small consistent outline icons; avoid platform-dependent symbol fonts."""
    s = size / 24
    def line(*points):
        canvas.create_line(*(v*s for v in points), fill=color, width=max(1, 1.6*s),
                           capstyle="round", joinstyle="round")
    if name == "speed":
        line(4, 18, 4, 12, 6, 7, 12, 4, 18, 7, 20, 12, 20, 18)
        line(12, 15, 16, 10)
        line(8, 19, 16, 19)
    elif name == "remote":
        line(3, 4, 21, 4, 21, 16, 3, 16, 3, 4)
        line(12, 16, 12, 21)
        line(8, 21, 16, 21)
        line(8, 10, 16, 10)
        line(13, 7, 16, 10, 13, 13)
    elif name == "keys":
        line(2, 5, 22, 5, 22, 19, 2, 19, 2, 5)
        for x in (6, 10, 14, 18):
            line(x, 9, x+.4, 9)
            line(x, 12, x+.4, 12)
        line(7, 16, 17, 16)
    else:
        line(12, 2, 21, 6, 20, 14, 17, 19, 12, 22, 7, 19, 4, 14, 3, 6, 12, 2)
        line(8, 12, 11, 15, 17, 9)
