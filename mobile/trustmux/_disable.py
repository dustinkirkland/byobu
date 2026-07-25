"""trustmux disable — stop Trustmux daemon and remove login hook."""
import os
from pathlib import Path

from trustmux._ctl import cmd_stop
from trustmux._enable import is_hook_for
from trustmux._paths import Instance

_LOGIN_FILES = [
    Path.home() / ".profile",
    Path.home() / ".bash_profile",
    Path.home() / ".bash_login",
    Path.home() / ".zprofile",
]


def _remove_hook(dest: Path, inst: Instance | None = None) -> None:
    if not dest.exists() or not os.access(dest, os.W_OK):
        return
    inst = inst or Instance()
    lines = dest.read_text().splitlines(keepends=True)
    # Only this instance's hook: disabling one must not un-enable the others.
    filtered = [l for l in lines if not is_hook_for(l, inst)]
    if len(filtered) < len(lines):
        dest.write_text("".join(filtered))


def main(inst: Instance | None = None) -> None:
    inst = inst or Instance()
    for f in _LOGIN_FILES:
        _remove_hook(f, inst)

    cmd_stop(inst=inst)

    print()
    print("Trustmux daemon stopped. It will no longer start automatically at login.")
    print()
    print(f"Paired device tokens are preserved in {inst.tokens_file}.")
    print()
    print("To re-enable later, run:")
    print(f"  trustmux enable{inst.label()}")
    print()


if __name__ == "__main__":
    main()
