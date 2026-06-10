"""trustmux disable — stop Trustmux daemon and remove login hook."""
import os
from pathlib import Path

from trustmux._ctl import cmd_stop

_LOGIN_FILES = [
    Path.home() / ".profile",
    Path.home() / ".bash_profile",
    Path.home() / ".bash_login",
    Path.home() / ".zprofile",
]


def _remove_hook(dest: Path) -> None:
    if not dest.exists() or not os.access(dest, os.W_OK):
        return
    lines = dest.read_text().splitlines(keepends=True)
    filtered = [l for l in lines if "trustmux-ctl" not in l and "trustmux start 2>/dev/null" not in l]
    if len(filtered) < len(lines):
        dest.write_text("".join(filtered))


def main() -> None:
    for f in _LOGIN_FILES:
        _remove_hook(f)

    cmd_stop()

    print()
    print("Trustmux daemon stopped. It will no longer start automatically at login.")
    print()
    print("Paired device tokens are preserved in ~/.config/trustmux/tokens.json.")
    print()
    print("To re-enable later, run:")
    print("  trustmux enable")
    print()


if __name__ == "__main__":
    main()
