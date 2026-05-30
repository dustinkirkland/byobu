#!/usr/bin/env python3
"""
Generate Trustmux PWA demo screenshots as PNG files.
Renders the UI programmatically using Pillow + cairosvg — no browser required.
Run: python generate_screenshots.py [--out-dir screenshots/]
"""

import argparse
import io
import os

import cairosvg
from PIL import Image, ImageDraw, ImageFont

# ── CSS design tokens ──────────────────────────────────────────────────────
BG      = "#141414"
BG2     = "#1e1e1e"
BG3     = "#282828"
BORDER  = "#333333"
TEXT    = "#d4d4d4"
DIM     = "#666666"
ACCENT  = "#4e9de0"
GREEN   = "#4ec94e"
AMBER   = "#e0a040"
RED     = "#e05050"
UBUNTU  = "#E95420"   # Ubuntu orange

# ── viewport: Pixel 7 / typical Android phone ─────────────────────────────
W, H = 390, 844
SCALE = 2          # 2× for high-DPI; output PNGs are 780×1688
SW, SH = W * SCALE, H * SCALE

# ── font paths ─────────────────────────────────────────────────────────────
_MONO   = "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"
_MONO_B = "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"
_SANS   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ── SVG logo path ─────────────────────────────────────────────────────────
_SVG_LOGO = os.path.join(os.path.dirname(__file__),
                          "trustmux", "static", "trustmux.svg")


def _font(path, size):
    try:
        return ImageFont.truetype(path, size * SCALE)
    except Exception:
        return ImageFont.load_default()


F11  = _font(_MONO,   11)
F12  = _font(_MONO,   12)
F13  = _font(_MONO,   13)
F15  = _font(_MONO,   15)
F15B = _font(_MONO_B, 15)
F18B = _font(_MONO_B, 18)
F11S = _font(_SANS,   11)
F13S = _font(_SANS,   13)
F13B = _font(_SANS_B, 13)


def px(n):
    return int(n * SCALE)


def hex_to_rgb(h, alpha=255):
    h = h.lstrip('#')
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def new_canvas():
    return Image.new("RGBA", (SW, SH), hex_to_rgb(BG))


def draw_rect(d, x, y, x2, y2, fill=None, outline=None, radius=0, lw=1):
    kw = dict(radius=px(radius)) if radius else {}
    fn = d.rounded_rectangle if radius else d.rectangle
    box = [px(x), px(y), px(x2), px(y2)]
    if fill:
        fn(box, fill=hex_to_rgb(fill), **kw)
    if outline:
        fn(box, outline=hex_to_rgb(outline), width=lw * SCALE, **kw)


def draw_text(d, x, y, text, font, color=TEXT):
    d.text((px(x), px(y)), text, font=font, fill=hex_to_rgb(color))


def tw(text, font):
    """Text width in logical px."""
    bb = font.getbbox(text)
    return (bb[2] - bb[0]) // SCALE


def draw_line(d, x1, y1, x2, y2, fill=BORDER, width=1):
    d.line([px(x1), px(y1), px(x2), px(y2)],
           fill=hex_to_rgb(fill), width=width * SCALE)


def draw_circle(d, cx, cy, r, fill):
    d.ellipse([px(cx - r), px(cy - r), px(cx + r), px(cy + r)],
              fill=hex_to_rgb(fill))


# ── SVG logo ───────────────────────────────────────────────────────────────
_logo_cache = {}


def get_logo_img(h_px):
    """Return a Pillow Image of the SVG logo at logical height h_px."""
    if h_px in _logo_cache:
        return _logo_cache[h_px]
    size = h_px * SCALE
    png_bytes = cairosvg.svg2png(url=_SVG_LOGO,
                                  output_width=size, output_height=size)
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    _logo_cache[h_px] = img
    return img


def paste_logo(canvas, x, y, h_px=24):
    """Paste the SVG logo onto canvas at logical coordinates."""
    logo = get_logo_img(h_px)
    canvas.paste(logo, (px(x), px(y)), logo)


