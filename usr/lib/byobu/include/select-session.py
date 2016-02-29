#!/usr/bin/python
#
#    select-session.py
#    Copyright (C) 2010 Canonical Ltd.
#    Copyright (C) 2012-2014 Dustin Kirkland <kirkland@byobu.co>
#
#    Authors: Dustin Kirkland <kirkland@byobu.co>
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
except:
	# But fall back to using the default input
	True


PKG = "byobu"
SHELL = os.getenv("SHELL", "/bin/bash")
HOME = os.getenv("HOME")
BYOBU_CONFIG_DIR = os.getenv("BYOBU_CONFIG_DIR", HOME + "/.byobu")
BYOBU_BACKEND = os.getenv("BYOBU_BACKEND", "tmux")
choice = -1
sessions = []
text = []

BYOBU_UPDATE_ENVVARS = ["DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "SESSION_MANAGER", "GPG_AGENT_INFO", "XDG_SESSION_COOKIE", "XDG_SESSION_PATH", "GNOME_KEYRING_CONTROL", "GNOME_KEYRING_PID", "GPG_AGENT_INFO", "SSH_ASKPASS", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "WINDOWID", "UPSTART_JOB", "UPSTART_EVENTS", "UPSTART_SESSION", "UPSTART_INSTANCE"]


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
			for s in output.splitlines():
				# Ignore hidden sessions (named sessions that start with a "_")
				if s and not s.startswith("_"):
					text.append("tmux: %s" % s.strip())
					sessions.append("tmux____%s" % s.split(":")[0])
					i += 1
	return sessions


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
	# must use the binary, not the wrapper!
	if backend == "tmux":
		os.execvp("tmux", ["", "-2", "attach", "-t", session_name])
	else:
		os.execvp("screen", ["", "-AOxRR", session_name])

sessions = get_sessions()

show_shell = os.path.exists("%s/.always-select" % (BYOBU_CONFIG_DIR))
if len(sessions) > 1 or show_shell:
	sessions.append("NEW")
	text.append("Create a new Byobu session (%s)" % BYOBU_BACKEND)
	sessions.append("SHELL")
	text.append("Run a shell without Byobu (%s)" % SHELL)

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
				choice = int(input("\nChoose 1-%d [1]: " % (i - 1)))
			except:
				choice = int(eval(input("\nChoose 1-%d [1]: " % (i - 1))))
			if choice >= 1 and choice < i:
				break
			else:
				tries += 1
				choice = -1
				sys.stderr.write("\nERROR: Invalid input\n")
		except KeyboardInterrupt:
			sys.stdout.write("\n")
			sys.exit(0)
		except:
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
			os.execvp("byobu", ["", "new-session", SHELL])
		else:
			os.execvp("byobu", ["", SHELL])
	elif sessions[choice - 1] == "SHELL":
		os.execvp(SHELL, [SHELL])
	else:
		# Attach to the chosen session; must use the binary, not the wrapper!
		attach_session(sessions[choice - 1])

# No valid selection, default to the youngest session, create if necessary
if BYOBU_BACKEND == "tmux":
	args = ""
else:
	args = "-AOxRR"
os.execvp("byobu", ["", args])
