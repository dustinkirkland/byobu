#!/usr/bin/python3
#
#    agent-state.py - what the coding agent in each tmux pane is doing
#    Copyright (C) 2026 Dustin Kirkland
#
#    Authors: Dustin Kirkland <kirkland@byobu.org>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, version 3 of the License.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""byobu-agent-state: what the coding agent in a tmux pane is doing.

The state lives in the pane's own tmux user options (tmux 3.2+), where the
status line, scripts and Trustmux all read it:

  @agent         what runs there (claude, codex, ...)
  @agent_state   working, blocked (needs you), failed (stopped on an
                 error), done (finished, not yet seen) or idle
  @agent_since   when the state last changed (seconds since the epoch)
  @agent_detail  why, briefly ("approve Bash: make test")

Agents report through hooks (--claude-hook reads Claude Code's) or by
running this with a state. setup(), once per tmux server, gives byobu's
window list one mark per window for the most urgent thing in it, agents'
states and tmux's own alerts alike; a window shown in a client marks its
done and failed panes seen (idle).
"""

import json
import os
import subprocess
import sys
import time

STATES = ("working", "blocked", "failed", "done", "idle")
OPTIONS = ("@agent", "@agent_state", "@agent_since", "@agent_detail")
DETAIL_MAX = 80

# tmux's own default window-status-format (and -current-format), which
# setup() replaces with byobu's; one someone has changed is left alone
TMUX_DEFAULT_FORMAT = "#I:#W#{?window_flags,#{window_flags}, }"

# Each window's mark, most urgent first; each one is an option of its own,
# which setup() sets only where it is unset, so a tmux.conf can pick others
MARKS = (("blocked", "?"),        # an agent needs you (or rang the bell)
         ("bell", "!"),           # something rang the bell
         ("failed", "✗"),         # an agent stopped on an error
         ("done", "◉"),           # an agent finished, not yet seen
         ("activity", "@"),       # new output, not yet seen
         ("silence", "~"),        # no output for monitor-silence seconds
         ("working", "◔"),        # an agent is busy
         ("idle", "-"))           # nothing to report

# The window's panes' states, and whether any pane holds an agent
_STATES = "#{P:#{@agent_state} }"
_AGENT = "#{!=:#{P:#{@agent}},}"


def _has(state):
    return "#{m:*%s*,%s}" % (state, _STATES)


_BLOCKED = "#{||:%s,#{&&:#{window_bell_flag},%s}}" % (_has("blocked"), _AGENT)
_CONDITIONS = (("blocked", _BLOCKED),
               ("bell", "#{window_bell_flag}"),
               ("failed", _has("failed")),
               ("done", _has("done")),
               ("activity", "#{&&:#{window_activity_flag},#{?%s,0,1}}" % _AGENT),
               ("silence", "#{window_silence_flag}"),
               ("working", _has("working")))


def _mark_format():
    out = "#{@byobu_mark_idle}"
    for name, cond in reversed(_CONDITIONS):
        out = "#{?%s,#{@byobu_mark_%s},%s}" % (cond, name, out)
    return out


WINDOW_MARK = _mark_format()
# A window an agent needs takes the bell's style, and one with news from an
# agent the activity style, as tmux styles its own alerts
WINDOW_STYLE = ("#{?%s,#[#{window-status-bell-style}],"
                "#{?#{||:%s,%s},#[#{window-status-activity-style}],}}"
                % (_BLOCKED, _has("failed"), _has("done")))
STATUS_FORMAT = ("#{E:@byobu_window_style}#I:#{?window_zoomed_flag,#[italics],}#W"
                 "#{?window_zoomed_flag,#[noitalics],}#{E:@byobu_window_mark}")

# setup()'s hooks; an index of their own, so they don't replace anyone's
HOOK_INDEX = 86
SEEN_HOOKS = ("session-window-changed", "client-session-changed", "after-select-pane")
SETUP_VERSION = "2"


def tmux(*args):
    """Run a tmux command (";" separates several); its output, or None."""
    try:
        r = subprocess.run(("tmux",) + args, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")


def arg(value):
    """tmux splits commands on an argument ending in ";": escape one."""
    return value[:-1] + "\\;" if value.endswith(";") else value


def supported():
    """Inside tmux 3.2 or later: pane options came in 3.0, but the window
    list's marks nest deeper than 3.0 and 3.1 expand formats."""
    if not os.environ.get("TMUX"):
        return False
    v = (tmux("-V") or "").strip()
    v = v[len("tmux "):] if v.startswith("tmux ") else v
    v = v[len("next-"):] if v.startswith("next-") else v
    major, _, minor = v.partition(".")
    if not (major.isdigit() and minor[:1].isdigit()):
        return True    # openbsd-7.6, master: assume current
    minor = minor[:len(minor) - len(minor.lstrip("0123456789"))]
    return (int(major), int(minor)) >= (3, 2)


def clean(text):
    """One short line: no control characters, at most DETAIL_MAX long."""
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return text if len(text) <= DETAIL_MAX else text[:DETAIL_MAX - 1] + "…"


def pane_options(pane):
    out = tmux("display-message", "-p", "-t", pane,
               "#{@agent}\t#{@agent_state}\t#{window_id}")
    if out is None:
        return None
    return dict(zip(("agent", "state", "window"), out.rstrip("\n").split("\t")))


def shown(window):
    """Is a terminal showing the window?  Control-mode clients (Trustmux's
    daemon, iTerm2) don't count: nobody is necessarily looking."""
    out = tmux("list-clients", "-F", "#{client_control_mode} #{window_id}") or ""
    return ("0 " + window) in out.splitlines()


def set_state(pane, state, detail=None, agent=None):
    cur = pane_options(pane)
    if cur is None:
        return
    if state in SEEN and shown(cur["window"]):
        state = "idle"    # finished in front of someone: seen already
    cmds = []
    if agent and agent != cur["agent"]:
        cmds.append(("set", "-p", "-t", pane, "@agent", arg(agent)))
    if state != cur["state"]:
        cmds.append(("set", "-p", "-t", pane, "@agent_state", state))
        cmds.append(("set", "-p", "-t", pane, "@agent_since", str(int(time.time()))))
    if detail is not None:
        detail = clean(detail)
        cmds.append(("set", "-p", "-t", pane, "@agent_detail", arg(detail)) if detail
                    else ("set", "-pu", "-t", pane, "@agent_detail"))
    if not cur["agent"] and (agent or state):
        if not agent:
            cmds.append(("set", "-p", "-t", pane, "@agent", "agent"))
        cmds += quiet_window(cur["window"])
    if cmds:
        run(cmds)
    if not cur["agent"]:
        setup()


def run(cmds):
    args = []
    for c in cmds:
        if args:
            args.append(";")
        args += c
    tmux(*args)


def quiet_window(window):
    """An agent streaming output keeps its window "active" all the time,
    which drowns the signal: turn activity monitoring off for the window
    while it holds an agent (clear() turns it back)."""
    out = tmux("show-options", "-wv", "-t", window, "@byobu_agent_quiet")
    if out and out.strip():
        return []
    if (tmux("display-message", "-p", "-t", window, "#{monitor-activity}") or "").strip() != "1":
        return []
    return [("set", "-w", "-t", window, "monitor-activity", "off"),
            ("set", "-w", "-t", window, "@byobu_agent_quiet", "1")]


def clear(pane):
    cur = pane_options(pane)
    if cur is None:
        return
    cmds = [("set", "-pu", "-t", pane, o) for o in OPTIONS]
    others = (tmux("list-panes", "-t", cur["window"], "-F", "#{pane_id}\t#{@agent}") or "")
    if not any(a for p, _, a in (l.partition("\t") for l in others.splitlines()) if p != pane):
        if (tmux("show-options", "-wv", "-t", cur["window"], "@byobu_agent_quiet") or "").strip():
            cmds += [("set", "-wu", "-t", cur["window"], "monitor-activity"),
                     ("set", "-wu", "-t", cur["window"], "@byobu_agent_quiet")]
    run(cmds)


SEEN = ("done", "failed")


def seen_window(window):
    """A window shown in a client: its done and failed panes have been seen."""
    out = tmux("list-panes", "-t", window, "-F", "#{pane_id}\t#{@agent_state}") or ""
    now = str(int(time.time()))
    cmds = []
    for line in out.splitlines():
        pane, _, state = line.partition("\t")
        if state in SEEN:
            cmds += [("set", "-p", "-t", pane, "@agent_state", "idle"),
                     ("set", "-p", "-t", pane, "@agent_since", now)]
    if cmds:
        run(cmds)


def agents():
    """Every agent pane in the server: (pane, target, agent, state, since, detail)."""
    out = tmux("list-panes", "-a", "-F",
               "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t"
               "#{@agent}\t#{@agent_state}\t#{@agent_since}\t#{@agent_detail}") or ""
    rows = []
    for line in out.splitlines():
        f = line.split("\t", 5)
        if len(f) == 6 and f[2]:
            rows.append(f)
    return rows


def next_pane():
    """The pane that has needed you longest: blocked first, then failed,
    then done."""
    rank = {"blocked": 0, "failed": 1, "done": 2}
    waiting = [r for r in agents() if r[3] in rank]
    if not waiting:
        tmux("display-message", "No agent needs you")
        return
    r = min(waiting, key=lambda r: (rank[r[3]], int(r[4] or 0)))
    tmux("switch-client", "-t", r[1], ";", "select-window", "-t", r[1],
         ";", "select-pane", "-t", r[0])


def setup():
    """Once per tmux server: byobu's window list (unless the format was
    customized: #{E:@byobu_window_style} and #{E:@byobu_window_mark} bring
    it to your own), and hooks that mark a shown window's done and failed
    panes seen."""
    if (tmux("show-options", "-gqv", "@byobu_agent_setup") or "").strip() == SETUP_VERSION:
        return
    me = os.path.join(os.environ.get("BYOBU_PREFIX", "/usr"), "bin", "byobu-agent-state")
    seen = ("if-shell -F '#{||:%s,%s}' "
            "\"run-shell -b '%s --seen-window #{window_id}'\"" % (_has("done"), _has("failed"), me))
    cmds = [("set", "-g", "@byobu_window_mark", WINDOW_MARK),
            ("set", "-g", "@byobu_window_style", WINDOW_STYLE)]
    have = tmux("show-options", "-g") or ""
    for name, mark in MARKS:
        if not any(l.startswith("@byobu_mark_%s " % name) for l in have.splitlines()):
            cmds.append(("set", "-g", "@byobu_mark_" + name, mark))
    for opt in ("window-status-format", "window-status-current-format"):
        if (tmux("show-options", "-gwv", opt) or "").rstrip("\n") == TMUX_DEFAULT_FORMAT:
            cmds.append(("set", "-gw", opt, STATUS_FORMAT))
    for hook in SEEN_HOOKS:
        cmds.append(("set-hook", "-g", "%s[%d]" % (hook, HOOK_INDEX), seen))
    cmds.append(("set", "-g", "@byobu_agent_setup", SETUP_VERSION))
    run(cmds)


def claude_hook(event):
    """Map a Claude Code hook event (its JSON on stdin) to a state."""
    name = event.get("hook_event_name", "")
    if name == "SessionStart":
        return ("idle", "", "claude")
    if name == "SessionEnd":
        return ("clear", None, None)
    if name in ("UserPromptSubmit", "PreToolUse", "PostToolUse",
                "PostToolUseFailure", "PostToolBatch"):
        return ("working", "" if name == "UserPromptSubmit" else None, "claude")
    if name == "PermissionRequest":
        tool = event.get("tool_name", "")
        inp = event.get("tool_input") or {}
        what = inp.get("command") or inp.get("file_path") or inp.get("url") or ""
        return ("blocked", "approve %s%s" % (tool, ": " + what if what else ""), "claude")
    if name == "Notification":
        kind = event.get("notification_type", "")
        if kind in ("permission_prompt", "agent_needs_input",
                    "elicitation_dialog", "elicitation_url_dialog"):
            return ("blocked", event.get("message") or "needs input", "claude")
        return None    # idle_prompt and the rest: no change
    if name == "Stop":
        return ("done", "", "claude")
    if name == "StopFailure":
        return ("failed", event.get("error") or "API error", "claude")
    return None


# ── Claude Code's hooks ───────────────────────────────────────────────
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
                 "PostToolUse", "PostToolUseFailure", "Notification", "Stop",
                 "StopFailure", "SessionEnd")
