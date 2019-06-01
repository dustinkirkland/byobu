###############################################################################
#    tmux common profile
#      This tmux configuration profile is intended to correspond to Byobu's
#      traditional GNU Screen profile
#
#    Copyright (C) 2011-2014 Dustin Kirkland
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
###############################################################################

# Initialize environment, clean up
set-environment -g BYOBU_BACKEND tmux
new-session -d byobu-janitor
set -s escape-time 0

# Change to Screen's ctrl-a escape sequence and use screen-like keys
source $BYOBU_PREFIX/share/byobu/keybindings/tmux-screen-keys.conf

# Add ^A, F12 to the prefix list
set -g prefix ^A
# Note, prefix2 only available in tmux 1.6+
set -g prefix2 F12

# Byobu's Keybindings
source $BYOBU_PREFIX/share/byobu/keybindings/f-keys.tmux
set-window-option -g xterm-keys on

#set-option -g set-titles on
#set-option -g set-titles-string '#(whoami)@#H - byobu (#S)'
set-option -g pane-active-border-style bg=$BYOBU_HIGHLIGHT,fg=$BYOBU_HIGHLIGHT
set-option -g pane-border-style fg=$BYOBU_ACCENT
set-option -g history-limit 100000
set-option -g display-panes-time 150
set-option -g display-panes-colour $BYOBU_ACCENT
set-option -g display-panes-active-colour $BYOBU_HIGHLIGHT
set-option -g clock-mode-colour $BYOBU_ACCENT
set-option -g clock-mode-style 24
set-option -g mode-keys vi
set-option -g mode-style bg=$BYOBU_ACCENT,fg=$BYOBU_LIGHT

set-window-option -g window-status-style default
set-window-option -g window-status-style bg=$BYOBU_DARK,fg=$BYOBU_LIGHT
set-window-option -g window-status-current-style bg=$BYOBU_DARK,fg=$BYOBU_LIGHT,reverse
set-window-option -g window-status-activity-style bg=$BYOBU_DARK,fg=$BYOBU_LIGHT,bold
set-window-option -g automatic-rename on
set-window-option -g aggressive-resize on
set-window-option -g monitor-activity on

set -g default-terminal "$BYOBU_TERM"

# The following helps with Shift-PageUp/Shift-PageDown
set -g terminal-overrides 'xterm*:smcup@:rmcup@'

# Must set default-command to $SHELL, in order to not source ~/.profile
# BUG: Should *not* hardcode /bin/bash here
# Use byobu-shell here, to show MOTD, but would do so with every new window and split
#set -g default-command 'exec byobu-shell'
set -g default-command $SHELL

set -g status-style bg=$BYOBU_DARK,fg=$BYOBU_LIGHT
set -g status-interval 1
set -g status-left-length 256
set -g status-right-length 256
set -g status-left '#(byobu-status tmux_left)'
set -g status-right '#(byobu-status tmux_right)'$BYOBU_DATE$BYOBU_TIME
set -g message-style bg=$BYOBU_ACCENT,fg=white
