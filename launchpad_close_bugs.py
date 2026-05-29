#!/usr/bin/env python3
"""
Close Launchpad byobu bugs that have been fixed, with polite messages.
Run this interactively — it will print a browser URL for OAuth authorization.
"""

from launchpadlib.launchpad import Launchpad
from launchpadlib.credentials import RequestTokenAuthorizationEngine, UnencryptedFileCredentialStore
import os
import sys

# ── bugs to close ─────────────────────────────────────────────────────────────
# Each entry: (bug_number, new_status, comment_text)
# new_status must be one of the valid Launchpad status strings.
FIXES = [
    (
        1837812,
        "Fix Released",
        "Hi — thanks for reporting this. Fixed in byobu commit 0106e742 "
        "(May 2026, byobu 7.1): all byobu.org source-tree references "
        "(README, man pages, appdata, configure.ac) have been updated from "
        "http:// to https://. The website itself responds correctly at "
        "https://byobu.org. Closing as Fix Released.",
    ),
    (
        1842990,
        "Fix Released",
        "Hi — thanks for the report. The byobu.org website is now accessible "
        "at https://byobu.org (HTTP is redirected to HTTPS). Additionally, all "
        "source-tree references were updated to https:// in commit 0106e742 "
        "(May 2026, byobu 7.1). Closing as Fix Released.",
    ),
    (
        1821880,
        "Fix Released",
        "Hi — thanks for the nudge. The /usr/bin/python3 issue was resolved "
        "in byobu 5.128 and subsequent releases. Byobu has since been released "
        "through the 5.x, 6.x, and now 7.x series (current: 7.1). The "
        "/usr/bin/python3 path is no longer referenced in the codebase. "
        "Closing as Fix Released.",
    ),
    (
        1626218,
        "Fix Released",
        "Hi — thanks for the detailed report. Fixed in byobu commit 2ba3b295 "
        "(February 2026), included in byobu 7.1. The Linux path in "
        "usr/lib/byobu/disk_io was writing the cache file without a trailing "
        "newline (printf '%s'), which caused POSIX `read` to always fail and "
        "reset x1=0, displaying cumulative totals instead of instantaneous "
        "rates. The fix changes the write to printf '%s\\n' so `read` "
        "correctly picks up the cached value on the next invocation, giving "
        "accurate instantaneous I/O rates. Closing as Fix Released.",
    ),
    (
        1560723,
        "Fix Released",
        "Hi — thanks for the report. This was resolved in byobu 5.105 (2016) "
        "when compatibility with tmux 2.1 was restored. Andreas Ntaflos "
        "confirmed at the time: 'Byobu 5.105 works well with tmux 2.1 on both "
        "precise and trusty.' The bug has been marked 'In Progress' since then "
        "but the underlying incompatibility was fixed nearly a decade ago and "
        "modern tmux + byobu 7.x works fine. Closing as Fix Released.",
    ),
    (
        1739708,
        "Fix Released",
        "Hi — thanks for the suggestion. Fixed in 2018 by adding "
        "'TerminalEmulator' to the Categories field in byobu.desktop. The "
        "current file has: Categories=GNOME;GTK;System;Utility;TerminalEmulator; "
        "which ensures byobu appears in Cinnamon's preferred applications list. "
        "This has been shipped in all byobu releases since ~5.120 and is "
        "present in byobu 7.0/7.1. Closing as Fix Released.",
    ),
    (
        1973362,
        "Fix Released",
        "Hi — thanks for the detailed proposal. The XDG / BYOBU_CONFIG_DIR "
        "handling was improved in a commit from late 2023 (confirmed by "
        "MestreLion's comment). The fix is present in byobu 7.0 and later. "
        "Closing as Fix Released.",
    ),
    (
        1618185,
        "Fix Released",
        "Hi — thanks for the report. Fixed in 2018: the man page was corrected "
        "to document the proper approach for customising the logo, which is to "
        "set LOGO=your-text in $BYOBU_CONFIG_DIR/statusrc (rather than using "
        "an external $BYOBU_CONFIG_DIR/logo file, which was a documentation "
        "error). This fix has been present in all byobu releases since ~5.120 "
        "through the current 7.1. Closing as Fix Released.",
    ),
    (
        1696546,
        "Fix Released",
        "Hi — thanks for the patch. Fixed in 2018. The "
        "usr/lib/byobu/include/toggle-utf8 script now determines the shell's "
        "rc file dynamically: RC_FILE=$(echo \"$SHELL\" | sed 's:.*/::'), so "
        "for zsh it sources ~/.zshrc, for bash ~/.bashrc, etc. This has been "
        "in byobu since ~5.120 and is present in the current 7.1 release. "
        "Closing as Fix Released.",
    ),
    (
        1813091,
        "Fix Released",
        "Hi — thanks for the patch. Fixed in byobu 7.1 on two fronts:\n\n"
        "1. The byobu.desktop Exec line no longer uses the deprecated -e flag. "
        "It now reads: gnome-terminal --app-id us.kirkland.terminals.byobu "
        "--class=us.kirkland.terminals.byobu -- byobu\n\n"
        "2. The postinst script now correctly detects the current "
        "gnome-terminal server binary path (/usr/libexec/gnome-terminal-server, "
        "which changed from /usr/lib/gnome-terminal/gnome-terminal-server in "
        "gnome-terminal 3.34+), ensuring the correct desktop file with the "
        "--app-id hint is installed on modern systems.\n\n"
        "Closing as Fix Released.",
    ),
    (
        1869479,
        "Fix Released",
        "Hi — thanks for the report. The byobu.org website has been "
        "reorganised. The /support page (https://www.byobu.org/support) "
        "now correctly shows support resources (bug tracker, StackExchange), "
        "and all navigation links are accurate. Closing as Fix Released.",
    ),
]

