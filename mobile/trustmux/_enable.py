"""trustmux enable — start Trustmux daemon and enable it at login."""
import os
import sys
from pathlib import Path

from trustmux._ctl import TOKENS_FILE, cmd_setup, cmd_start

_HOOK = "trustmux start 2>/dev/null || true\n"

_LOGIN_FILES = [
    Path.home() / ".profile",
    Path.home() / ".bash_profile",
    Path.home() / ".bash_login",
]
if "zsh" in os.environ.get("SHELL", ""):
    _LOGIN_FILES.append(Path.home() / ".zprofile")


def _install_hook(dest: Path) -> None:
    if not dest.exists():
        return
    text = dest.read_text()
    if _HOOK in text or "trustmux-ctl" in text:
        return
    with dest.open("a") as f:
        f.write(f"\n{_HOOK}")


def main() -> None:
    if cmd_setup(quiet=True) != 0:
        print("\nFirst-time setup did not complete. Fix the issue above, then re-run:")
        print("  trustmux enable")
        sys.exit(1)

    for f in _LOGIN_FILES:
        _install_hook(f)

    started = cmd_start("serve") == 0

    print()
    if started:
        print("Trustmux daemon started and will launch automatically at each login.")
    else:
        print("Trustmux daemon is already running and will launch automatically at each login.")
    print()

    if not TOKENS_FILE.exists() or TOKENS_FILE.stat().st_size == 0:
        print("Next step — pair your phone:")
        print("  trustmux pair")
        print()
        print("Open the URL printed above in your phone's browser, enter the code, and tap Pair.")
        print()

    print("To stop and disable later, run:")
    print("  trustmux disable")
    print()


if __name__ == "__main__":
    main()
