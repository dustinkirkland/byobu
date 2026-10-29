"""Real-Android scenarios for the trustmux PWA.

Run through run.sh, never directly on a host. Inside the container an x86_64
Android emulator (KVM) runs real Chrome against a real trustmux daemon and
tmux. Input goes in through adb as real touches, so Chrome's own touch
selection (long press, the selection handles, autoscroll) is what gets
exercised -- the part headless Chromium can't do. Results are read back over
Chrome's remote debugging protocol.

Coordinates: page code works in CSS pixels relative to the viewport; adb
works in screen pixels. calibrate() finds the mapping by tapping a known
screen point and reading where the page saw it.
"""
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request

import websocket

PORT = "8443"
DEVTOOLS = 9222
SHOTS = os.environ.get("SHOTS", "/tmp/shots")


def sh(*cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True).stdout.strip()


def adb(*args, check=True):
    return sh("adb", *args, check=check)


def tmux_fmt(target, fmt):
    return sh("tmux", "display-message", "-p", "-t", target, fmt)


# ── Chrome DevTools ───────────────────────────────────────────────────────
class Cdp:
    def __init__(self):
        self.ws = None
        self.n = 0
        self.connect()

    def connect(self):
        deadline = time.time() + 30
        while True:
            try:
                targets = json.load(urllib.request.urlopen(f"http://localhost:{DEVTOOLS}/json"))
                page = next(t for t in targets if t["type"] == "page"
                            and f":{PORT}" in t["url"])
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"],
                                              suppress_origin=True)

    def send(self, method, params=None):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]

    def js(self, expr):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        if "exceptionDetails" in r:
            raise RuntimeError(f"js failed: {r['exceptionDetails']}")
        return r["result"].get("value")


# ── device ────────────────────────────────────────────────────────────────
class Device:
    """Screen-pixel touches through adb, and the CSS->screen mapping."""

    def __init__(self, cdp):
        self.cdp = cdp
        self.dpr = cdp.js("window.devicePixelRatio")
        self.ox = self.oy = 0.0

    def calibrate(self):
        # Record where the page sees a touch, then tap a known screen point
        # in the middle of #output.
        self.cdp.js("""window.__cal = null; document.addEventListener('touchstart',
            e => { window.__cal = [e.touches[0].clientX, e.touches[0].clientY]; },
            { capture: true, once: true })""")
        sx, sy = 540, 1300
        adb("shell", "input", "tap", str(sx), str(sy))
        for _ in range(40):
            got = self.cdp.js("window.__cal")
            if got:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("calibration tap never reached the page")
        self.ox = sx - got[0] * self.dpr
        self.oy = sy - got[1] * self.dpr
        time.sleep(0.6)

    def screen(self, x, y):
        return round(self.ox + x * self.dpr), round(self.oy + y * self.dpr)

    def touch_script(self, steps):
        """Run a list of ('down'|'move'|'up', sx, sy) / ('sleep', s) steps
        in one adb shell, so the timing between them is the device's."""
        cmds = []
        for st in steps:
            if st[0] == "sleep":
                cmds.append(f"sleep {st[1]}")
            else:
                cmds.append(f"input motionevent {st[0].upper()} {st[1]} {st[2]}")
        adb("shell", "; ".join(cmds))

    def shot(self, name):
        os.makedirs(SHOTS, exist_ok=True)
        with open(os.path.join(SHOTS, name + ".png"), "wb") as f:
            f.write(subprocess.run(["adb", "exec-out", "screencap", "-p"],
                                   capture_output=True, check=True).stdout)


# ── pane content ──────────────────────────────────────────────────────────
WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
         "lima mike november oscar papa quebec romeo sierra tango").split()


def sample_lines(n=90):
    out = []
    for i in range(1, n + 1):
        k = 2 + (i * 7) % 5
        out.append(f"L{i:02d} " + " ".join(WORDS[(i + j) % len(WORDS)] for j in range(k)))
    return out


def fill_pane(lines):
    tty = tmux_fmt("e2e.0", "#{pane_tty}")
    with open(tty, "w") as f:
        f.write("\x1b[2J\x1b[H" + "\r\n".join(lines) + "\r\n")


