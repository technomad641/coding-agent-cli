#!/usr/bin/env python3
"""Renders a hand-scripted terminal session as an animated GIF.

Not a screen recording - a from-scratch draw of the exact transcript already
verified in the README's Usage section, with a typewriter reveal and a
blinking cursor. No ffmpeg/asciinema/vhs dependency, fully reproducible.

Usage:
    pip install pillow
    python3 scripts/make_demo_gif.py     # writes docs/demo.gif
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "docs" / "demo.gif"

SCALE = 2  # supersample for crisper text, downscaled at the end
W, H = 900, 620
CHROME_H = 40
PAD_X, PAD_Y = 18, 14
FONT_SIZE = 15
LINE_H = 22

BG = (11, 14, 20)
CHROME = (22, 27, 34)
DOT_RED, DOT_YEL, DOT_GRN = (255, 95, 86), (255, 189, 46), (39, 201, 63)
TITLE = (139, 148, 158)
FG_DIM = (139, 148, 158)
FG_TEXT = (230, 237, 243)
FG_USER = (126, 231, 135)
FG_TOOL = (210, 153, 34)
FG_APPROVE = (240, 136, 62)
FG_APPROVE_YES = (126, 231, 135)
FG_OK = (126, 231, 135)

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
font = ImageFont.truetype(FONT_PATH, FONT_SIZE * SCALE)

# Each line: (text, color, chars_per_frame, frame_ms, pause_ms_after)
# chars_per_frame/frame_ms model typing speed for "> " user lines and
# streaming speed for everything Claude/the tool prints - both are real
# behaviors of the actual CLI (readline input vs. token streaming), not
# arbitrary animation choices.
LINES = [
    ("$ python main.py", FG_DIM, 3, 18, 150),
    ("coding-agent-cli - basic coding harness (claude-opus-5)", FG_DIM, 6, 12, 60),
    ("project root: ~/scratch/test-project", FG_DIM, 6, 12, 60),
    ('type a task, or "exit" to quit', FG_DIM, 6, 12, 400),
    ("", FG_DIM, 1, 10, 0),
    ("> add a .gitignore for a node project", FG_USER, 2, 34, 500),
    ("", FG_DIM, 1, 10, 0),
    ("[str_replace_based_edit_tool] create .gitignore", FG_TOOL, 5, 14, 200),
    ("Created .gitignore", FG_OK, 5, 14, 350),
    ("", FG_DIM, 1, 10, 0),
    ("Done - added a .gitignore covering node_modules, dist, and .env.", FG_TEXT, 5, 12, 700),
    ("", FG_DIM, 1, 10, 0),
    ("> run the tests", FG_USER, 2, 34, 500),
    ("", FG_DIM, 1, 10, 0),
    ("  run: npm test", FG_APPROVE, 5, 14, 250),
    ("  allow? [y/N] y", FG_APPROVE, 4, 20, 550),
    ("", FG_DIM, 1, 10, 0),
    ("[bash] npm test", FG_TOOL, 5, 14, 150),
    ("...", FG_DIM, 2, 40, 150),
    ("All 12 tests passed.", FG_OK, 5, 14, 350),
    ("", FG_DIM, 1, 10, 0),
    ("Tests are passing - no changes needed.", FG_TEXT, 5, 12, 700),
    ("", FG_DIM, 1, 10, 0),
]

frames = []
durations = []
completed = []  # list of (text, color) already fully drawn


def draw_frame(partial_text=None, partial_color=None, cursor_on=True):
    img = Image.new("RGB", (W * SCALE, H * SCALE), BG)
    d = ImageDraw.Draw(img)

    # macOS-style chrome bar
    d.rectangle([0, 0, W * SCALE, CHROME_H * SCALE], fill=CHROME)
    for i, c in enumerate((DOT_RED, DOT_YEL, DOT_GRN)):
        cx = (PAD_X + i * 22 + 8) * SCALE
        cy = (CHROME_H // 2) * SCALE
        r = 6 * SCALE
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
    title = "coding-agent-cli"
    tw = d.textlength(title, font=font)
    d.text(((W * SCALE - tw) / 2, (CHROME_H // 2) * SCALE - FONT_SIZE * SCALE // 2), title, font=font, fill=TITLE)

    y = CHROME_H + PAD_Y
    for text, color in completed:
        d.text((PAD_X * SCALE, y * SCALE), text, font=font, fill=color)
        y += LINE_H

    if partial_text is not None:
        cursor = "█" if cursor_on else " "
        d.text((PAD_X * SCALE, y * SCALE), partial_text + cursor, font=font, fill=partial_color)
    elif cursor_on:
        d.text((PAD_X * SCALE, y * SCALE), "█", font=font, fill=FG_DIM)

    return img.resize((W, H), Image.LANCZOS)


for text, color, cpf, ms, pause_ms in LINES:
    if text == "":
        completed.append(("", color))
        continue
    pos = 0
    blink = True
    while pos < len(text):
        pos = min(pos + cpf, len(text))
        frames.append(draw_frame(text[:pos], color, cursor_on=blink))
        durations.append(ms)
        blink = not blink if ms >= 30 else blink
    # settle: show the finished line solid for a beat, cursor blinking
    for b in (True, False, True):
        frames.append(draw_frame(text, color, cursor_on=b))
        durations.append(max(pause_ms // 3, 90))
    completed.append((text, color))

# idle blinking cursor on a fresh prompt at the very end, before the loop restarts
for b in (True, False, True, False):
    frames.append(draw_frame("> ", FG_USER, cursor_on=b))
    durations.append(500)

print(f"{len(frames)} frames")
OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
frames[0].save(
    OUTPUT_PATH,
    save_all=True,
    append_images=frames[1:],
    duration=durations,
    loop=0,
    optimize=True,
)
print(f"wrote {OUTPUT_PATH}")