MARK = "byobu-agent-state"


def claude_settings_path():
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or
                        os.path.join(os.path.expanduser("~"), ".claude"), "settings.json")


def claude_hooks(enable):
    """Add (or remove) our command to every event in Claude Code's user
    settings, keeping everything else there as it was."""
    path = claude_settings_path()
    try:
        with open(path) as f:
            text = f.read()
        settings = json.loads(text) if text.strip() else {}
    except FileNotFoundError:
        text, settings = None, {}
    except ValueError as e:
        print("%s isn't valid JSON (%s); not changing it" % (path, e), file=sys.stderr)
        return 1
    if not isinstance(settings, dict):
        print("%s isn't a JSON object; not changing it" % path, file=sys.stderr)
        return 1
    hooks = settings.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        print("%s: \"hooks\" isn't an object; not changing it" % path, file=sys.stderr)
        return 1
    # Drop ours first, so enabling twice (or from a moved prefix) leaves one
    for event in list(hooks):
        groups = hooks[event] if isinstance(hooks[event], list) else []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                g["hooks"] = [h for h in g["hooks"]
                              if not (isinstance(h, dict) and MARK in str(h.get("command", "")))]
        groups = [g for g in groups if not (isinstance(g, dict) and g.get("hooks") == [])]
        if groups:
            hooks[event] = groups
        elif isinstance(hooks[event], list):
            del hooks[event]
    if enable:
        me = os.path.join(os.environ.get("BYOBU_PREFIX", "/usr"), "bin", MARK)
        for event in CLAUDE_EVENTS:
            hooks.setdefault(event, []).append(
                {"hooks": [{"type": "command", "command": me + " --claude-hook", "timeout": 5}]})
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    new = json.dumps(settings, indent=2) + "\n"
    if new == text:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if text is not None:
        with open(path + ".byobu-backup", "w") as f:
            f.write(text)
    tmp = path + ".byobu-tmp"
    with open(tmp, "w") as f:
        f.write(new)
    os.replace(tmp, path)
    print("%s Claude Code's hooks in %s%s" % ("Enabled" if enable else "Disabled", path,
          " (the old one is %s.byobu-backup)" % path if text is not None else ""))
    return 0


