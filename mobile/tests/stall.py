"""Stall tmux, or the whole phone, now and then for a second or two.

A release build runs about ten phases at once, and on that machine
everything answers seconds late. A CPU limit alone rarely does that, so
races with it showed up one per release candidate. Two kinds of stall:

    TMUX_STALL=1     the tmux server stops: captures and subscribe replies
                     come seconds late (tests/e2e and tests/android)
    PHONE_STALL=1    the Android emulator stops: the daemon's updates pile
                     up and the page gets them all at once, and the pane
                     can jump just as a finger lands (tests/android)

=1 means stalls of 0.5-3s; =MIN-MAX sets the range (e.g. TMUX_STALL=0.5-6
goes past the daemon's 5s capture timeout, so its retry runs too). Each run
prints its seed; TMUX_STALL_SEED / PHONE_STALL_SEED repeat a schedule.

Scenarios that stop tmux themselves run inside paused().
"""

import contextlib
import os
import random
import signal
import sys
import threading

# Seconds between stalls. The phone gets them closer together: a pane
# jumping under a landing finger needs one to end in a narrow window.
GAPS = {"TMUX": (1.0, 5.0), "PHONE": (0.3, 2.0)}
STALLER = None


class Staller:
    def __init__(self, kind, pid, spec):
        lo, _, hi = (spec if spec != "1" else "0.5-3").partition("-")
        self.kind, self.pid = kind, pid
        self.lo, self.hi = float(lo), float(hi or lo)
        self.gap = GAPS[kind]
        self.seed = int(os.environ.get(f"{kind}_STALL_SEED") or random.randrange(1 << 32))
        self.rng = random.Random(self.seed)
        self.lock = threading.Lock()     # held during a stall, and by paused()
        self.done = threading.Event()
        self.count, self.total = 0, 0.0
        self.thread = threading.Thread(target=self.run, daemon=True)
        print(f"{kind}_STALL: stops for {self.lo:g}-{self.hi:g}s every "
              f"{self.gap[0]:g}-{self.gap[1]:g}s ({kind}_STALL_SEED={self.seed})", flush=True)

    def run(self):
        while not self.done.wait(self.rng.uniform(*self.gap)):
            secs = self.rng.uniform(self.lo, self.hi)
            with self.lock:
                if self.done.is_set():
                    break
                os.kill(self.pid, signal.SIGSTOP)
                try:
                    self.done.wait(secs)
                finally:
                    os.kill(self.pid, signal.SIGCONT)
            self.count += 1
            self.total += secs

    def summary(self):
        return f"{self.kind}_STALL: {self.count} stalls, {self.total:.0f}s in all"


def start(pid):
    """Start stalling tmux server `pid` if TMUX_STALL is set."""
    global STALLER
    spec = os.environ.get("TMUX_STALL", "")
    if spec in ("", "0"):
        return
    STALLER = Staller("TMUX", pid, spec)
    STALLER.thread.start()


def stop():
    if STALLER:
        STALLER.done.set()
        STALLER.thread.join()
        print(STALLER.summary(), flush=True)


@contextlib.contextmanager
def paused():
    """No tmux stalls in here (waits out one under way)."""
    if not STALLER:
        yield
        return
    with STALLER.lock:
        yield


# PHONE_STALL: the emulator runs as root and the tests don't, so
# in_container.sh runs this as root: `stall.py PHONE PID SPEC GOFILE`.
# Stalls run while GOFILE exists (the tests create it once calibrated and
# remove it at the end), and the summary goes to GOFILE.done.
if __name__ == "__main__":
    import time
    kind, pid, spec, go = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
    while not os.path.exists(go):
        time.sleep(0.2)
    s = Staller(kind, pid, spec)
    s.thread.start()
    while os.path.exists(go):
        time.sleep(0.2)
    s.done.set()
    s.thread.join()
    with open(go + ".done", "w") as f:
        f.write(s.summary() + "\n")