# ── geometric enter arrow (↵) ─────────────────────────────────────────────
def draw_enter_arrow(d, cx, cy, size=10, color="#ffffff"):
    """Draw a return/enter arrow centred at (cx, cy) in logical px."""
    c = hex_to_rgb(color)
    lw = max(2, SCALE)
    s = size * SCALE
    cx, cy = px(cx), px(cy)
    # Horizontal arm: right side going left
    arm_y = cy - s // 5
    d.line([(cx + s // 2, arm_y), (cx - s // 4, arm_y)],
           fill=c, width=lw)
    # Vertical arm: drop down from right end
    d.line([(cx + s // 2, arm_y), (cx + s // 2, cy + s // 4)],
           fill=c, width=lw)
    # Horizontal lower arm: turn left
    d.line([(cx + s // 2, cy + s // 4), (cx - s // 4, cy + s // 4)],
           fill=c, width=lw)
    # Arrow head pointing left
    tip_x = cx - s // 4
    tip_y = cy + s // 4
    d.polygon([(tip_x, tip_y),
               (tip_x + s // 3, tip_y - s // 3),
               (tip_x + s // 3, tip_y + s // 3)],
              fill=c)


# ── geometric padlock ─────────────────────────────────────────────────────
def draw_padlock(d, cx, cy, size=28, body_color=DIM, shackle_color=DIM):
    """Draw a padlock centred at (cx, cy) in logical px."""
    bw = size * SCALE
    bh = int(bw * 0.7)
    bx = px(cx) - bw // 2
    by = px(cy)
    # Shackle (rounded arch above body)
    sw = int(bw * 0.55)
    sx = px(cx) - sw // 2
    sh = int(bh * 0.85)
    sy = by - sh
    lw = max(3, SCALE * 2)
    d.arc([sx, sy, sx + sw, sy + sh * 2],
          start=180, end=0,
          fill=hex_to_rgb(shackle_color), width=lw)
    # Body rectangle
    d.rounded_rectangle([bx, by, bx + bw, by + bh],
                         radius=px(4), fill=hex_to_rgb(body_color))
    # Keyhole: small circle + slit
    kx, ky = px(cx), by + bh // 3
    kr = max(4, SCALE * 3)
    d.ellipse([kx - kr, ky - kr, kx + kr, ky + kr],
              fill=hex_to_rgb(BG2))
    slit_h = bh // 3
    d.rectangle([kx - kr // 2, ky, kx + kr // 2, ky + slit_h],
                fill=hex_to_rgb(BG2))


# ── shared layout sections ─────────────────────────────────────────────────

def draw_header(canvas, ctx_name="bash", pane_idx=2, pane_total=3):
    """Draw the top navigation header. Logo+wordmark left; everything else right."""
    d = ImageDraw.Draw(canvas)
    y0, y1 = 0, 40
    draw_rect(d, 0, y0, W, y1, fill=BG2)
    draw_line(d, 0, y1, W, y1)

    # ── Left: SVG logo + wordmark ──
    paste_logo(canvas, 8, 9, h_px=22)
    draw_text(d, 35, 12, "Trustmux", F15, ACCENT)
    d = ImageDraw.Draw(canvas)   # redraw after paste

    # ── Right: + button, ›, N/M, ‹, ctx-name  (right to left) ──
    btn_w, btn_h = 20, 20
    btn_y = 10
    rx = W - 8   # cursor from right

    # + create
    rx -= btn_w
    draw_rect(d, rx, btn_y, rx + btn_w, btn_y + btn_h,
              fill=BG3, outline=BORDER, radius=3)
    draw_text(d, rx + 6, btn_y + 3, "+", F13, TEXT)

    # › next
    rx -= (btn_w + 4)
    draw_rect(d, rx, btn_y, rx + btn_w, btn_y + btn_h,
              fill=BG3, outline=BORDER, radius=3)
    draw_text(d, rx + 6, btn_y + 3, ">", F13, TEXT)

    # N/M pane counter
    lbl = f"{pane_idx}/{pane_total}"
    lbl_w = tw(lbl, F11)
    rx -= (lbl_w + 8)
    draw_text(d, rx, btn_y + 5, lbl, F11, TEXT)

    # ‹ prev
    rx -= (btn_w + 4)
    draw_rect(d, rx, btn_y, rx + btn_w, btn_y + btn_h,
              fill=BG3, outline=BORDER, radius=3)
    draw_text(d, rx + 6, btn_y + 3, "<", F13, TEXT)

    # Context name
    if ctx_name:
        rx -= (tw(ctx_name, F11) + 8)
        draw_text(d, rx, btn_y + 5, ctx_name, F11, ACCENT)

    return y1


def draw_statusbar(d, top, connected=True,
                   hostname="dev.local", clock="May 30 16:42:07"):
    y0, y1 = top, top + 22
    draw_rect(d, 0, y0, W, y1, fill=BG2)
    draw_line(d, 0, y1, W, y1)

    dot_color = GREEN if connected else AMBER
    draw_circle(d, 17, top + 11, 3.5, dot_color)
    draw_text(d, 25, top + 6, "connected" if connected else "connecting…",
              F11, DIM)

    clock_w  = tw(clock, F11)
    host_w   = tw(hostname, F11)
    draw_text(d, W - 8 - clock_w, top + 6, clock, F11, DIM)
    draw_text(d, W - 8 - clock_w - 6 - host_w, top + 6, hostname, F11, ACCENT)

    return y1


def draw_byobu_statusline(d, top, bottom):
    draw_rect(d, 0, top, W, bottom, fill=BG2)
    draw_line(d, 0, top, W, top)

    # Ubuntu chip (orange u + version), then system chips
    chips_left = [
        (UBUNTU,    "#ffffff", "u"),
        (BG3,       TEXT,      "26.04"),
        (BG3,       DIM,       "up 3d 7h"),
    ]
    chips_right = [
        ("#3465a4",  "#d3d7cf", "16.2G"),
        ("#4e9a06",  "#141414", "CPU 4%"),
        ("#555753",  "#eeeeec", "0.42"),
        (ACCENT,     "#141414", "16:42"),
    ]

    cx = 8
    for bg, fg, txt in chips_left:
        w = tw(txt, F11) + 10
        draw_rect(d, cx, top + 3, cx + w, bottom - 3, fill=bg, radius=8)
        draw_text(d, cx + 4, top + 5, txt, F11, fg)
        cx += w + 3

    cx = W - 8
    for bg, fg, txt in reversed(chips_right):
        w = tw(txt, F11) + 10
        cx -= w
        draw_rect(d, cx, top + 3, cx + w, bottom - 3, fill=bg, radius=8)
        draw_text(d, cx + 4, top + 5, txt, F11, fg)
        cx -= 3


def draw_inputbar(canvas, top, text_mode=False,
                  input_text=None, placeholder="Send keys to pane…"):
    d = ImageDraw.Draw(canvas)
    y0 = top
    draw_rect(d, 0, y0, W, H, fill=BG2)
    draw_line(d, 0, y0, W, y0)

    pad = 8
    btn_w = 30
    sx = W - pad - btn_w                   # send button x
    kx = sx - 4 - btn_w                    # kbd-mode button x
    cmd_x2 = kx - 6
    cmd_x, cmd_y = pad, y0 + pad
    cmd_y2 = H - pad

    # textarea
    border_col = ACCENT if text_mode else BORDER
    draw_rect(d, cmd_x, cmd_y, cmd_x2, cmd_y2,
              fill=BG3, outline=border_col, radius=4)
    if input_text:
        # wrap text at ~cmd width
        draw_text(d, cmd_x + 8, cmd_y + 9, input_text, F13, TEXT)
    else:
        draw_text(d, cmd_x + 8, cmd_y + 9, placeholder, F13, DIM)

    # keyboard mode toggle
    kbdlabel = "Aa" if text_mode else "$_"
    kbdcolor  = ACCENT if text_mode else TEXT
    draw_rect(d, kx, cmd_y, kx + btn_w, cmd_y + 38,
              fill=BG3, outline=BORDER, radius=4)
    draw_text(d, kx + 4, cmd_y + 11, kbdlabel, F13, kbdcolor)

    # send button
    draw_rect(d, sx, cmd_y, sx + btn_w, cmd_y + 38, fill=ACCENT, radius=4)
    btn_cx = sx + btn_w // 2
    btn_cy = cmd_y + 19
    draw_enter_arrow(d, btn_cx, btn_cy, size=9, color="#ffffff")


# ── fake terminal content: Claude Code session ─────────────────────────────
CLAUDE_LINES = [
    (TEXT,   "kirkland@dev:~/src/byobu$ claude"),
    (ACCENT, "   ✻ Welcome to Claude Code!"),
    (DIM,    "     /help for help, /status for your plan"),
    (DIM,    ""),
    (ACCENT, " > What does trustmux do?"),
    (DIM,    ""),
    (TEXT,   "Trustmux is a secure terminal bridge"),
    (TEXT,   "that lets you control tmux/byobu from"),
    (TEXT,   "your phone via a PWA. It tunnels over"),
    (TEXT,   "Tailscale and uses WebAuthn biometrics"),
    (TEXT,   "to lock the app when backgrounded."),
    (DIM,    ""),
    (ACCENT, " > Show me the connection flow"),
    (DIM,    ""),
    (GREEN,  "1. trustmux-enable  — starts daemon"),
    (GREEN,  "2. Open PWA on phone, tap Pair"),
    (GREEN,  "3. Enter 6-digit code from terminal"),
    (GREEN,  "4. WebSocket streams pane snapshots"),
    (DIM,    ""),
    (TEXT,   " > ▌"),
]

MAIN_LINES = [
    (TEXT,   "kirkland@dev:~/src/byobu$ ls -la"),
    (DIM,    "total 72"),
    (TEXT,   "drwxr-xr-x  8 kirkland kirkland 4096 May 30 16:40 ."),
    (TEXT,   "drwxr-xr-x 42 kirkland kirkland 4096 May 30 15:12 .."),
    (ACCENT, "drwxr-xr-x  2 kirkland kirkland 4096 May 29 09:33 mobile"),
    (ACCENT, "drwxr-xr-x  3 kirkland kirkland 4096 May 28 11:50 usr"),
    (TEXT,   "-rw-r--r--  1 kirkland kirkland 1843 May 30 16:38 README.md"),
    (TEXT,   "-rw-r--r--  1 kirkland kirkland  512 May 29 08:20 Makefile"),
    (DIM,    ""),
    (TEXT,   "kirkland@dev:~/src/byobu$ git log --oneline -5"),
    (AMBER,  "475c6f9 Add 'Nine problems solved' table"),
    (AMBER,  "5e023e9 Honest Trustmux vs Mosh FAQ"),
    (AMBER,  "25d0fef Add FAQ entry comparing to mosh"),
    (AMBER,  "e289d2d Add FAQ section with 12 Q&As"),
    (AMBER,  "9390f29 Refine byobu nav wordmark"),
    (DIM,    ""),
    (TEXT,   "kirkland@dev:~/src/byobu$ ▌"),
]


def draw_output(d, top, bottom, lines=None):
    if lines is None:
        lines = MAIN_LINES
    x, y = 12, top + 10
    lh = int(12 * 1.45)
    for color, txt in lines:
        if y + lh > bottom:
            break
        if txt:
            draw_text(d, x, y, txt, F12, color)
        y += lh


# ── screen renderers ───────────────────────────────────────────────────────

SL_H   = 22   # byobu status line height
IB_H   = 56   # input bar height
SL_TOP = H - IB_H - SL_H
IB_TOP = H - IB_H


def screen_main():
    """Screen 1: Main connected UI with terminal output."""
    img = new_canvas()
    h_bot = draw_header(img, ctx_name="bash", pane_idx=2, pane_total=3)
    d = ImageDraw.Draw(img)
    sb_bot = draw_statusbar(d, h_bot)
    draw_output(d, sb_bot, SL_TOP)
    draw_byobu_statusline(d, SL_TOP, IB_TOP)
    draw_inputbar(img, IB_TOP)
    return img.resize((W, H), Image.LANCZOS), "01_main.png"


def screen_pairing():
    """Screen 2: Pairing code entry overlay."""
    img = new_canvas()
    h_bot = draw_header(img, ctx_name="")
    d = ImageDraw.Draw(img)
    draw_statusbar(d, h_bot, connected=False)

    overlay = Image.new("RGBA", (SW, SH), (10, 10, 10, int(0.97 * 255)))
    img = Image.alpha_composite(img, overlay)
    d = ImageDraw.Draw(img)

    bx, bw = 35, 320
    by, bh = 155, 390
    draw_rect(d, bx, by, bx + bw, by + bh, fill=BG2, outline=BORDER, radius=14)

    # Logo
    img_tmp = img
    logo = get_logo_img(52)
    lx = px(bx + bw // 2) - logo.width // 2
    img_tmp.paste(logo, (lx, px(by + 24)), logo)
    d = ImageDraw.Draw(img_tmp)

    title = "TRUSTMUX"
    draw_text(d, bx + bw // 2 - tw(title, F18B) // 2,
              by + 86, title, F18B, ACCENT)

    for i, line in enumerate(["Enter the pairing code",
                               "shown in the terminal:"]):
        draw_text(d, bx + bw // 2 - tw(line, F13S) // 2,
                  by + 120 + i * 18, line, F13S, DIM)

    # Code input box
    draw_rect(d, bx + 14, by + 166, bx + bw - 14, by + 220,
              fill=BG3, outline=BORDER, radius=8)
    code_f = _font(_MONO_B, 28)
    code = "482-917"
    draw_text(d, bx + bw // 2 - tw(code, code_f) // 2,
              by + 178, code, code_f, TEXT)

    # Pair button
    draw_rect(d, bx + 14, by + 238, bx + bw - 14, by + 282,
              fill=ACCENT, radius=8)
    btn = "Pair device"
    draw_text(d, bx + bw // 2 - tw(btn, F15B) // 2,
              by + 252, btn, F15B, "#ffffff")

    return img_tmp.resize((W, H), Image.LANCZOS), "02_pairing.png"


def screen_create_overlay():
    """Screen 3: New pane / window / session overlay."""
    img = new_canvas()
    h_bot = draw_header(img, ctx_name="bash", pane_idx=2, pane_total=3)
    d = ImageDraw.Draw(img)
    sb_bot = draw_statusbar(d, h_bot)
    draw_output(d, sb_bot, SL_TOP)
    draw_byobu_statusline(d, SL_TOP, IB_TOP)
    draw_inputbar(img, IB_TOP)

    overlay = Image.new("RGBA", (SW, SH), (0, 0, 0, int(0.72 * 255)))
    img = Image.alpha_composite(img, overlay)
    d = ImageDraw.Draw(img)

    items = [
        ("  New pane",    TEXT,  False),
        ("  New window",  TEXT,  False),
        ("  New session", TEXT,  False),
        (None,            None,  True),
        ("Cancel",        DIM,   False),
    ]
    row_h, sep_h = 52, 8
    total_h = sum(sep_h if sep else row_h for _, _, sep in items) + 12
    bx, bw = 55, 280
    by = (H - total_h) // 2 - 20
    draw_rect(d, bx, by, bx + bw, by + total_h, fill=BG2, outline=BORDER, radius=14)

    ry = by + 6
    icons = ["[+]", "[ ]", "[=]"]
    ic = 0
    for label, color, is_sep in items:
        if is_sep:
            draw_line(d, bx + 10, ry + 1, bx + bw - 10, ry + 1, BORDER)
            ry += sep_h
        else:
            if ic < len(icons):
                draw_text(d, bx + 16, ry + 16, icons[ic], F15, ACCENT)
                ic += 1
            draw_text(d, bx + 58, ry + 16, label, F15, color)
            ry += row_h

    return img.resize((W, H), Image.LANCZOS), "03_create_overlay.png"


def screen_lock():
    """Screen 4: Biometric lock screen."""
    img = new_canvas()
    h_bot = draw_header(img, ctx_name="bash", pane_idx=2, pane_total=3)
    d = ImageDraw.Draw(img)
    draw_statusbar(d, h_bot)

    overlay = Image.new("RGBA", (SW, SH), (10, 10, 10, int(0.97 * 255)))
    img = Image.alpha_composite(img, overlay)
    d = ImageDraw.Draw(img)

    bx, bw = 45, 300
    by, bh = 240, 300
    draw_rect(d, bx, by, bx + bw, by + bh, fill=BG2, outline=BORDER, radius=14)

    # Greyed SVG logo
    logo = get_logo_img(44)
    grey = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    for px_data in [(i, logo.getpixel((i % logo.width, i // logo.width)))
                    for i in range(logo.width * logo.height)]:
        pass  # use convert instead
    grey_logo = logo.convert("LA").convert("RGBA")
    # dim it
    r, g, b, a = grey_logo.split()
    from PIL import ImageEnhance
    grey_logo = ImageEnhance.Brightness(grey_logo).enhance(0.5)
    lx = px(bx + bw // 2) - logo.width // 2
    img.paste(grey_logo, (lx, px(by + 20)), grey_logo)
    d = ImageDraw.Draw(img)

    # Padlock drawn geometrically
    draw_padlock(d, bx + bw // 2, by + 100, size=28, body_color=DIM, shackle_color=DIM)

    # LOCKED label
    locked = "LOCKED"
    draw_text(d, bx + bw // 2 - tw(locked, F18B) // 2,
              by + 148, locked, F18B, ACCENT)

    # Unlock button
    draw_rect(d, bx + 14, by + 188, bx + bw - 14, by + 230,
              fill=ACCENT, radius=8)
    ul = "Unlock"
    draw_text(d, bx + bw // 2 - tw(ul, F15B) // 2,
              by + 202, ul, F15B, "#ffffff")

    # Disable lock
    dl = "Disable lock"
    draw_text(d, bx + bw // 2 - tw(dl, F13S) // 2,
              by + 250, dl, F13S, DIM)

    return img.resize((W, H), Image.LANCZOS), "04_lock_screen.png"


def screen_text_mode():
    """Screen 5: Text / spell-check mode — Claude Code session."""
    img = new_canvas()
    h_bot = draw_header(img, ctx_name="claude", pane_idx=1, pane_total=3)
    d = ImageDraw.Draw(img)
    sb_bot = draw_statusbar(d, h_bot)
    draw_output(d, sb_bot, SL_TOP, CLAUDE_LINES)
    draw_byobu_statusline(d, SL_TOP, IB_TOP)
    draw_inputbar(img, IB_TOP, text_mode=True,
                  input_text="How do I pair a new device?")
    return img.resize((W, H), Image.LANCZOS), "05_text_mode.png"


# ── main ───────────────────────────────────────────────────────────────────

def generate_all(out_dir="screenshots"):
    os.makedirs(out_dir, exist_ok=True)
    screens = [
        screen_main,
        screen_pairing,
        screen_create_overlay,
        screen_lock,
        screen_text_mode,
    ]
    paths = []
    for fn in screens:
        img, name = fn()
        path = os.path.join(out_dir, name)
        img.save(path, "PNG")
        print(f"  wrote {path}  ({img.size[0]}x{img.size[1]})")
        paths.append(path)
    return paths


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate Trustmux PWA demo screenshots")
    parser.add_argument("--out-dir", default="screenshots")
    args = parser.parse_args()
    print(f"Generating screenshots -> {args.out_dir}/")
    paths = generate_all(args.out_dir)
    print(f"Done. {len(paths)} PNG files written.")
