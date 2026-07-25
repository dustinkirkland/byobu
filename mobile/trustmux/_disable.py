"""trustmux disable — stop Trustmux daemon and remove login hook."""
import os
from pathlib import Path

from trustmux._ctl import cmd_stop
from trustmux._enable import is_hook_line
from trustmux._paths import Instance

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
    filtered = [l for l in lines if not is_hook_line(l)]
    if len(filtered) < len(lines):
        dest.write_text("".join(filtered))


def main(inst: Instance | None = None) -> None:
    inst = inst or Instance()
    for f in _LOGIN_FILES:
        _remove_hook(f)

    cmd_stop(inst=inst)

    print()
    print("Trustmux daemon stopped. It will no longer start automatically at login.")
    print()
    print(f"Paired device tokens are preserved in {inst.tokens_file}.")
    print()
    print("To re-enable later, run:")
    print("  trustmux enable")
    print()


if __name__ == "__main__":
    main()
