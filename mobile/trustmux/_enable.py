"""trustmux enable — start Trustmux daemon and enable it at login."""
import os
import sys
from pathlib import Path

from trustmux._ctl import (DEFAULT_PORT, can_use_serve, cmd_setup, cmd_start,
                           port_opt)
from trustmux._paths import Instance

_HOOK = "trustmux start 2>/dev/null || true\n"

_LOGIN_FILES = [
    Path.home() / ".profile",
    Path.home() / ".bash_profile",
    Path.home() / ".bash_login",
]
if "zsh" in os.environ.get("SHELL", ""):
    _LOGIN_FILES.append(Path.home() / ".zprofile")


def hook_line(port: int = DEFAULT_PORT, inst: Instance | None = None) -> str:
    """Login hook to install. Only carries --name/--port when they are not
    the defaults, so profiles written by older versions stay byte-identical."""
    inst = inst or Instance()
    return f"trustmux start{inst.label()}{port_opt(port)} 2>/dev/null || true\n"


def is_hook_line(line: str) -> bool:
    """True for a trustmux login hook in any version's format."""
    return "trustmux-ctl" in line or ("trustmux start" in line and "2>/dev/null" in line)


# The flag that names an instance was called --instance before it was --name.
# Hooks live in ~/.profile and are not rewritten on upgrade, so a hook written
# by the older spelling has to stay recognisable or `disable`/`rm` would leave
# it behind to resurrect an instance that no longer exists.
_NAME_FLAGS = ("--name", "--instance")


def is_hook_for(line: str, inst: Instance | None = None) -> bool:
    """True for a hook line belonging to this instance.

    Each instance owns one line, so enabling a second instance appends rather
    than overwriting the first.
    """
    inst = inst or Instance()
    if not is_hook_line(line):
        return False
    if any(f"{flag} {inst.name}" in line for flag in _NAME_FLAGS):
        return True
    # A hook that names no instance is the default instance's.
    return (inst.name == Instance().name
            and not any(flag in line for flag in _NAME_FLAGS))


def _install_hook(dest: Path, hook: str = _HOOK, inst: Instance | None = None) -> None:
    if not dest.exists():
        return
    inst = inst or Instance()
    lines = dest.read_text().splitlines(keepends=True)
    if any(is_hook_for(l, inst) for l in lines):
        # Rewrite in place so `enable --port N` updates this instance's hook
        # instead of silently keeping the old port.
        updated = [hook if is_hook_for(l, inst) else l for l in lines]
        if updated != lines:
            dest.write_text("".join(updated))
        return
    with dest.open("a") as f:
        f.write(f"\n{hook}")


def main(port: int = DEFAULT_PORT, inst: Instance | None = None) -> None:
    inst = inst or Instance()
    # enable == setup + start in serve mode + login hook, so it inherits the
    # serve restriction. Check up front: otherwise cmd_setup refuses and the
    # generic "re-run trustmux enable" hint below would just loop.
    if not can_use_serve(inst):
        print("", file=sys.stderr)
        print("  To start it at login anyway, add one of the above to your shell",
              file=sys.stderr)
        print("  profile yourself — `enable` only manages serve-mode instances.",
              file=sys.stderr)
        sys.exit(1)

    if cmd_setup(quiet=True, port=port, inst=inst) != 0:
        print("\nFirst-time setup did not complete. Fix the issue above, then re-run:")
        print(f"  trustmux enable{inst.label()}{port_opt(port)}")
        sys.exit(1)

    hook = hook_line(port, inst)
    for f in _LOGIN_FILES:
        _install_hook(f, hook, inst)

    started = cmd_start("serve", port, inst) == 0

    print()
    if started:
        print("Trustmux daemon started and will launch automatically at each login.")
    else:
        print("Trustmux daemon is already running and will launch automatically at each login.")
    print()

    tokens = inst.tokens_file
    if not tokens.exists() or tokens.stat().st_size == 0:
        print("Next step — pair your phone:")
        print(f"  trustmux pair{inst.label()}")
        print()
        print("Open the URL printed above in your phone's browser, enter the code, and tap Pair.")
        print()

    print("To stop and disable later, run:")
    print(f"  trustmux disable{inst.label()}")
    print()


if __name__ == "__main__":
    main()
