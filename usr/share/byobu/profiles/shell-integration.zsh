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

# zsh counterpart to shell-integration.bash -- see that file for the OSC 133
# marker reference and rationale. zsh's native precmd/preexec hook arrays
# make this simpler than bash's PROMPT_COMMAND/PS0 juggling: no risk of
# reordering someone else's chained PROMPT_COMMAND string, just an array
# entry each, and $? is still the last command's exit status when a precmd
# function runs, same as bash.

__byobu_osc133_precmd() {
	local _exit=$?
	# BEL (\a), not ST (ESC \\) -- see shell-integration.bash for why: ST's
	# second byte is a literal backslash, which can collide with a prompt
	# framework's own escaping when the B marker sits inside $PROMPT. BEL
	# is single-byte and OSC-spec-legal; using it uniformly here too.
	printf '\033]133;D;%d\a' "$_exit"
	printf '\033]133;A\a'
}

__byobu_osc133_preexec() {
	printf '\033]133;C\a'
}

if [[ -z "${precmd_functions[(r)__byobu_osc133_precmd]}" ]]; then
	precmd_functions=(__byobu_osc133_precmd $precmd_functions)
fi
if [[ -z "${preexec_functions[(r)__byobu_osc133_preexec]}" ]]; then
	preexec_functions=(__byobu_osc133_preexec $preexec_functions)
fi

__byobu_osc133_b=$'\033]133;B\a'
case "$PROMPT" in
	*"$__byobu_osc133_b"*) ;;
	*) PROMPT="${PROMPT}%{${__byobu_osc133_b}%}" ;;
esac
unset __byobu_osc133_b

# vi: syntax=sh ts=4 noexpandtab
