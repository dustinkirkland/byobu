"""Browser end-to-end scenarios for the trustmux PWA.

Run through run.sh, never directly on a host: it starts its own tmux server
and trustmux daemon.  Two tmux panes run a raw-mode logger that records every
byte it receives; Chromium (phone-sized, touch) pairs with the daemon and
taps the real UI; each scenario asserts the exact bytes a pane got.

To add a scenario, write a function decorated with @scenario that drives
`ui` and calls expect().  Scenarios run in file order and share one
browser session, so each must leave the UI as it found it: terminal
keyboard mode, key bar closed, pane 0 selected, text box empty.

Timing: never sleep in place of "the browser caught up".  Wait for the
condition itself (wait_until/wait_stable, a locator wait, a page-side
counter), because under load anything can take longer than a guess.  A
sleep is right only for pacing a gesture or for a window in which
something must NOT happen.  Gestures the app times (swipes, triple taps)
are timed by what the page saw (see GESTURES); one that load stretched
past the app's limits raises GestureMissed and the scenario runs again.
Any change here must pass the load gate in run.sh with no failures.
"""
import json
import os
import subprocess
import sys
import time
import traceback

from playwright.sync_api import sync_playwright

WORK = os.path.expanduser("~/e2e")
LOGGER = os.path.join(WORK, "logger.py")
PORT = "8443"

# Stands in for speechSynthesis: records each utterance (text and rate) and
# each cancel on <html data-tts-log>, readable through a locator (the app's
# CSP rules out evaluating JS to read it). Each utterance "takes" 500ms,
# or until window.__ttsHold is cleared (set over CDP) while that is true.
TTS_MOCK = r"""
(() => {
  const log = [];
  const write = () => document.documentElement.setAttribute('data-tts-log', JSON.stringify(log));
  let timer = 0;
  class Utterance { constructor(text) { this.text = text; } }
  const synth = {
    speak(u) { log.push({ t: u.text, r: u.rate }); write();
               const done = () => window.__ttsHold ? (timer = setTimeout(done, 50))
                                                   : u.onend && u.onend();
               timer = setTimeout(done, 500); },
    cancel() { clearTimeout(timer); log.push('CANCEL'); write(); },
    resume() {}, pause() {}, getVoices() { return []; },
    addEventListener() {}, speaking: false,
  };
  Object.defineProperty(window, 'speechSynthesis', { value: synth, configurable: true });
  Object.defineProperty(window, 'SpeechSynthesisUtterance', { value: Utterance, configurable: true });
})();
"""

LOGGER_SRC = r"""
import os, sys, tty
log, banner = sys.argv[1], sys.argv[2]
print(banner, flush=True)
tty.setraw(0)
with open(log, "ab", buffering=0) as f:
    while True:
        b = os.read(0, 4096)
        if not b:
            break
        f.write(b)
"""


def sh(*cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True).stdout.strip()


def wait_until(fn, what, timeout=10, every=0.05):
    """Poll fn() until it returns something true, and return that."""
    end = time.time() + timeout
    while True:
        got = fn()
        if got:
            return got
        if time.time() > end:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(every)


def wait_stable(fn, what, quiet=0.3, timeout=10, every=0.05):
    """Poll fn() until its value has held for `quiet` seconds; return it."""
    end = time.time() + timeout
    last, since = fn(), time.time()
    while time.time() - since < quiet:
        if time.time() > end:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(every)
        cur = fn()
        if cur != last:
            last, since = cur, time.time()
    return last


class GestureMissed(Exception):
    """Load stretched a gesture past the app's own time limits, so the app
    rightly ignored it and the scenario proves nothing; main() runs it again."""


# Page-side record of the input the app saw, in the app's own clock: when
# each touch and primary pointer went down and up, every navigateRelative()
# call, and every #output scroll event.  Capture listeners on window run in
# the same dispatch as the app's handlers, so once a count here moves, the
# app has handled that event too.  A reload wipes it; gestures() reinstalls.
GESTURES = """(() => {
  if (window.__g) return 0;
  const g = window.__g = {ts: [], te: [], pd: [], pu: [], nav: 0, scrolls: 0};
  const at = a => () => a.push(performance.now());
  addEventListener('touchstart', at(g.ts), {capture: true, passive: true});
  addEventListener('touchend', at(g.te), {capture: true, passive: true});
  addEventListener('pointerdown', e => e.isPrimary && g.pd.push(performance.now()), true);
  addEventListener('pointerup', e => e.isPrimary && g.pu.push(performance.now()), true);
  const nav = navigateRelative;
  navigateRelative = (...a) => { g.nav++; return nav(...a); };
  document.getElementById('output').addEventListener('scroll', () => { g.scrolls++; },
                                                     {passive: true});
  return 1;
})()"""


def gestures(ui):
    js(ui, GESTURES)
    return js(ui, "window.__g")


class Pane:
    """A tmux pane running the logger; received() is what arrived since mark()."""

    def __init__(self, index):
        self.index = index
        self.log = os.path.join(WORK, f"pane{index}.log")
        self.banner = f"E2E-PANE-{index}-READY"
        self._offset = 0

    def command(self):
        return f"python3 {LOGGER} {self.log} {self.banner}"

    def mark(self):
        self._offset = os.path.getsize(self.log) if os.path.exists(self.log) else 0

    def received(self):
        if not os.path.exists(self.log):
            return b""
        with open(self.log, "rb") as f:
            f.seek(self._offset)
            return f.read()


def start_backend():
    os.makedirs(WORK, exist_ok=True)
    with open(LOGGER, "w") as f:
        f.write(LOGGER_SRC)
    panes = [Pane(0), Pane(1)]
    sh("tmux", "new-session", "-d", "-s", "e2e", "-x", "100", "-y", "30", panes[0].command())
    sh("tmux", "split-window", "-t", "e2e", panes[1].command())
    sh("tmux", "select-pane", "-t", "e2e.0")
    out = sh("trustmux", "start-direct", "--port", PORT)
    if "started" not in out:
        raise RuntimeError(f"trustmux start-direct failed:\n{out}")
    # `trustmux pair` prints the pairing link, then waits for the device.
    pair = subprocess.Popen(["trustmux", "pair"], stdout=subprocess.PIPE, text=True,
                            env=dict(os.environ, PYTHONUNBUFFERED="1"))
    deadline = time.time() + 20
    for line in pair.stdout:
        if "Connect:" in line and "#" in line:
            return panes, line.split()[-1], pair
        if time.time() > deadline:
            break
    raise RuntimeError("no pairing link from `trustmux pair`")