# ── selection probes (page side) ──────────────────────────────────────────
SEL_INFO = """(() => {
  const s = window.getSelection();
  if (!s.rangeCount || s.isCollapsed) return null;
  const r = s.getRangeAt(0), rects = [...r.getClientRects()].filter(q => q.width > 0);
  const first = rects[0], last = rects[rects.length - 1];
  return { text: String(s),
           start: first && [first.left, first.top, first.bottom],
           end: last && [last.right, last.top, last.bottom],
           scrollTop: document.getElementById('output').scrollTop };
})()"""


def line_rect(cdp, prefix):
    """CSS-pixel rect of the #output line starting with prefix."""
    return cdp.js("""(() => {
      // The latest copy: earlier ones can still be in scrollback.
      for (const el of [...document.getElementById('output').children].reverse())
        if (el.textContent.startsWith(%s)) {
          const r = el.getBoundingClientRect();
          return [r.left, r.top, r.right, r.bottom];
        }
      return null; })()""" % json.dumps(prefix))


def sel_lines(text):
    return sorted({l.split()[0] for l in (text or "").splitlines() if l.strip()[:1] == "L"})


def long_press_word(cdp, dev, prefix, dx=None):
    """Long press the first word after the line's label (or dx CSS px in),
    scrolling it into the middle of #output first if it's out of view."""
    r = line_rect(cdp, prefix)
    box = cdp.js("(() => { const b = document.getElementById('output')"
                 ".getBoundingClientRect(); return [b.top, b.bottom]; })()")
    if not (box[0] + 20 < r[1] and r[3] < box[1] - 20):
        cdp.js("document.getElementById('output').scrollTop += %f"
               % (r[1] - (box[0] + box[1]) / 2))
        time.sleep(0.5)
        r = line_rect(cdp, prefix)
    if dx is None:
        # Middle of the word after "Lnn ": a press on a space selects nothing.
        cw = cdp.js("(() => { const s = document.createElement('span'); s.textContent = 'x'.repeat(10);"
                    " document.getElementById('output').appendChild(s);"
                    " const w = s.getBoundingClientRect().width / 10; s.remove(); return w; })()")
        dx = cw * (len(prefix) + 1.5)
    x, y = dev.screen(r[0] + dx, (r[1] + r[3]) / 2)
    dev.touch_script([("down", x, y), ("sleep", 0.9), ("up", x, y)])
    time.sleep(0.8)
    return cdp.js(SEL_INFO)


