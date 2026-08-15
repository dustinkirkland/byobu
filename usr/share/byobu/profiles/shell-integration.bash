#    byobu's OSC 133 shell integration -- semantic prompt markers
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

# Emits OSC 133 semantic-prompt markers -- the protocol iTerm2, Kitty,
# Ghostty, VS Code, and Warp use for jump-to-prompt navigation and
# command-output selection -- so any terminal or tool with OSC 133 support,
# including Trustmux's daemon, can locate prompts and command boundaries
# precisely instead of guessing from rendered text.
#
# Reference: https://gitlab.freedesktop.org/terminal-wg/specifications/-/blob/master/proposals/semantic-prompts.md
#
#   A            -- prompt start
#   B            -- prompt end / command input starts
#   C            -- command executed, output starts
#   D;exit_code  -- command finished
#
# Chains with any existing PROMPT_COMMAND/PS0/PS1 rather than replacing them
# -- this is meant to layer onto a user's own prompt setup (or byobu's own
# colorized one from byobu-enable-prompt), not take it over.

__byobu_osc133_precmd() {
	# Must be the very first statement: subshells later in this function
	# (or in whatever PROMPT_COMMAND was chained after it) would otherwise
	# clobber $? before we capture the real command's exit status.
	local _exit=$?
	# BEL (\a), not ST (ESC \\), terminates these: ST's second byte is a
	# literal backslash, which collides with bash's own \[ \] PS1 parser
	# when the B marker (below) sits inside PS1 -- \\ right before the
	# closing \] reads as an escaped backslash, leaving a stray literal
	# ] in the rendered prompt. BEL is single-byte and OSC-spec-legal.
	printf '\033]133;D;%d\a' "$_exit"
	printf '\033]133;A\a'
}

__byobu_osc133_preexec() {
	printf '\033]133;C\a'
}

case "$PROMPT_COMMAND" in
	*__byobu_osc133_precmd*) ;;
	"") PROMPT_COMMAND="__byobu_osc133_precmd" ;;
	*)  PROMPT_COMMAND="__byobu_osc133_precmd; ${PROMPT_COMMAND}" ;;
esac

case "$PS0" in
	*__byobu_osc133_preexec*) ;;
	*) PS0="\$(__byobu_osc133_preexec)${PS0}" ;;
esac

__byobu_osc133_b=$'\033]133;B\a'
case "$PS1" in
	*"$__byobu_osc133_b"*) ;;
	*) PS1="${PS1}\[${__byobu_osc133_b}\]" ;;
esac
unset __byobu_osc133_b

# vi: syntax=sh ts=4 noexpandtab