def open_ui(browser, url, banner):
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844},
                              has_touch=True, is_mobile=True)
    ctx.add_init_script(TTS_MOCK)
    page = ctx.new_page()
    page.goto(url)
    wait_output(page, banner, timeout=20000)
    # The one-time "Lock Trustmux?" prompt: the app shows it (if WebAuthn
    # is there at all) in the same task that starts connecting, so by the
    # time the pane's output is up it is either showing or never will be.
    if page.locator("#bio-setup-overlay").is_visible():
        page.tap("#bio-never-btn")
        page.wait_for_selector("#bio-setup-overlay", state="hidden", timeout=10000)
    return page


def expect(pane, want, timeout=10.0):
    """Wait for pane to have received exactly `want` (and nothing more)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = pane.received()
        if got == want and want:
            break
        if len(got) > len(want):
            break
        time.sleep(0.05)
    time.sleep(0.4)   # a window for anything extra (a failure) to show up in
    got = pane.received()
    if got != want:
        raise AssertionError(f"pane {pane.index} received {got!r}, expected {want!r}")


def wait_output(ui, text, timeout=8000):
    # Locator waits, not wait_for_function: the app's CSP (rightly) forbids
    # the eval that would need, and the tests should run under the real CSP.
    ui.locator("#output", has_text=text).wait_for(timeout=timeout)


def popup(ui, button, item):
    ui.tap(button)
    ui.wait_for_selector(item, state="visible", timeout=3000)
    ui.tap(item)


def focus_cmd(ui):
    # In direct (key bar) mode the text box is collapsed out of the layout but
    # still focused for the soft keyboard; Playwright can't tap it, so focus
    # it directly and type through the keyboard.
    ui.locator("#cmd").focus()


SCENARIOS = []


def scenario(fn):
    SCENARIOS.append(fn)
    return fn


@scenario
def send_text(ui, p0, p1):
    ui.fill("#cmd", "hello")
    ui.tap("#btn-send")
    expect(p0, b"hello\r")


@scenario
def send_empty_is_enter(ui, p0, p1):
    ui.tap("#btn-send")
    expect(p0, b"\r")


@scenario
def keyboard_enter_sends_text(ui, p0, p1):
    ui.fill("#cmd", "abc")
    ui.press("#cmd", "Enter")
    expect(p0, b"abc\r")


@scenario
def keyboard_enter_empty_is_enter(ui, p0, p1):
    ui.press("#cmd", "Enter")
    expect(p0, b"\r")


@scenario
def shift_enter_is_newline_not_send(ui, p0, p1):
    ui.fill("#cmd", "line1")
    ui.press("#cmd", "Shift+Enter")
    expect(p0, b"")
    if "\n" not in ui.input_value("#cmd"):
        raise AssertionError("Shift+Enter didn't add a newline to the box")
    ui.fill("#cmd", "")


@scenario
def escape_popup_esc(ui, p0, p1):
    popup(ui, "#btn-escape", "#escape-popup-esc")
    expect(p0, b"\x1b")


@scenario
def escape_popup_ctrl_c(ui, p0, p1):
    popup(ui, "#btn-escape", "#escape-popup-ctrlc")
    expect(p0, b"\x03")


@scenario
def keybar_keys(ui, p0, p1):
    popup(ui, "#btn-escape", "#escape-popup-keys")
    ui.wait_for_selector("#keybar", state="visible", timeout=3000)
    want = b""
    for key, sends in (("Escape", b"\x1b"), ("Tab", b"\t"), ("Left", b"\x1b[D"),
                       ("BSpace", b"\x7f"), ("Enter", b"\r")):
        ui.tap(f'#keybar button[data-key="{key}"]')
        want += sends
        # Each key lands before the next is tapped, so the order is fixed.
        wait_until(lambda: p0.received() == want or len(p0.received()) > len(want),
                   f"the pane to receive {want!r}")
    expect(p0, b"\x1b\t\x1b[D\x7f\r")
    ui.tap("#keybar-close")
    ui.wait_for_selector("#keybar", state="hidden", timeout=3000)


@scenario
def direct_mode_streams_keystrokes(ui, p0, p1):
    popup(ui, "#btn-escape", "#escape-popup-keys")
    ui.wait_for_selector("#keybar", state="visible", timeout=3000)
    focus_cmd(ui)
    ui.keyboard.type("xyz", delay=60)
    expect(p0, b"xyz")            # streamed as typed, no Enter yet
    p0.mark()
    ui.keyboard.press("Backspace")
    expect(p0, b"\x7f")
    p0.mark()
    ui.keyboard.press("Enter")
    expect(p0, b"\r")
    ui.tap("#keybar-close")
    ui.wait_for_selector("#keybar", state="hidden", timeout=3000)


@scenario
def keybar_ctrl_combines(ui, p0, p1):
    popup(ui, "#btn-escape", "#escape-popup-keys")
    ui.wait_for_selector("#keybar", state="visible", timeout=3000)
    ui.tap("#keybar-ctrl")
    focus_cmd(ui)
    ui.keyboard.type("c")
    expect(p0, b"\x03")
    ui.tap("#keybar-close")
    ui.wait_for_selector("#keybar", state="hidden", timeout=3000)


@scenario
def password_mode(ui, p0, p1):
    popup(ui, "#btn-kbd-mode", "#kbdmode-popup-password")
    ui.wait_for_selector("#pwd", state="visible", timeout=3000)
    ui.tap("#btn-send")           # empty: must NOT send a bare Enter
    expect(p0, b"")
    ui.fill("#pwd", "s3cret")
    ui.tap("#btn-send")
    expect(p0, b"s3cret\r")
    if ui.input_value("#pwd"):
        raise AssertionError("password box not cleared after Send")
    popup(ui, "#btn-kbd-mode", "#kbdmode-popup-terminal")
    ui.wait_for_selector("#cmd", state="visible", timeout=3000)


@scenario
def pane_switching_targets_the_right_pane(ui, p0, p1):
    ui.tap("#btn-next")
    wait_output(ui, p1.banner)
    ui.fill("#cmd", "two")
    ui.tap("#btn-send")
    expect(p1, b"two\r")
    expect(p0, b"")
    ui.tap("#btn-prev")
    wait_output(ui, p0.banner)
    p0.mark()
    ui.fill("#cmd", "one")
    ui.tap("#btn-send")
    expect(p0, b"one\r")


def tmux_fmt(target, fmt):
    return sh("tmux", "display-message", "-p", "-t", target, fmt)


def wait_seg(ui, cls, want, timeout=10.0):
    """Wait for header segment .ctx-seg-<cls> to read `want` (None: absent)."""
    loc = ui.locator(f"#ctx-name .ctx-seg-{cls}")
    deadline = time.time() + timeout
    got = None
    while time.time() < deadline:
        got = loc.inner_text() if loc.count() else None
        if got == want:
            return
        time.sleep(0.1)
    raise AssertionError(f"header {cls} segment is {got!r}, expected {want!r}")


def wait_tmux(target, fmt, want, timeout=10.0):
    deadline = time.time() + timeout
    got = None
    while time.time() < deadline:
        got = tmux_fmt(target, fmt)
        if got == want:
            return
        time.sleep(0.1)
    raise AssertionError(f"tmux {fmt} on {target} is {got!r}, expected {want!r}")


def rename_via_ui(ui, seg, name):
    ui.tap(f"#ctx-name .ctx-seg-{seg}")
    ui.wait_for_selector("#ctx-rename-open", state="visible", timeout=3000)
    ui.tap("#ctx-rename-open")
    ui.wait_for_selector("#ctx-rename-input", state="visible", timeout=3000)
    ui.fill("#ctx-rename-input", name)
    ui.tap("#ctx-rename-confirm")
    ui.wait_for_selector("#ctx-overlay", state="hidden", timeout=3000)


@scenario
def header_is_session_number_window_name_pane(ui, p0, p1):
    sid = tmux_fmt("e2e", "#{session_id}").lstrip("$")
    wait_seg(ui, "session", sid)
    wait_seg(ui, "window", tmux_fmt("e2e:0", "#{window_name}"))
    if ui.locator("#ctx-name .ctx-seg-pane").count() != 1:
        raise AssertionError("split window should show a pane segment")


@scenario
def window_rename_in_app_reaches_tmux(ui, p0, p1):
    rename_via_ui(ui, "window", "editor")
    wait_tmux("e2e:0", "#{window_name}", "editor")
    wait_seg(ui, "window", "editor")
    rename_via_ui(ui, "window", "")          # back to automatic naming
    wait_tmux("e2e:0", "#{automatic-rename}", "1")
    wait_seg(ui, "window", tmux_fmt("e2e:0", "#{window_name}"))


@scenario
def window_rename_in_tmux_reaches_app(ui, p0, p1):
    sh("tmux", "rename-window", "-t", "e2e:0", "fromtmux")
    wait_seg(ui, "window", "fromtmux")
    sh("tmux", "set-option", "-wu", "-t", "e2e:0", "automatic-rename")
    wait_seg(ui, "window", tmux_fmt("e2e:0", "#{window_name}"))


@scenario
def pane_rename_in_app_sets_locked_tmux_title(ui, p0, p1):
    pane = tmux_fmt("e2e.0", "#{pane_id}")
    rename_via_ui(ui, "pane", "left side")
    wait_tmux(pane, "#{pane_title}", "left side")
    wait_tmux(pane, "#{allow-set-title}", "0")
    wait_seg(ui, "pane", "left side")
    rename_via_ui(ui, "pane", "")
    wait_tmux(pane, "#{allow-set-title}", "1")
    wait_tmux(pane, "#{pane_title}", "")
    expect(p0, b"")                          # nothing typed into the pane


@scenario
def pane_title_set_in_tmux_reaches_app(ui, p0, p1):
    pane = tmux_fmt("e2e.0", "#{pane_id}")
    sh("tmux", "select-pane", "-t", pane, "-T", "from tmux")
    wait_seg(ui, "pane", "from tmux")
    sh("tmux", "select-pane", "-t", pane, "-T", "")


def pick(ui, window_label, pane_nth):
    """Navigate through the picker: window row, then the nth pane row."""
    ui.tap("#ctx-name .ctx-seg-window")
    ui.locator("#ctx-list .ctx-btn", has_text=window_label).tap()
    rows = ui.locator("#ctx-list .ctx-btn[data-ctx-id]")
    rows.first.wait_for(timeout=3000)
    rows.nth(pane_nth).tap()
    ui.wait_for_selector("#ctx-overlay", state="hidden", timeout=3000)


@scenario
def unsplit_window_has_no_pane_segment(ui, p0, p1):
    solo = Pane(2)
    sh("tmux", "new-window", "-d", "-t", "e2e:5", "-n", "solo", solo.command())
    try:
        # The open picker re-renders when the next topology poll brings
        # the window, and the row tap waits for it to appear.
        pick(ui, "5:solo", 0)
        wait_output(ui, solo.banner)
        wait_seg(ui, "window", "solo")
        wait_seg(ui, "pane", None)
        if ui.locator("#ctx-name .ctx-seg-sep").count() != 1:
            raise AssertionError("unsplit window should show exactly one '/'")
        pick(ui, "0:", 0)                    # back to pane 0
        wait_output(ui, p0.banner)
    finally:
        sh("tmux", "kill-window", "-t", "e2e:5", check=False)


# app.js's own limits on the gestures it times.
SWIPE_MAX_MS, SWIPE_DOUBLE_MS = 500, 700
TAP_HOLD_MS, TAP_GAP_MS = 350, 400
OUT = "document.getElementById('output')"


class Touch:
    """Real touch input through CDP, so Chromium scrolls natively exactly as
    it would under a finger (Playwright's own API has taps, not swipes).
    Each gesture returns once the page has handled its last event, with the
    times the page saw it at (see GESTURES)."""

    def __init__(self, ui):
        self.ui = ui
        gestures(ui)

    def send(self, kind, pts):
        cdp(self.ui).send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": pts})

    def swipe(self, dx, dy=0, ms=180, steps=8):
        """One swipe; returns (start, end) in the page's clock."""
        box = self.ui.locator("#output").bounding_box()
        x0 = box["x"] + box["width"] / 2 - dx / 2
        y0 = box["y"] + box["height"] / 2
        n = len(gestures(self.ui)["te"])
        self.send("touchStart", [{"x": x0, "y": y0}])
        for i in range(1, steps + 1):
            time.sleep(ms / 1000 / steps)    # the swipe's pace
            self.send("touchMove", [{"x": x0 + dx * i / steps, "y": y0 + dy * i / steps}])
        self.send("touchEnd", [])
        g = wait_until(lambda: (lambda g: len(g["te"]) > n and g)(gestures(self.ui)),
                       "the page to see the swipe end")
        return g["ts"][-1], g["te"][-1]

    def flick(self, dx, dy=0):
        """A swipe the app times as one, or GestureMissed."""
        t0, t1 = self.swipe(dx, dy)
        if t1 - t0 > SWIPE_MAX_MS:
            raise GestureMissed(f"a flick took {t1 - t0:.0f}ms")
        return t0, t1

    def drag(self, dx, dy=0):
        """A swipe too slow for the app to count, or GestureMissed."""
        t0, t1 = self.swipe(dx, dy, ms=900)
        if t1 - t0 <= SWIPE_MAX_MS:
            raise GestureMissed(f"a slow drag took only {t1 - t0:.0f}ms")
        return t0, t1

    def double(self, dx, gap=0.15):
        """Two flicks the app pairs up, or GestureMissed."""
        _, end = self.flick(dx)
        time.sleep(gap)
        start, _ = self.flick(dx)
        if start - end > SWIPE_DOUBLE_MS:
            raise GestureMissed(f"{start - end:.0f}ms between the two swipes")

    def taps(self, x, y, n):
        """n quick taps the app counts as one multi-tap, or GestureMissed."""
        k = len(gestures(self.ui)["pu"])
        for _ in range(n):
            self.send("touchStart", [{"x": x, "y": y}])
            time.sleep(0.04)                 # a tap's length
            self.send("touchEnd", [])
            time.sleep(0.12)                 # and the pause between taps
        g = wait_until(lambda: (lambda g: len(g["pu"]) >= k + n and g)(gestures(self.ui)),
                       "the page to see the taps")
        downs, ups = g["pd"][-n:], g["pu"][-n:]
        if max(u - d for d, u in zip(downs, ups)) > TAP_HOLD_MS or \
                max((b - a for a, b in zip(ups, ups[1:])), default=0) > TAP_GAP_MS:
            raise GestureMissed(f"taps seen down {downs} up {ups}")


def showing(ui, pane):
    return ui.locator("#output", has_text=pane.banner).count() == 1


def navigations(ui):
    return gestures(ui)["nav"]


def assert_stayed(ui, pane, nav):
    """No gesture since navigations() returned `nav` navigated.  The page
    handled each one before Touch returned, so this needs no waiting."""
    if navigations(ui) != nav or not showing(ui, pane):
        raise AssertionError(f"expected to still be on pane {pane.index}")


def scroll_left(ui):
    return wait_stable(lambda: js(ui, f"{OUT}.scrollLeft"), "#output to stop scrolling")


def set_wrap(ui, on):
    label = lambda: ui.locator("#escape-popup-wrap").inner_text().startswith("✓")
    if label() != on:
        popup(ui, "#btn-escape", "#escape-popup-wrap")
        wait_until(lambda: label() == on, "the wrap setting to change")
        wait_stable(lambda: js(ui, f"[{OUT}.scrollWidth, {OUT}.scrollHeight]"),
                    "#output to re-render for the wrap setting")


@scenario
def double_swipe_navigates_like_the_arrows(ui, p0, p1):
    touch = Touch(ui)
    set_wrap(ui, True)
    try:
        nav = navigations(ui)
        touch.double(-150)                   # swipe left twice: next
        if navigations(ui) != nav + 1:
            raise AssertionError("a double swipe left didn't navigate")
        wait_output(ui, p1.banner)
        touch.double(150)                    # swipe right twice: previous
        if navigations(ui) != nav + 2:
            raise AssertionError("a double swipe right didn't navigate")
        wait_output(ui, p0.banner)
    finally:
        set_wrap(ui, False)
    expect(p0, b"")
    expect(p1, b"")


@scenario
def single_or_slow_or_vertical_swipes_do_not_navigate(ui, p0, p1):
    touch = Touch(ui)
    set_wrap(ui, True)
    try:
        nav = navigations(ui)
        touch.flick(-150)                    # one swipe only
        assert_stayed(ui, p0, nav)
        touch.drag(-150)                     # slow drag, twice
        touch.drag(-150)
        assert_stayed(ui, p0, nav)
        touch.swipe(-60, dy=200)             # mostly vertical, twice
        touch.swipe(-60, dy=200)
        assert_stayed(ui, p0, nav)
        touch.flick(-150)                    # left then right: no pair
        time.sleep(0.15)                     # the pause between them
        touch.flick(150)
        assert_stayed(ui, p0, nav)
    finally:
        set_wrap(ui, False)


@scenario
def terminal_mode_sideways_scroll_is_not_a_swipe(ui, p0, p1):
    # A line wider than the phone makes #output scroll sideways in
    # Terminal mode (wrap off). Swipes that scroll it must not navigate;
    # once it is scrolled to that edge, a double swipe does.
    tty = tmux_fmt("e2e.0", "#{pane_tty}")
    with open(tty, "w") as f:
        f.write("\r\n" + "W" * 95 + "\r\n")
    touch = Touch(ui)
    set_wrap(ui, False)
    try:
        ui.locator("#output", has_text="W" * 95).wait_for(timeout=10000)
        js(ui, f"{OUT}.scrollLeft = 0")
        room = wait_stable(lambda: js(ui, f"{OUT}.scrollWidth - {OUT}.clientWidth"),
                           "the long line's layout")
        if room < 300 or scroll_left(ui) != 0:
            raise AssertionError(f"#output can't scroll sideways ({room}px)")
        nav = navigations(ui)
        try:
            touch.double(-150)               # scrolls the long line left
        except GestureMissed:
            pass                             # not navigating is the point
        assert_stayed(ui, p0, nav)
        if scroll_left(ui) < 1:
            raise AssertionError("the swipes didn't scroll the long line")
        for _ in range(12):                  # slow drags (never a swipe)
            if scroll_left(ui) >= room - 1:  # to reach the right edge
                break
            touch.drag(-300)
        else:
            raise AssertionError("slow drags never reached the right edge")
        assert_stayed(ui, p0, nav)
        touch.double(-150)                   # at the edge: navigates
        if navigations(ui) != nav + 1:
            raise AssertionError("a double swipe at the edge didn't navigate")
        wait_output(ui, p1.banner)
    finally:
        if not showing(ui, p0):
            ui.tap("#btn-prev")
            wait_output(ui, p0.banner)
        with open(tty, "w") as f:
            f.write("\x1b[2J\x1b[H" + p0.banner + "\r\n")


TTS_SAMPLE = [
    "user@host:~$ echo hi",
    "hi there friend. How are you?",
    "⏺ I updated the parser. It now handles tabs.",
    "  Second line of the same sentence",
    "  continues here.",
    "  +-+-+ ---- ===",
    "",
    "⏺ Bash(git status)",
    "  ⎿  On branch master",
    "     nothing to commit",
    "",
    "⏺ One. Two. Three. Four. Five. Six.",
    "╭──────╮",
]


def tts_log(ui):
    raw = ui.locator("html").get_attribute("data-tts-log")
    return json.loads(raw) if raw else []


def tts_spoken_since(ui, mark, timeout=20):
    """Utterances logged after index `mark`, once reading has finished.
    Reading starts inside the third tap's own handler, so once Touch.taps()
    returns it has started, or never will."""
    wait_until(lambda: ui.locator("#tts-stop").is_hidden(), "reading to finish", timeout=timeout)
    return tts_log(ui)[mark:]


def tts_spoken(ui, mark):
    return [g for g in tts_log(ui)[mark:] if g != "CANCEL"]


def pane_settled(ui):
    """Wait for #output to stop changing: a write to the pane can arrive
    as several updates, and each re-render moves lines around."""
    wait_stable(lambda: js(ui, f"[{OUT}.scrollHeight, {OUT}.scrollTop, {OUT}.children.length,"
                               f" {OUT}.lastElementChild && {OUT}.lastElementChild.textContent]"),
                "#output to settle")


def line_point(ui, text):
    """Where to touch the latest copy of `text` in #output (earlier
    scenarios' output can still be in scrollback).  Reading scrolls #output
    to follow the highlight, and earlier scenarios can leave it scrolled
    either way, so bring the line into view like a user would."""
    pane_settled(ui)
    line = ui.locator("#output > span", has_text=text).last
    out = ui.locator("#output")

    def in_view():
        line.scroll_into_view_if_needed()
        box = wait_stable(line.bounding_box, f"{text!r} to scroll into view")
        o = out.bounding_box()
        y = box["y"] + box["height"] / 2
        return (box, o) if o["y"] < y < o["y"] + o["height"] else None
    box, o = wait_until(in_view, f"{text!r} to be on screen to touch")
    # #output may be scrolled sideways (an earlier Terminal-mode scenario
    # leaves it at the right edge), so touch inside the visible part of it.
    return max(box["x"], o["x"]) + 20, box["y"] + box["height"] / 2


def triple_tap(ui, text):
    Touch(ui).taps(*line_point(ui, text), 3)


def set_tts(ui, on, rate_steps=0):
    ui.tap("#btn-settings")
    ui.wait_for_selector("#settings-tts-toggle", state="visible", timeout=3000)
    def toggle():
        if ui.locator("#settings-tts-toggle").inner_text().startswith("✓") != on:
            ui.tap("#settings-tts-toggle")
    if on:
        toggle()
    # The speed row only shows while read aloud is on.
    for _ in range(abs(rate_steps)):
        ui.tap("#settings-tts-faster" if rate_steps > 0 else "#settings-tts-slower")
    if not on:
        toggle()
    ui.tap("#settings-close")


def write_sample(p0):
    tty = tmux_fmt("e2e.0", "#{pane_tty}")
    with open(tty, "w") as f:
        f.write("\x1b[2J\x1b[H" + "\r\n".join(TTS_SAMPLE) + "\r\n")
    return tty


def restore_pane0(tty, p0):
    with open(tty, "w") as f:
        f.write("\x1b[2J\x1b[H" + p0.banner + "\r\n")


@scenario
def read_aloud_is_off_until_enabled(ui, p0, p1):
    tty = write_sample(p0)
    try:
        wait_output(ui, TTS_SAMPLE[-1])
        mark = len(tts_log(ui))
        triple_tap(ui, "Second line")
        time.sleep(0.5)                      # a window for a wrong start
        if tts_spoken_since(ui, mark):
            raise AssertionError("read aloud spoke while disabled")
    finally:
        restore_pane0(tty, p0)


@scenario
def read_aloud_reads_the_tapped_block(ui, p0, p1):
    tty = write_sample(p0)
    set_tts(ui, True)
    try:
        wait_output(ui, TTS_SAMPLE[-1])
        cases = [
            ("Second line", ["I updated the parser.", "It now handles tabs.",
                             "Second line of the same sentence continues here."]),
            ("nothing to commit", ["Bash: git status"]),
            ("hi there", ["echo hi", "hi there friend.", "How are you?"]),
        ]
        for tap_on, want in cases:
            mark = len(tts_log(ui))
            triple_tap(ui, tap_on)
            got = tts_spoken_since(ui, mark)
            texts = [g["t"] for g in got if g != "CANCEL"]
            if texts != want:
                raise AssertionError(f"tapping {tap_on!r} spoke {texts!r}, expected {want!r}")
            if any(g["r"] != 1.2 for g in got if g != "CANCEL"):
                raise AssertionError(f"expected rate 1.2, got {got!r}")
        if ui.locator(".tts-block, .tts-reading").count():
            raise AssertionError("highlight left behind after reading")
    finally:
        set_tts(ui, False)
        restore_pane0(tty, p0)
    expect(p0, b"")


@scenario
def read_aloud_highlights_and_triple_tap_stops(ui, p0, p1):
    tty = write_sample(p0)
    set_tts(ui, True)
    try:
        wait_output(ui, TTS_SAMPLE[-1])
        mark = len(tts_log(ui))
        triple_tap(ui, "Four. Five.")
        ui.wait_for_selector("#tts-stop", state="visible", timeout=2000)
        if ui.locator("#output .tts-reading", has_text="Four. Five.").count() < 1:
            raise AssertionError("the line being read isn't highlighted")
        # Into the second sentence, then hold it there: under load the
        # stopping taps could otherwise land after the last sentence.
        wait_until(lambda: len(tts_spoken(ui, mark)) >= 2, "the second sentence")
        js(ui, "window.__ttsHold = true")
        triple_tap(ui, "Four. Five.")        # stop
        ui.wait_for_selector("#tts-stop", state="hidden", timeout=2000)
        got = tts_log(ui)[mark:]
        spoken = [g for g in got if g != "CANCEL"]
        if not (1 <= len(spoken) < 6) or got[-1] != "CANCEL":
            raise AssertionError(f"triple tap didn't stop reading: {got!r}")
        js(ui, "window.__ttsHold = false")
        time.sleep(1.2)                      # a window for speaking on (a failure)
        if len(tts_log(ui)) != mark + len(got):
            raise AssertionError("kept speaking after stop")
        if ui.locator(".tts-block, .tts-reading").count():
            raise AssertionError("highlight left behind after stop")
        # The Stop pill stops it too.
        js(ui, "window.__ttsHold = true")
        triple_tap(ui, "Four. Five.")
        ui.wait_for_selector("#tts-stop", state="visible", timeout=2000)
        ui.tap("#tts-stop")
        ui.wait_for_selector("#tts-stop", state="hidden", timeout=2000)
    finally:
        js(ui, "window.__ttsHold = false")
        set_tts(ui, False)
        restore_pane0(tty, p0)


@scenario
def read_aloud_settings_persist(ui, p0, p1):
    set_tts(ui, True, rate_steps=2)          # 1.2 -> 1.4
    try:
        ui.reload()
        wait_output(ui, p0.banner, timeout=20000)
        ui.tap("#btn-settings")
        ui.wait_for_selector("#settings-tts-toggle", state="visible", timeout=3000)
        if not ui.locator("#settings-tts-toggle").inner_text().startswith("✓"):
            raise AssertionError("read aloud didn't stay enabled across a reload")
        rate = ui.locator("#settings-tts-rate-value").inner_text()
        if rate != "1.4×":
            raise AssertionError(f"speed after reload is {rate!r}, expected 1.4×")
        ui.tap("#settings-close")
        tty = write_sample(p0)
        try:
            wait_output(ui, TTS_SAMPLE[-1])
            mark = len(tts_log(ui))
            triple_tap(ui, "hi there")
            got = [g for g in tts_spoken_since(ui, mark) if g != "CANCEL"]
            if not got or any(g["r"] != 1.4 for g in got):
                raise AssertionError(f"expected rate 1.4, got {got!r}")
        finally:
            restore_pane0(tty, p0)
    finally:
        set_tts(ui, False, rate_steps=-2)


# ── text selection ────────────────────────────────────────────────────────
def cdp(ui):
    if not hasattr(ui, "_e2e_cdp"):
        ui._e2e_cdp = ui.context.new_cdp_session(ui)
    return ui._e2e_cdp


def js(ui, expr):
    """Evaluate in the page over CDP. DevTools evaluation isn't subject to the
    page's CSP, so this works without loosening it (unlike page.evaluate's
    polling helpers)."""
    r = cdp(ui).send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    if "exceptionDetails" in r:
        raise RuntimeError(f"js failed: {r['exceptionDetails']}")
    return r["result"].get("value")


SELECTED = "String(window.getSelection())"


# Selects the latest copy of a JSON string (%s) inside #output.
SELECT_JS = """(() => {
      const want = %s;
      const w = document.createTreeWalker(document.getElementById('output'),
                                          NodeFilter.SHOW_TEXT);
      let n, last = null;
      while ((n = w.nextNode())) if (n.data.includes(want)) last = n;
      if (!last) return false;
      const i = last.data.indexOf(want);
      const r = document.createRange();
      r.setStart(last, i); r.setEnd(last, i + want.length);
      const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
      return true;
    })()"""


def select_text(ui, text):
    """Select `text` inside #output, as a double tap and drag would."""
    return js(ui, SELECT_JS % json.dumps(text))


def clear_selection(ui):
    js(ui, "window.getSelection().removeAllRanges()")


def write_pane0(text):
    tty = tmux_fmt("e2e.0", "#{pane_tty}")
    with open(tty, "w") as f:
        f.write("\x1b[2J\x1b[H" + text)
    return tty


def touch(ui, kind, x=0, y=0):
    pts = [{"x": x, "y": y}] if kind != "touchEnd" else []
    cdp(ui).send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": pts})


def tap(ui, x, y):
    """One tap; returns once the page has seen it end."""
    n = len(gestures(ui)["te"])
    touch(ui, "touchStart", x, y)
    time.sleep(0.04)                         # a tap's length
    touch(ui, "touchEnd")
    wait_until(lambda: len(gestures(ui)["te"]) > n, "the page to see the tap")


def to_bottom(ui):
    """scrollOutputToBottom() scrolls in a requestAnimationFrame callback:
    wait for that frame (and the next) as well as for the position, or a
    scroll made next is undone when it runs."""
    js(ui, """(() => { window.__frames = 0; scrollOutputToBottom();
      requestAnimationFrame(() => requestAnimationFrame(() => { window.__frames = 1; }));
      return 0; })()""")
    wait_until(lambda: js(ui, "window.__frames"), "scrollOutputToBottom's frame")
    return wait_stable(lambda: js(ui, f"{OUT}.scrollTop"), "#output to settle at the bottom")


def scroll_output_to(ui, top):
    """Set #output's scrollTop as a scroll would; return where it settles
    once the page (and the app's scroll handler) has seen the scroll."""
    n = gestures(ui)["scrolls"]
    js(ui, f"{OUT}.scrollTop = {top}")
    wait_until(lambda: gestures(ui)["scrolls"] > n, "#output's scroll event")
    return wait_stable(lambda: js(ui, f"{OUT}.scrollTop"), "#output to stop scrolling")


@scenario
def pane_does_not_redraw_under_a_held_finger(ui, p0, p1):
    tty = write_pane0("HOLD-ONE first line\r\n")
    try:
        wait_output(ui, "HOLD-ONE")
        x, y = line_point(ui, "HOLD-ONE")
        # Nothing held back from before, so a deferral below is HOLD-TWO's.
        wait_until(lambda: not js(ui, "_deferredForSelection"), "a quiet pane")
        n = len(gestures(ui)["ts"])
        touch(ui, "touchStart", x, y)
        try:
            wait_until(lambda: len(gestures(ui)["ts"]) > n, "the page to see the finger")
            with open(tty, "w") as f:
                f.write("HOLD-TWO arrives mid gesture\r\n")
            # The update arrives and the app holds it back for the finger.
            wait_until(lambda: js(ui, "_deferredForSelection"), "the update to arrive")
            if ui.locator("#output", has_text="HOLD-TWO").count():
                raise AssertionError("pane re-rendered while a finger was down on it")
        finally:
            touch(ui, "touchEnd")
        clear_selection(ui)   # a long press may have selected a word
        wait_output(ui, "HOLD-TWO", timeout=10000)
    finally:
        clear_selection(ui)
        restore_pane0(tty, p0)


@scenario
def scrolled_up_view_stays_put_while_the_pane_prints(ui, p0, p1):
    # The capture is the pane's last 300 lines; past that, each new line
    # drops one off the top, which used to slide a scrolled-up view up a
    # line per update.
    import threading
    tty = write_pane0("".join(f"KEEP-{i:03d} history\r\n" for i in range(400)))
    line_at = """(() => {
      const o = document.getElementById('output'), b = o.getBoundingClientRect();
      let el = document.elementFromPoint(b.left + 40, b.top + b.height / 2);
      while (el && el.parentElement !== o) el = el.parentElement;
      return el ? el.textContent.slice(0, 30) : null; })()"""
    stop = []

    def printer():
        for i in range(40):
            if stop:
                return
            with open(tty, "w") as f:
                f.write(f"NEW-{i:03d} streamed\r\n")
            time.sleep(0.1)                  # the pane's printing pace
    try:
        wait_output(ui, "KEEP-399")
        bottom = to_bottom(ui)
        scroll_output_to(ui, bottom - 600)
        before = js(ui, line_at)
        t = threading.Thread(target=printer)
        t.start()
        seen = []
        while t.is_alive():
            seen.append(js(ui, line_at))
            time.sleep(0.2)                  # sampling pace
        wait_output(ui, "NEW-039", timeout=10000)
        seen.append(wait_stable(lambda: js(ui, line_at), "the view to settle"))
        moved = [x for x in seen if x != before]
        if not before or moved:
            raise AssertionError(f"the line in view went from {before!r} to {moved[:1]!r}")
    finally:
        stop.append(1)
        sh("tmux", "clear-history", "-t", "e2e.0")
        restore_pane0(tty, p0)


@scenario
def selection_survives_cursor_moves(ui, p0, p1):
    # The key bar's ghost caret sits at the pane cursor; moving it splits and
    # re-joins the cursor line's text, which used to collapse a selection.
    popup(ui, "#btn-escape", "#escape-popup-keys")
    ui.wait_for_selector("#keybar", state="visible", timeout=3000)
    text = "SEL-ALPHA bravo charlie delta echo"
    tty = write_pane0(text)
    cursor_at = lambda x: wait_until(lambda: js(ui, "_cursorX") == x,
                                     f"the app to see the cursor at column {x}")
    try:
        wait_output(ui, "delta echo")
        cursor_at(len(text))
        if not select_text(ui, "bravo charlie"):
            raise AssertionError("couldn't find the text to select")
        x = len(text)
        for move, dx in (("\x1b[20D", -20), ("\x1b[3C", 3), ("\x1b[4D", -4), ("\x1b[10C", 10)):
            with open(tty, "w") as f:
                f.write(move)
            x += dx
            cursor_at(x)                     # the app has handled the move
            got = js(ui, SELECTED)
            if got != "bravo charlie":
                raise AssertionError(f"selection became {got!r} after a cursor move")
        clear_selection(ui)
        # Once the selection is gone the ghost catches up with the cursor.
        wait_until(lambda: ui.locator("#compose-ghost").count() == 1,
                   "the ghost caret to come back after the selection cleared")
    finally:
        clear_selection(ui)
        ui.tap("#keybar-close")
        ui.wait_for_selector("#keybar", state="hidden", timeout=3000)
        restore_pane0(tty, p0)


# Counts runs of the app's delayed pane-tap focus, and when the last one ran
# relative to the last touchend, in the page's clock.
FOCUS_HOOK = """(() => {
  const f = window.__focus = {runs: 0, end: null, after: null};
  if (!window.__focusHooked) {
    window.__focusHooked = true;
    const orig = _paneTapFocus;
    _paneTapFocus = function () {
      const r = orig.apply(this, arguments);
      window.__focus.after = performance.now() - window.__focus.end;
      window.__focus.runs++;
      return r;
    };
    addEventListener('touchend', () => { window.__focus.end = performance.now(); },
                     {capture: true, passive: true});
  }
  return 0;
})()"""


@scenario
def pane_tap_waits_out_a_double_tap_before_the_keyboard(ui, p0, p1):
    popup(ui, "#btn-escape", "#escape-popup-keys")
    ui.wait_for_selector("#keybar", state="visible", timeout=3000)
    tty = write_pane0("TAP-LINE some words to select\r\n")
    focused = "document.activeElement && document.activeElement.id"
    focus_ran = lambda: wait_until(lambda: js(ui, "window.__focus.runs"),
                                   "the app's pane-tap focus to run")
    try:
        wait_output(ui, "TAP-LINE")
        x, y = line_point(ui, "TAP-LINE")
        # A lone tap still brings the keyboard up, just not instantly.
        js(ui, "document.activeElement.blur()")
        js(ui, FOCUS_HOOK)
        tap(ui, x, y)
        focus_ran()
        if js(ui, focused) != "cmd":
            raise AssertionError("a single tap on the pane no longer focuses the input")
        after = js(ui, "window.__focus.after")
        if after < 340:
            raise AssertionError(f"keyboard opened {after:.0f}ms after a tap, inside the double-tap window")
        # A double tap that selects a word keeps the keyboard down.  What the
        # second tap does on a phone, done 120ms after this one in the
        # page's own clock: always before the app's 350ms focus timer.
        js(ui, "document.activeElement.blur()")
        js(ui, FOCUS_HOOK)
        select_words = SELECT_JS % json.dumps("words")
        js(ui, f"addEventListener('touchend', () => setTimeout(() => {select_words}, 120),"
               " {capture: true, once: true})")
        tap(ui, x, y)
        focus_ran()
        if js(ui, focused) == "cmd":
            raise AssertionError("keyboard opened over a fresh word selection")
        if js(ui, SELECTED) != "words":
            raise AssertionError(f"selection is {js(ui, SELECTED)!r}, expected 'words'")
    finally:
        clear_selection(ui)
        ui.tap("#keybar-close")
        ui.wait_for_selector("#keybar", state="hidden", timeout=3000)
        restore_pane0(tty, p0)


@scenario
def selection_drag_cannot_scroll_into_blank_rows(ui, p0, p1):
    # tmux sends the pane's blank rows below the cursor; the live view parks
    # the last content line at the bottom with those rows scrolled out of
    # sight. Chrome's drag-to-select autoscroll used to run on into them,
    # taking the selection to the bottom. Set scrollTop as that autoscroll
    # would, during a selection and without one.
    lines = [f"DRAG-{i} line of output" for i in range(1, 6)]
    # Filler scrolls into tmux history so #output has height to scroll;
    # then home + clear-to-end (which keeps history) draws the lines at the
    # top of the screen, with the rest of the 30 rows blank below them.
    filler = "".join(f"filler {i}\r\n" for i in range(60))
    tty = write_pane0(filler + "\x1b[H\x1b[J" + "\r\n".join(lines))
    try:
        wait_output(ui, "DRAG-5")
        # Start from the live view: earlier scenarios can leave #output
        # scrolled up, and then it doesn't follow the bottom.
        parked = to_bottom(ui)
        room = js(ui, f"{OUT}.scrollHeight - {OUT}.clientHeight")
        if room - parked < 100:
            raise AssertionError(f"no hidden blank rows to test with ({parked} of {room})")
        if not select_text(ui, "DRAG-3 line"):
            raise AssertionError("couldn't find the text to select")
        wait_until(lambda: js(ui, "_selScrollPos !== null"), "the app to see the selection")
        got = scroll_output_to(ui, parked + 300)
        if got > parked + 2:
            raise AssertionError(f"selecting, #output scrolled to {got}, past the content at {parked}")
        if js(ui, SELECTED) != "DRAG-3 line":
            raise AssertionError(f"selection changed to {js(ui, SELECTED)!r}")
        # Without a selection, scrolling is the user's to do as they like.
        clear_selection(ui)
        wait_until(lambda: js(ui, "_selScrollPos === null"), "the app to see the selection go")
        if scroll_output_to(ui, parked + 300) <= parked + 2:
            raise AssertionError("scrolling was clamped with no selection")
    finally:
        clear_selection(ui)
        restore_pane0(tty, p0)


# ── background screen lock ────────────────────────────────────────────────
# A real lock needs a WebAuthn credential; the background timeout logic
# doesn't, so these turn the lock on in page state and drive the app's own
# visibilitychange handler, skewing Date.now() for the time spent away.
def lock_choice(ui):
    ui.tap("#btn-settings")
    ui.wait_for_selector("#settings-lock-list .ctx-btn", state="visible", timeout=3000)
    on = [t for t in ui.locator("#settings-lock-list .ctx-btn").all_inner_texts() if t.startswith("✓")]
    ui.tap("#settings-close")
    return on


def choose_lock(ui, label):
    ui.tap("#btn-settings")
    ui.wait_for_selector("#settings-lock-list .ctx-btn", state="visible", timeout=3000)
    ui.locator("#settings-lock-list .ctx-btn", has_text=label).tap()
    ui.tap("#settings-close")


def set_hidden(ui, hidden, skew_ms=0):
    js(ui, f"""(() => {{
        const now = Date.now;
        Object.defineProperty(document, 'hidden', {{value: {str(hidden).lower()}, configurable: true}});
        if ({skew_ms}) Date.now = () => now.call(Date) + {skew_ms};
        try {{ document.dispatchEvent(new Event('visibilitychange')); }}
        finally {{ Date.now = now; delete document.hidden; }}
    }})()""")


def locked(ui):
    return js(ui, "_isLocked") and ui.locator("#lock-overlay").is_visible()


@scenario
def background_lock_follows_the_gear_menu_choice(ui, p0, p1):
    if lock_choice(ui) != ["✓ 30 seconds (default)"]:
        raise AssertionError(f"default choice is {lock_choice(ui)!r}")
    try:
        js(ui, "_lockEnabled = 'true'")
        set_hidden(ui, True); set_hidden(ui, False, skew_ms=20_000)
        if locked(ui):
            raise AssertionError("locked after 20s away with the 30s default")
        set_hidden(ui, True); set_hidden(ui, False, skew_ms=31_000)
        if not locked(ui):
            raise AssertionError("didn't lock after 31s away with the 30s default")
        js(ui, "unlockApp()")

        choose_lock(ui, "1 minute")
        ui.reload()
        wait_output(ui, p0.banner, timeout=20000)
        if lock_choice(ui) != ["✓ 1 minute"]:
            raise AssertionError(f"after reload the choice is {lock_choice(ui)!r}, expected 1 minute")
        js(ui, "_lockEnabled = 'true'")
        set_hidden(ui, True); set_hidden(ui, False, skew_ms=31_000)
        if locked(ui):
            raise AssertionError("locked after 31s away with 1 minute chosen")
        set_hidden(ui, True); set_hidden(ui, False, skew_ms=61_000)
        if not locked(ui):
            raise AssertionError("didn't lock after 61s away with 1 minute chosen")
        js(ui, "unlockApp()")

        choose_lock(ui, "Immediately")
        set_hidden(ui, True)
        if not locked(ui):
            raise AssertionError("Immediately didn't lock on hide")
        # A biometric prompt can hide the page; unlocking while it's hidden
        # must not be undone when the page comes back.
        js(ui, "unlockApp()")
        set_hidden(ui, False)
        if locked(ui):
            raise AssertionError("Immediately relocked on return after an unlock")
    finally:
        js(ui, "localStorage.removeItem('lock-hidden-ms'); unlockApp()")
        ui.reload()
        wait_output(ui, p0.banner, timeout=20000)


def main(filters):
    chosen = [s for s in SCENARIOS if not filters or any(f in s.__name__ for f in filters)]
    if not chosen:
        print(f"no scenario matches {filters}; have: {', '.join(s.__name__ for s in SCENARIOS)}")
        return 2
    # REPEAT=N runs the chosen scenarios N times over, for flaky behavior.
    chosen = chosen * int(os.environ.get("REPEAT", "1"))
    panes, url, pair = start_backend()
    results = []
    retries = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        ui = open_ui(browser, url, panes[0].banner)
        for s in chosen:
            try:
                for attempt in range(1, 4):
                    for pane in panes:
                        pane.mark()
                    try:
                        s(ui, *panes)
                        break
                    except GestureMissed as e:
                        if attempt == 3:
                            raise
                        retries += 1
                        print(f"RETRY {s.__name__}: load stretched a gesture ({e})", flush=True)
                        # Outlast the app's gesture windows (a wall-clock
                        # second is at least one in the page) and start over
                        # from pane 0.
                        time.sleep(1)
                        if not showing(ui, panes[0]):
                            ui.tap("#btn-prev")
                            wait_output(ui, panes[0].banner)
                results.append((s.__name__, None))
                print(f"PASS  {s.__name__}", flush=True)
            except Exception as e:
                results.append((s.__name__, e))
                print(f"FAIL  {s.__name__}: {e}", flush=True)
                if not isinstance(e, AssertionError):
                    traceback.print_exc()
                shot = os.path.join(WORK, f"{s.__name__}.png")
                ui.screenshot(path=shot)
        browser.close()
    pair.kill()
    sh("trustmux", "stop", check=False)
    failed = [n for n, e in results if e]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed"
          + (f": {', '.join(failed)}" if failed else "")
          + (f" ({retries} retried for gesture timing)" if retries else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