def drag_handle(cdp, dev, which, lines, ms=700, settle=0.8):
    """Drag the start or end selection handle by `lines` text lines, at a
    steady finger pace. Chrome draws the end handle's bulb below and to the
    right of the selection end, the start handle's below and to the left."""
    s = cdp.js(SEL_INFO)
    if which == "end":
        px, py = dev.screen(s["end"][0], s["end"][2])
        hx = px + 28
    else:
        px, py = dev.screen(s["start"][0], s["start"][2])
        hx = px - 28
    hy = py + 30
    lh = (s["end"][2] - s["end"][1]) * dev.dpr
    dy = round(lines * lh)
    n = max(1, abs(dy) // 4)
    steps = [("down", hx, hy), ("sleep", 0.15)]
    for i in range(1, n + 1):
        steps += [("move", hx, hy + dy * i // n), ("sleep", round(ms / 1000 / n, 3))]
    steps += [("sleep", 0.1), ("up", hx, hy + dy)]
    dev.touch_script(steps)
    time.sleep(settle)
    return cdp.js(SEL_INFO)


def dismiss_selection(cdp, dev):
    """Tap blank space right of the text, as a person dismisses a selection.
    removeAllRanges() alone leaves Chrome's touch-selection UI in selection
    mode, and the next long press only dismisses it."""
    box = cdp.js("(() => { const b = document.getElementById('output')"
                 ".getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom]; })()")
    x, y = dev.screen(box[2] - 12, (box[1] + box[3]) / 2)
    adb("shell", "input", "tap", str(x), str(y))
    time.sleep(0.5)
    cdp.js("window.getSelection().removeAllRanges()")
    time.sleep(0.3)


# ── harness ───────────────────────────────────────────────────────────────
def start_backend():
    """tmux + trustmux daemon, paired with the emulator's Chrome. Returns the
    pairing process (kept alive until the end)."""
    sh("tmux", "new-session", "-d", "-s", "e2e", "-x", "100", "-y", "30")
    out = sh("trustmux", "start-direct", "--port", PORT)
    if "started" not in out:
        raise RuntimeError(f"trustmux start-direct failed:\n{out}")
    pair = subprocess.Popen(["trustmux", "pair"], stdout=subprocess.PIPE, text=True,
                            env=dict(os.environ, PYTHONUNBUFFERED="1"))
    deadline = time.time() + 20
    for line in pair.stdout:
        if "Connect:" in line and "#" in line:
            # The emulator reaches the daemon through `adb reverse`, as
            # localhost (a secure context, like the phone's https origin).
            return pair, f"https://localhost:{PORT}/#" + line.split("#")[-1].strip()
        if time.time() > deadline:
            break
    raise RuntimeError("no pairing link from `trustmux pair`")


def open_chrome(url):
    adb("reverse", f"tcp:{PORT}", f"tcp:{PORT}")
    adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url,
        "com.android.chrome")
    adb("forward", f"tcp:{DEVTOOLS}", "localabstract:chrome_devtools_remote")
    cdp = Cdp()
    deadline = time.time() + 30
    while not cdp.js("!!document.getElementById('output') && "
                     "document.getElementById('output').className !== 'placeholder'"):
        if time.time() > deadline:
            raise RuntimeError("trustmux didn't come up in Chrome")
        time.sleep(0.5)
    cdp.js("document.getElementById('bio-never-btn') && document.getElementById('bio-never-btn').click()")
    return cdp


SCENARIOS = []


def scenario(fn):
    SCENARIOS.append(fn)
    return fn


class Ctx:
    def __init__(self, cdp, dev):
        self.cdp, self.dev = cdp, dev

    def reset(self, scroll_up=0):
        dismiss_selection(self.cdp, self.dev)
        self.cdp.js("scrollOutputToBottom()")
        time.sleep(0.8)
        if scroll_up:
            self.cdp.js(f"document.getElementById('output').scrollTop -= {scroll_up}")
            time.sleep(0.6)

    def drag(self, line, which, lines, ms=700):
        s = long_press_word(self.cdp, self.dev, line)
        if not s:
            raise AssertionError(f"long press on {line.strip()} selected nothing")
        s0 = s["scrollTop"]
        s2 = drag_handle(self.cdp, self.dev, which, lines, ms=ms)
        if not s2:
            raise AssertionError("the selection disappeared during the drag")
        return sel_lines(s2["text"]), s2["scrollTop"] - s0


def expect_lines(got, lo, hi, first=None, last=None):
    """got: the Lnn labels of the lines selected after the first (partial)
    line. Between lo and hi of them, optionally bounded by first/last."""
    n = len(got)
    ok = lo <= n <= hi
    if first and got and got[0] < first:
        ok = False
    if last and got and got[-1] > last:
        ok = False
    if not ok:
        raise AssertionError(f"selected {n} lines {got[:1]}..{got[-1:]}, expected {lo}-{hi}"
                             + (f" within {first}..{last}" if first or last else ""))


def expect_scroll(moved, lines_max, line_px=13.7):
    if abs(moved) > lines_max * line_px + 1:
        raise AssertionError(f"#output scrolled {moved:.0f}px during the drag, "
                             f"more than {lines_max} lines")


@scenario
def drag_three_lines_down(ctx):
    ctx.reset()
    got, moved = ctx.drag("L80 ", "end", 3)
    expect_lines(got, 1, 4, "L81", "L84")
    expect_scroll(moved, 0)


@scenario
def drag_start_up_past_the_top_edge(ctx):
    # Used to select the whole scrollback: the handle left #output and the
    # selection start landed on the nav bar.
    ctx.reset()
    got, moved = ctx.drag("L60 ", "start", -5)
    expect_lines(got, 2, 8, "L52", "L60")
    expect_scroll(moved, 3)


@scenario
def drag_end_down_past_the_bottom_edge(ctx):
    ctx.reset()
    got, moved = ctx.drag("L84 ", "end", 12, ms=1500)
    expect_lines(got, 2, 7, "L85", "L90")
    expect_scroll(moved, 0)


@scenario
def scrolled_up_drag_end_down_past_the_bottom(ctx):
    # Used to jump #output to the end and select everything below.
    ctx.reset(scroll_up=600)
    got, moved = ctx.drag("L50 ", "end", 8, ms=1200)
    expect_lines(got, 2, 12, "L51", "L62")
    expect_scroll(moved, 4)


@scenario
def scrolled_up_drag_start_up(ctx):
    # Used to jump #output to the end even though the drag went up.
    ctx.reset(scroll_up=900)
    got, moved = ctx.drag("L30 ", "start", -8, ms=1200)
    expect_lines(got, 2, 10, "L20", "L30")
    expect_scroll(moved, 4)


@scenario
def finger_scroll_still_scrolls_with_a_selection(ctx):
    ctx.reset(scroll_up=900)
    s = long_press_word(ctx.cdp, ctx.dev, "L30 ")
    if not s:
        raise AssertionError("long press selected nothing")
    before = ctx.cdp.js("document.getElementById('output').scrollTop")
    r = line_rect(ctx.cdp, "L30 ")
    x, y = ctx.dev.screen(300, r[1] - 120)
    ctx.dev.touch_script([("down", x, y)] + [("move", x, y + k * 40) for k in range(1, 9)]
                         + [("up", x, y + 320)])
    time.sleep(1.5)
    after = ctx.cdp.js("document.getElementById('output').scrollTop")
    if before - after < 60:
        raise AssertionError(f"a finger scroll moved #output {before - after:.0f}px; "
                             "it should scroll freely while text is selected")


@scenario
def drag_while_the_pane_is_printing(ctx):
    ctx.reset()
    stop = []

    def printer():
        tty = tmux_fmt("e2e.0", "#{pane_tty}")
        i = 0
        while not stop:
            with open(tty, "w") as f:
                f.write(f"P{i:03d} printing while you select\r\n")
            i += 1
            time.sleep(0.3)
    import threading
    t = threading.Thread(target=printer, daemon=True)
    t.start()
    try:
        time.sleep(1)
        got, _ = ctx.drag("L80 ", "end", 3)
        expect_lines(got, 1, 4, "L81", "L84")
    finally:
        stop.append(1)
        t.join()
        fill_pane(sample_lines())
        time.sleep(1.5)


def set_wrap(cdp, on):
    label = "document.getElementById('escape-popup-wrap').textContent"
    if cdp.js(label).startswith("✓") != on:
        cdp.js("document.getElementById('btn-escape').click()")
        time.sleep(0.3)
        cdp.js("document.getElementById('escape-popup-wrap').click()")
        time.sleep(0.8)


@scenario
def wrap_mode_drag_start_up_past_the_top_edge(ctx):
    # Aa mode wraps long lines; a capture line is then several screen rows.
    set_wrap(ctx.cdp, True)
    try:
        ctx.reset()
        got, moved = ctx.drag("L60 ", "start", -5)
        expect_lines(got, 2, 8, "L52", "L60")
        expect_scroll(moved, 3)
        ctx.reset(scroll_up=600)
        got, moved = ctx.drag("L50 ", "end", 8, ms=1200)
        expect_lines(got, 2, 12, "L51", "L62")
        expect_scroll(moved, 4)
    finally:
        dismiss_selection(ctx.cdp, ctx.dev)
        set_wrap(ctx.cdp, False)


def main(filters):
    chosen = [s for s in SCENARIOS if not filters or any(f in s.__name__ for f in filters)]
    # REPEAT=N runs the chosen scenarios N times over, for flaky behavior.
    chosen = chosen * int(os.environ.get("REPEAT", "1"))
    pair, url = start_backend()
    cdp = open_chrome(url)
    dev = Device(cdp)
    fill_pane(sample_lines())
    time.sleep(2)
    cdp.js("scrollOutputToBottom()")
    time.sleep(0.8)
    dev.calibrate()
    ctx = Ctx(cdp, dev)
    results = []
    for s in chosen:
        try:
            s(ctx)
            results.append((s.__name__, None))
            print(f"PASS  {s.__name__}", flush=True)
        except Exception as e:
            results.append((s.__name__, e))
            print(f"FAIL  {s.__name__}: {e}", flush=True)
            if not isinstance(e, AssertionError):
                traceback.print_exc()
        dev.shot(s.__name__)
    pair.kill()
    sh("trustmux", "stop", check=False)
    failed = [n for n, e in results if e]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed"
          + (f": {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
