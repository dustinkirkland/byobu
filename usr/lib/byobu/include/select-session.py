#!/usr/bin/env python3
#
#    select-session.py
#    Copyright (C) 2010 Canonical Ltd.
#    Copyright (C) 2012-2014 Dustin Kirkland <kirkland@byobu.org>
#
#    Authors: Dustin Kirkland <kirkland@byobu.org>
#             Ryan C. Thompson <rct@thompsonclan.org>
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


import os
import re
import sys
import subprocess
try:
	# For Python3, try and import input from builtins
	from builtins import input
except Exception:
	# But fall back to using the default input
	True


PKG = "byobu"
SHELL = os.getenv("SHELL", "/bin/bash")
HOME = os.getenv("HOME")
BYOBU_CONFIG_DIR = os.getenv("BYOBU_CONFIG_DIR", HOME + "/.byobu")
BYOBU_BACKEND = os.getenv("BYOBU_BACKEND", "tmux")
BYOBU_TERM = os.getenv("BYOBU_TERM")
choice = -1
sessions = []
text = []
reuse_sessions = os.path.exists("%s/.reuse-session" % (BYOBU_CONFIG_DIR))

# LANG/LANGUAGE: byobu.in normalizes these (falling back to
# /etc/default/locale) before it ever execs in here, including on a
# reattach -- but that corrected value only reaches an *existing* session's
# own captured environment (what new windows/splits there inherit) via this
# same update_environment() push, same as every other var below. Without
# it, a session whose server-start environment had a bad LANG (e.g. created
# as an SSH forced command, GH #120) stays stuck with it forever, even once
# reattached from a client whose LANG is already correct.
BYOBU_UPDATE_ENVVARS = ["DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "SESSION_MANAGER", "GPG_AGENT_INFO", "XDG_SESSION_COOKIE", "XDG_SESSION_PATH", "GNOME_KEYRING_CONTROL", "GNOME_KEYRING_PID", "GPG_AGENT_INFO", "SSH_ASKPASS", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "WINDOWID", "UPSTART_JOB", "UPSTART_EVENTS", "UPSTART_SESSION", "UPSTART_INSTANCE", "LANG", "LANGUAGE"]


def get_sessions():
	sessions = []
	i = 0
	output = False
	if BYOBU_BACKEND == "screen":
		try:
			output = subprocess.Popen(["screen", "-ls"], stdout=subprocess.PIPE).communicate()[0]
		except subprocess.CalledProcessError as cpe:
			# screen -ls seems to always return 1
			if cpe.returncode != 1:
				raise
			else:
				output = cpe.output
		if sys.stdout.encoding is None:
			output = output.decode("UTF-8")
		else:
			output = output.decode(sys.stdout.encoding)
		if output:
			for s in output.splitlines():
				s = re.sub(r'\s+', ' ', s)
				# Ignore hidden sessions (named sessions that start with a "." or a "_")
				if s and s != " " and (s.find(" ") == 0 and len(s) > 1 and s.count("..") == 0 and s.count("._") == 0):
					text.append("screen: %s" % s.strip())
					items = s.split(" ")
					sessions.append("screen____%s" % items[1])
					i += 1
	if BYOBU_BACKEND == "tmux":
		output = subprocess.Popen(["tmux", "list-sessions"], stdout=subprocess.PIPE).communicate()[0]
		if sys.stdout.encoding is None:
			output = output.decode("UTF-8")
		else:
			output = output.decode(sys.stdout.encoding)
		if output:
			seen_groups = set()
			for s in output.splitlines():
				# Ignore hidden sessions (named sessions that start with a "_")
				if s and not s.startswith("_"):
					# Collapse tmux session groups (e.g. "1", "1-1", "1-2") into a single
					# menu entry; attaching any member attaches the shared windows.
					group = re.search(r"\(group (\S+)\)", s)
					if group:
						if group.group(1) in seen_groups:
							continue
						seen_groups.add(group.group(1))
					text.append("tmux: %s" % s.strip())
					sessions.append("tmux____%s" % s.split(":")[0])
					i += 1
	return sessions


def cull_zombies(session_name):
	# When using tmux session groups, closing a client will leave
	# unattached "zombie" sessions that will never be reattached.
	# Search for and kill any unattached hidden sessions in the same group
	if BYOBU_BACKEND == "tmux":
		output = subprocess.Popen(["tmux", "list-sessions"], stdout=subprocess.PIPE).communicate()[0]
		if sys.stdout.encoding is None:
			output = output.decode("UTF-8")
		else:
			output = output.decode(sys.stdout.encoding)
		if not output:
			return

		# Find the master session to extract the group name. We use
		# the group number to be extra sure the right session is getting
		# killed. We don't want to accidentally kill the wrong one
		pattern = "^%s:.+\\((group [^\\)]+)\\).*$" % session_name
		master = re.search(pattern, output, re.MULTILINE)
		if not master:
			return

		# Kill all the matching hidden & unattached sessions
		pattern = "^_%s-\\d+:.+\\(%s\\)$" % (session_name, master.group(1))
		for s in re.findall(pattern, output, re.MULTILINE):
			subprocess.Popen(["tmux", "kill-session", "-t", s.split(":")[0]])


def update_environment(session):
	backend, session_name = session.split("____", 2)
	for var in BYOBU_UPDATE_ENVVARS:
		value = os.getenv(var)
		if value:
			if backend == "tmux":
				cmd = ["tmux", "setenv", "-t", session_name, var, value]
			else:
				cmd = ["screen", "-S", session_name, "-X", "setenv", var, value]
			subprocess.call(cmd, stdout=open(os.devnull, "w"))


def attach_session(session):
	update_environment(session)
	backend, session_name = session.split("____", 2)
	cull_zombies(session_name)
	# must use the binary, not the wrapper!
	if backend == "tmux":
		# default-terminal is a server-global tmux option, set only once,
		# by whichever client happened to create this server's first
		# session (see profiles/tmux's "set -g default-terminal
		# $BYOBU_TERM", sourced only on that first new-session). Every
		# later attach lands here instead and never touched it, so a
		# session created from a client with poor terminfo (no
		# tmux-256color, say) stayed stuck with that choice forever,
		# even once attached from a client that has it -- GH #96's
		# italic-renders-as-reverse bug, just one creation away from the
		# original fix. BYOBU_TERM is byobu.in's freshly-computed
		# preference for *this* attaching client's own environment, so
		# nudge the server to it now: it only affects $TERM for panes
		# created after this point, so it cannot disrupt anything already
		# running in the session.
		if BYOBU_TERM:
			subprocess.call(["tmux", "set-option", "-g", "default-terminal", BYOBU_TERM],
			                 stdout=open(os.devnull, "w"))
		if reuse_sessions:
			os.execvp("tmux", ["tmux", "-u", "attach", "-t", session_name])
		else:
			os.execvp("tmux", ["tmux", "-u", "new-session", "-t", session_name, ";", "set-option", "destroy-unattached"])
	else:
		os.execvp("screen", ["screen", "-AOxRR", session_name])


sessions = get_sessions()

show_shell = os.path.exists("%s/.always-select" % (BYOBU_CONFIG_DIR))
if len(sessions) > 1 or show_shell:
	sessions.append("NEW")
	text.append("Create a new Byobu session (%s)  [n, or n NAME]" % BYOBU_BACKEND)
	sessions.append("SHELL")
	text.append("Run a shell without Byobu (%s)  [s]" % SHELL)

new_name = None
if len(sessions) > 1:
	sys.stdout.write("\nByobu sessions...\n\n")
	tries = 0
	while tries < 3:
		i = 1
		for s in text:
			sys.stdout.write("  %d. %s\n" % (i, s))
			i += 1
		try:
			try:
				user_input = input("\nChoose 1-%d [1]: " % (i - 1))
			except Exception:
				user_input = ""
			if not user_input or user_input == "":
				choice = 1
				break
			# Fixed keys for the last two entries, whatever their number:
			# "n" (or "n NAME" to name it) for a new session, "s" for a
			# shell. Debian: #701055, #690582
			words = user_input.split()
			if words[0].lower() == "n" and len(words) <= 2:
				if len(words) == 2 and not re.match(r"^[A-Za-z0-9_-]+$", words[1]):
					tries += 1
					choice = -1
					sys.stderr.write("\nERROR: Session names may only use letters, digits, - and _\n")
					continue
				new_name = words[1] if len(words) == 2 else None
				choice = sessions.index("NEW") + 1
				break
			if words[0].lower() == "s" and len(words) == 1:
				choice = sessions.index("SHELL") + 1
				break
			try:
				choice = int(user_input.strip())
			except ValueError:
				# Never eval() what the user typed: this chooser can be
				# the only thing between a restricted login (ForceCommand,
				# rbash, kiosk) and an unrestricted shell.
				choice = -1
			if choice >= 1 and choice < i:
				break
			else:
				tries += 1
				choice = -1
				sys.stderr.write("\nERROR: Invalid input\n")
		except KeyboardInterrupt:
			sys.stdout.write("\n")
			sys.exit(0)
		except Exception:
			if choice == "" or choice == -1:
				choice = 1
				break
			tries += 1
			choice = -1
			sys.stderr.write("\nERROR: Invalid input\n")
elif len(sessions) == 1:
	# Auto-select the only session
	choice = 1

if choice >= 1:
	if sessions[choice - 1] == "NEW":
		# Create a new session
		if BYOBU_BACKEND == "tmux":
			name = ["-s", new_name] if new_name else []
			os.execvp("byobu", ["byobu", "new-session"] + name + [SHELL])
		else:
			name = ["-S", new_name] if new_name else []
			os.execvp("byobu", ["byobu"] + name + [SHELL])
	elif sessions[choice - 1] == "SHELL":
		os.execvp(SHELL, [SHELL])
	else:
		# Attach to the chosen session; must use the binary, not the wrapper!
		attach_session(sessions[choice - 1])

# No valid selection, default to the youngest session, create if necessary
if BYOBU_BACKEND == "tmux":
	os.execvp("tmux", ["tmux"])
else:
	os.execvp("screen", ["screen", "-AOxRR"])