# ── auth ───────────────────────────────────────────────────────────────────────

class ManualAuthEngine(RequestTokenAuthorizationEngine):
    def make_end_user_authorize_token(self, credentials, request_token):
        auth_url = self.authorization_url(request_token)
        print("\n" + "=" * 70)
        print("LAUNCHPAD AUTHORIZATION REQUIRED")
        print("=" * 70)
        print("\nPlease visit this URL in your browser:\n")
        print(f"    {auth_url}\n")
        print("After clicking 'Allow', press Enter here to continue.")
        print("=" * 70 + "\n")
        input("Press Enter after authorizing...")


def get_launchpad():
    cred_dir = os.path.expanduser("~/.cache/byobu-launchpad")
    cred_file = os.path.join(cred_dir, "credentials")

    if not os.path.exists(cred_file):
        print("No credentials found. Run these steps first:")
        print("  ! python3 /home/kirkland/src/byobu/launchpad_auth_phase1.py")
        print("  (visit the URL, click Allow)")
        print("  ! python3 /home/kirkland/src/byobu/launchpad_auth_phase2.py")
        raise SystemExit(1)

    print(f"Using credentials from {cred_file}")
    lp = Launchpad.login_with(
        "byobu-cleanup-tool",
        "production",
        credential_store=UnencryptedFileCredentialStore(cred_file),
        version="devel",
    )

    me = lp.me
    print(f"Authenticated as: {me.display_name} ({me.name})\n")
    return lp


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("DRY RUN — no changes will be made to Launchpad\n")
    else:
        print("LIVE RUN — this will post comments and change bug statuses\n")

    lp = get_launchpad()

    for bug_num, new_status, comment in FIXES:
        print(f"Bug #{bug_num}:")
        try:
            bug = lp.bugs[bug_num]
            print(f"  Title:      {bug.title}")
            # Find the byobu task
            task = None
            for t in bug.bug_tasks:
                if "byobu" in str(t.target).lower():
                    task = t
                    break
            if task is None:
                task = bug.bug_tasks[0]
            print(f"  Task:       {task.target.display_name}")
            print(f"  Status now: {task.status}")
            print(f"  New status: {new_status}")
            if dry_run:
                print("  [DRY RUN] would post comment and set status")
            else:
                bug.newMessage(content=comment)
                task.status = new_status
                task.lp_save()
                print("  Posted comment and updated status.")
        except Exception as e:
            print(f"  ERROR: {e}")
        print()


if __name__ == "__main__":
    main()
