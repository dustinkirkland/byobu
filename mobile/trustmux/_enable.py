"""trustmux enable — start Trustmux daemon and enable it at login."""
import os
import sys
from pathlib import Path

from trustmux._ctl import DEFAULT_PORT, cmd_setup, cmd_start, port_opt
from trustmux._paths import Instance

_HOOK = "trustmux start 2>/dev/null || true\n"

_LOGIN_FILES = [
    Path.home() / ".profile",
    Path.home() / ".bash_profile",
    Path.home() / ".bash_login",
]
if "zsh" in os.environ.get("SHELL", ""):
    _LOGIN_FILES.append(Path.home() / ".zprofile")


def hook_line(port: int = DEFAULT_PORT) -> str:
    """Login hook to install. Only carries --port when it is not the default,
    so profiles written by older versions stay byte-identical."""
    return f"trustmux start{port_opt(port)} 2>/dev/null || true\n"


def is_hook_line(line: str) -> bool:
    """True for a trustmux login hook in any version's format."""
    return "trustmux-ctl" in line or ("trustmux start" in line and "2>/dev/null" in line)



def _install_hook(dest: Path, hook: str = _HOOK) -> None:
    if not dest.exists():
        return
    lines = dest.read_text().splitlines(keepends=True)
    if any(is_hook_line(l) for l in lines):
        # Rewrite in place so `enable --port N` updates a hook installed
        # earlier with a different port instead of silently keeping the old one.
        updated = [hook if is_hook_line(l) else l for l in lines]
        if updated != lines:
            dest.write_text("".join(updated))
        return
    with dest.open("a") as f:
        f.write(f"\n{hook}")


def main(port: int = DEFAULT_PORT, inst: Instance | None = None) -> None:
    inst = inst or Instance()
    if cmd_setup(quiet=True, port=port, inst=inst) != 0:
        print("\nFirst-time setup did not complete. Fix the issue above, then re-run:")
        print(f"  trustmux enable{port_opt(port)}")
        sys.exit(1)

    hook = hook_line(port)
    for f in _LOGIN_FILES:
        _install_hook(f, hook)

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
        print("  trustmux pair")
        print()
        print("Open the URL printed above in your phone's browser, enter the code, and tap Pair.")
        print()

    print("To stop and disable later, run:")
    print("  trustmux disable")
    print()


if __name__ == "__main__":
    main()
