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
import os
import subprocess
import sys
import time
import traceback

from playwright.sync_api import sync_playwright

WORK = os.path.expanduser("~/e2e")
LOGGER = os.path.join(WORK, "logger.py")
PORT = "8443"

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
