"""Browser end-to-end scenarios for the trustmux PWA.

Run through run.sh, never directly on a host: it starts its own tmux server
and trustmux daemon.  Two tmux panes run a raw-mode logger that records every
byte it receives; Chromium (phone-sized, touch) pairs with the daemon and
taps the real UI; each scenario asserts the exact bytes a pane got.

To add a scenario, write a function decorated with @scenario that drives
`ui` and calls expect().  Scenarios run in file order and share one
browser session, so each must leave the UI as it found it: terminal
keyboard mode, key bar closed, pane 0 selected, text box empty.
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
# CSP rules out evaluating JS to read it). Each utterance "takes" 500ms.
TTS_MOCK = r"""
(() => {
  const log = [];
  const write = () => document.documentElement.setAttribute('data-tts-log', JSON.stringify(log));
  let timer = 0;
  class Utterance { constructor(text) { this.text = text; } }
  const synth = {
    speak(u) { log.push({ t: u.text, r: u.rate }); write();
               timer = setTimeout(() => u.onend && u.onend(), 500); },
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
    # The one-time "Lock Trustmux?" prompt shows up right after pairing.
    try:
        page.wait_for_selector("#bio-never-btn", state="visible", timeout=3000)
        page.tap("#bio-never-btn")
    except Exception:
        pass
    return page


def expect(pane, want, timeout=4.0):
    """Wait for pane to have received exactly `want` (and nothing more)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = pane.received()
        if got == want and want:
            break
        if len(got) > len(want):
            break
        time.sleep(0.05)
    time.sleep(0.4)   # anything extra arriving late is a failure too
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
    for key in ("Escape", "Tab", "Left", "BSpace", "Enter"):
        ui.tap(f'#keybar button[data-key="{key}"]')
        time.sleep(0.15)   # keep the order deterministic
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


def wait_seg(ui, cls, want, timeout=6.0):
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


def wait_tmux(target, fmt, want, timeout=4.0):
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
        time.sleep(2.5)                      # one topology poll
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


class Touch:
    """Real touch input through CDP, so Chromium scrolls natively exactly as
    it would under a finger (Playwright's own API has taps, not swipes)."""

    def __init__(self, ui):
        self.ui = ui
        self.cdp = ui.context.new_cdp_session(ui)

    def swipe(self, dx, dy=0, ms=180, steps=8):
        box = self.ui.locator("#output").bounding_box()
        x0 = box["x"] + box["width"] / 2 - dx / 2
        y0 = box["y"] + box["height"] / 2
        send = lambda kind, pts: self.cdp.send(
            "Input.dispatchTouchEvent", {"type": kind, "touchPoints": pts})
        send("touchStart", [{"x": x0, "y": y0}])
        for i in range(1, steps + 1):
            time.sleep(ms / 1000 / steps)
            send("touchMove", [{"x": x0 + dx * i / steps, "y": y0 + dy * i / steps}])
        send("touchEnd", [])
        time.sleep(0.05)

    def double(self, dx, gap=0.15):
        self.swipe(dx)
        time.sleep(gap)
        self.swipe(dx)


def showing(ui, pane):
    return ui.locator("#output", has_text=pane.banner).count() == 1


def assert_showing(ui, pane, still=1.0):
    time.sleep(still)
    if not showing(ui, pane):
        raise AssertionError(f"expected to still be on pane {pane.index}")


def set_wrap(ui, on):
    label = ui.locator("#escape-popup-wrap").inner_text()
    if label.startswith("✓") != on:
        popup(ui, "#btn-escape", "#escape-popup-wrap")
        time.sleep(0.3)


@scenario
def double_swipe_navigates_like_the_arrows(ui, p0, p1):
    touch = Touch(ui)
    set_wrap(ui, True)
    try:
        touch.double(-150)                   # swipe left twice: next
        wait_output(ui, p1.banner)
        touch.double(150)                    # swipe right twice: previous
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
        touch.swipe(-150)                    # one swipe only
        assert_showing(ui, p0)
        touch.swipe(-150, ms=900)            # slow drag, twice
        touch.swipe(-150, ms=900)
        assert_showing(ui, p0)
        touch.swipe(-60, dy=200)             # mostly vertical, twice
        touch.swipe(-60, dy=200)
        assert_showing(ui, p0)
        touch.swipe(-150)                    # left then right: no pair
        time.sleep(0.15)
        touch.swipe(150)
        assert_showing(ui, p0)
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
    ui.locator("#output", has_text="W" * 95).wait_for(timeout=5000)
    time.sleep(1.5)
    touch.double(-150)                       # scrolls the long line left
    assert_showing(ui, p0)
    for _ in range(6):                       # slow drags (never a swipe)
        touch.swipe(-300, ms=900)            # to reach the right edge
    time.sleep(1.0)
    touch.double(-150)                       # at the edge: navigates
    wait_output(ui, p1.banner)
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


def tts_spoken_since(ui, mark, settle=0.0, timeout=8.0):
    """Utterances logged after index `mark`, once reading has finished."""
    time.sleep(settle)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ui.locator("#tts-stop").is_hidden():
            break
        time.sleep(0.1)
    return tts_log(ui)[mark:]


def triple_tap(ui, text):
    # The latest copy: earlier scenarios' output can still be in scrollback.
    # Reading scrolls #output to follow the highlight, so a later sample can
    # sit below the fold; bring it into view like a user would.
    line = ui.locator("#output > span", has_text=text).last
    line.scroll_into_view_if_needed()
    time.sleep(0.2)
    box = line.bounding_box()
    touch = Touch(ui)
    # #output may be scrolled sideways (an earlier Terminal-mode scenario
    # leaves it at the right edge), so tap inside the visible part of it.
    out = ui.locator("#output").bounding_box()
    x = max(box["x"], out["x"]) + 20
    y = box["y"] + box["height"] / 2
    for _ in range(3):
        touch.cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        time.sleep(0.04)
        touch.cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        time.sleep(0.12)


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
        ui.locator("#output", has_text="continues here.").wait_for(timeout=5000)
        mark = len(tts_log(ui))
        triple_tap(ui, "Second line")
        if tts_spoken_since(ui, mark, settle=0.8):
            raise AssertionError("read aloud spoke while disabled")
    finally:
        restore_pane0(tty, p0)


@scenario
def read_aloud_reads_the_tapped_block(ui, p0, p1):
    tty = write_sample(p0)
    set_tts(ui, True)
    try:
        ui.locator("#output", has_text="continues here.").wait_for(timeout=5000)
        cases = [
            ("Second line", ["I updated the parser.", "It now handles tabs.",
                             "Second line of the same sentence continues here."]),
            ("nothing to commit", ["Bash: git status"]),
            ("hi there", ["echo hi", "hi there friend.", "How are you?"]),
        ]
        for tap_on, want in cases:
            mark = len(tts_log(ui))
            triple_tap(ui, tap_on)
            got = tts_spoken_since(ui, mark, settle=0.3)
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
        ui.locator("#output", has_text="Four. Five.").wait_for(timeout=5000)
        mark = len(tts_log(ui))
        triple_tap(ui, "Four. Five.")
        ui.wait_for_selector("#tts-stop", state="visible", timeout=2000)
        if ui.locator("#output .tts-reading", has_text="Four. Five.").count() < 1:
            raise AssertionError("the line being read isn't highlighted")
        time.sleep(0.6)                      # into the second sentence
        triple_tap(ui, "Four. Five.")        # stop
        ui.wait_for_selector("#tts-stop", state="hidden", timeout=2000)
        got = tts_log(ui)[mark:]
        spoken = [g for g in got if g != "CANCEL"]
        if not (1 <= len(spoken) < 6) or got[-1] != "CANCEL":
            raise AssertionError(f"triple tap didn't stop reading: {got!r}")
        time.sleep(1.2)
        if len(tts_log(ui)) != mark + len(got):
            raise AssertionError("kept speaking after stop")
        if ui.locator(".tts-block, .tts-reading").count():
            raise AssertionError("highlight left behind after stop")
        # The Stop pill stops it too.
        triple_tap(ui, "Four. Five.")
        ui.wait_for_selector("#tts-stop", state="visible", timeout=2000)
        ui.tap("#tts-stop")
        ui.wait_for_selector("#tts-stop", state="hidden", timeout=2000)
    finally:
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
            ui.locator("#output", has_text="hi there").wait_for(timeout=5000)
            mark = len(tts_log(ui))
            triple_tap(ui, "hi there")
            got = [g for g in tts_spoken_since(ui, mark, settle=0.3) if g != "CANCEL"]
            if not got or any(g["r"] != 1.4 for g in got):
                raise AssertionError(f"expected rate 1.4, got {got!r}")
        finally:
            restore_pane0(tty, p0)
    finally:
        set_tts(ui, False, rate_steps=-2)


def main(filters):
    chosen = [s for s in SCENARIOS if not filters or any(f in s.__name__ for f in filters)]
    if not chosen:
        print(f"no scenario matches {filters}; have: {', '.join(s.__name__ for s in SCENARIOS)}")
        return 2
    panes, url, pair = start_backend()
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        ui = open_ui(browser, url, panes[0].banner)
        for s in chosen:
            for pane in panes:
                pane.mark()
            try:
                s(ui, *panes)
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
          + (f": {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