USAGE = """Usage: byobu-agent-state [--pane %N] [--agent NAME] STATE [DETAIL]
       byobu-agent-state clear
       byobu-agent-state --claude-hook    (Claude Code hook JSON on stdin)
       byobu-agent-state --list | --next | --seen-window @N
       byobu-agent-state --enable-claude-hooks | --disable-claude-hooks

STATE is working, blocked, failed, done or idle."""


def main(argv):
    pane = os.environ.get("TMUX_PANE", "")
    agent = None
    while argv and argv[0] in ("--pane", "--agent") and len(argv) > 1:
        if argv[0] == "--pane":
            pane = argv[1]
        else:
            agent = argv[1]
        argv = argv[2:]
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    cmd = argv[0]
    if cmd == "--claude-hook":
        # A hook must never get in the agent's way: say nothing, exit 0
        try:
            event = json.load(sys.stdin)
        except ValueError:
            return 0
        if not (pane and supported()):
            return 0
        r = claude_hook(event if isinstance(event, dict) else {})
        if r:
            state, detail, agent = r
            clear(pane) if state == "clear" else set_state(pane, state, detail, agent)
        return 0
    if cmd in ("--enable-claude-hooks", "--disable-claude-hooks"):
        return claude_hooks(cmd == "--enable-claude-hooks")
    if not supported():
        return 0
    if cmd == "--list":
        for r in agents():
            print("\t".join(r))
    elif cmd == "--next":
        next_pane()
    elif cmd == "--seen-window" and len(argv) > 1:
        seen_window(argv[1])
    elif cmd == "--setup":
        setup()
    elif cmd == "clear":
        if pane:
            clear(pane)
    elif cmd in STATES:
        if pane:
            set_state(pane, cmd, " ".join(argv[1:]) if len(argv) > 1 else None, agent)
    else:
        print(USAGE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
