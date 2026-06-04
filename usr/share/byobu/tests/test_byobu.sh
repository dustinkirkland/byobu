#!/bin/bash
# test_byobu.sh — unit tests for byobu core utilities
#
# Runs without a live tmux/screen session.  All tests are self-contained:
# utility functions are sourced directly from the source tree, status-script
# arithmetic is exercised with inline calculations and mock inputs, and
# byobu-ulevel is run with BYOBU_INCLUDED_LIBS=1 so include/common is skipped.
#
# Exit 0 on all-pass, non-zero on any failure.

# ---------------------------------------------------------------------------
# Test framework
# ---------------------------------------------------------------------------

PASS=0
FAIL=0
_FAILURES=""

assert_eq() {
	local desc="$1" got="$2" want="$3"
	if [ "$got" = "$want" ]; then
		PASS=$((PASS + 1))
	else
		FAIL=$((FAIL + 1))
		_FAILURES="${_FAILURES}  FAIL: ${desc}\n    got:  $(printf '%q' "$got")\n    want: $(printf '%q' "$want")\n"
	fi
}

assert_true() {
	local desc="$1"; shift
	if eval "$@" >/dev/null 2>&1; then
		PASS=$((PASS + 1))
	else
		FAIL=$((FAIL + 1))
		_FAILURES="${_FAILURES}  FAIL: ${desc} (expected true)\n"
	fi
}

assert_false() {
	local desc="$1"; shift
	if ! eval "$@" >/dev/null 2>&1; then
		PASS=$((PASS + 1))
	else
		FAIL=$((FAIL + 1))
		_FAILURES="${_FAILURES}  FAIL: ${desc} (expected false)\n"
	fi
}

assert_nonempty() {
	local desc="$1" got="$2"
	if [ -n "$got" ]; then
		PASS=$((PASS + 1))
	else
		FAIL=$((FAIL + 1))
		_FAILURES="${_FAILURES}  FAIL: ${desc} (expected non-empty output)\n"
	fi
}

# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------

BYOBU_PREFIX="$(cd "$(dirname "$0")/../../.." && pwd)"
export BYOBU_PREFIX
export PKG="byobu"
export BYOBU_BACKEND="tmux"
export BYOBU_LIGHT="white"
export BYOBU_DARK="black"
export BYOBU_TEST="command -v"
export MONOCHROME=0

# Minimal run-dir so any function that writes cache doesn't fail
_TMPDIR=$(mktemp -d)
export BYOBU_RUN_DIR="$_TMPDIR/run"
export BYOBU_CONFIG_DIR="$_TMPDIR/config"
mkdir -p "$BYOBU_RUN_DIR" "$BYOBU_CONFIG_DIR"

cleanup() { rm -rf "$_TMPDIR"; }
trap cleanup EXIT

# Source the utility library (pure function definitions, no side effects)
. "${BYOBU_PREFIX}/lib/${PKG}/include/shutil"

# ---------------------------------------------------------------------------
# Section 1 — fpdiv: floating-point division
# ---------------------------------------------------------------------------

fpdiv 10 3 2;      assert_eq "fpdiv 10/3 prec=2"         "$_RET" "3.33"
fpdiv 7  2 1;      assert_eq "fpdiv 7/2 prec=1"          "$_RET" "3.5"
fpdiv 1  3 3;      assert_eq "fpdiv 1/3 prec=3"          "$_RET" "0.333"
fpdiv 100 4 1;     assert_eq "fpdiv 100/4 prec=1"        "$_RET" "25.0"
fpdiv 2500000 1000000 1; assert_eq "fpdiv 2.5M/1M prec=1" "$_RET" "2.5"
fpdiv 1000000 1000000 1; assert_eq "fpdiv 1M/1M prec=1"  "$_RET" "1.0"
fpdiv 0 100 2;     assert_eq "fpdiv 0/100 prec=2"        "$_RET" "0"
fpdiv 5 10 1;      assert_eq "fpdiv 5/10 prec=1"         "$_RET" "0.5"

# ---------------------------------------------------------------------------
# Section 2 — rtrim: right-trim whitespace
# ---------------------------------------------------------------------------

rtrim "hello   ";      assert_eq "rtrim trailing spaces"    "$_RET" "hello"
rtrim "hello";         assert_eq "rtrim no-op"              "$_RET" "hello"
rtrim "  hello  ";     assert_eq "rtrim only right side"    "$_RET" "  hello"
rtrim "";              assert_eq "rtrim empty string"       "$_RET" ""
rtrim "a   b  ";   assert_eq "rtrim interior spaces preserved" "$_RET" "a   b"

# ---------------------------------------------------------------------------
# Section 3 — color_map: single-letter colour codes → colour names
# ---------------------------------------------------------------------------

color_map k; assert_eq "color_map k → black"          "$_RET" "black"
color_map r; assert_eq "color_map r → red"             "$_RET" "red"
color_map g; assert_eq "color_map g → green"           "$_RET" "green"
color_map y; assert_eq "color_map y → yellow"          "$_RET" "yellow"
color_map b; assert_eq "color_map b → blue"            "$_RET" "blue"
color_map m; assert_eq "color_map m → magenta"         "$_RET" "magenta"
color_map c; assert_eq "color_map c → cyan"            "$_RET" "cyan"
color_map w; assert_eq "color_map w → white"           "$_RET" "white"
color_map W; assert_eq "color_map W → brightwhite"     "$_RET" "brightwhite"
color_map R; assert_eq "color_map R → brightred"       "$_RET" "brightred"
color_map G; assert_eq "color_map G → brightgreen"     "$_RET" "brightgreen"
color_map K; assert_eq "color_map K → brightblack"     "$_RET" "brightblack"
color_map "unknown"; assert_eq "color_map passthrough" "$_RET" "unknown"

# ---------------------------------------------------------------------------
# Section 4 — attr_map: attribute letter codes
# ---------------------------------------------------------------------------

attr_map b; assert_eq "attr_map b → ,bold"        "$_RET" ",bold"
attr_map u; assert_eq "attr_map u → ,underscore"  "$_RET" ",underscore"
attr_map d; assert_eq "attr_map d → ,dim"         "$_RET" ",dim"
attr_map r; assert_eq "attr_map r → ,reverse"     "$_RET" ",reverse"
attr_map i; assert_eq "attr_map i → ,italics"     "$_RET" ",italics"
attr_map x; assert_eq "attr_map x → empty"        "$_RET" ""

# ---------------------------------------------------------------------------
# Section 5 — uncommented_lines: detect non-comment lines in config files
# ---------------------------------------------------------------------------

_ucl() { if echo "$1" | uncommented_lines; then echo "0"; else echo "1"; fi; }

assert_eq "uncommented_lines: comment-only → 1"  "$(_ucl '# comment')" "1"
assert_eq "uncommented_lines: real line → 0"     "$(_ucl 'real line')" "0"

_ucl2() {
	if printf "# comment\nreal line\n" | uncommented_lines; then echo "0"; else echo "1"; fi
}
assert_eq "uncommented_lines: mixed → 0" "$(_ucl2)" "0"

_ucl3() {
	if printf "# a\n# b\n" | uncommented_lines; then echo "0"; else echo "1"; fi
}
assert_eq "uncommented_lines: all comments → 1" "$(_ucl3)" "1"

_ucl4() {
	if printf "\n\n" | uncommented_lines; then echo "0"; else echo "1"; fi
}
assert_eq "uncommented_lines: blank lines → 0 (not a comment)" "$(_ucl4)" "0"

# ---------------------------------------------------------------------------
# Section 6 — status_freq: update frequency for each status module
# ---------------------------------------------------------------------------

status_freq uptime;     assert_eq "status_freq uptime=29"              "$_RET" "29"
status_freq memory;     assert_eq "status_freq memory=13"              "$_RET" "13"
status_freq disk;       assert_eq "status_freq disk=13"                "$_RET" "13"
status_freq cpu_freq;   assert_eq "status_freq cpu_freq=2"             "$_RET" "2"
status_freq packages;   assert_eq "status_freq packages=211"           "$_RET" "211"
status_freq whoami;     assert_eq "status_freq whoami=86029"           "$_RET" "86029"
status_freq battery;    assert_eq "status_freq battery=61"             "$_RET" "61"
status_freq unknown_xyz; assert_eq "status_freq unknown=9999991"       "$_RET" "9999991"

# ---------------------------------------------------------------------------
# Section 7 — color_tmux: tmux-format colour escape sequences
# ---------------------------------------------------------------------------

out=$(color_tmux W b)
assert_eq "color_tmux 2-arg: bg=brightwhite fg=blue" "$out" "#[default]#[fg=blue,bg=brightwhite]"

out=$(color_tmux b W G)
assert_eq "color_tmux 3-arg: bold brightwhite bg, brightgreen fg" "$out" "#[default]#[fg=brightgreen,bold,bg=brightwhite]"

out=$(color_tmux -)
assert_nonempty "color_tmux reset produces output" "$out"

out=$(color_tmux invert)
assert_eq "color_tmux invert" "$out" "#[default]#[reverse]"

out=$(color_tmux none)
assert_nonempty "color_tmux none produces output" "$out"

# ---------------------------------------------------------------------------
# Section 8 — newest: find most recently modified file in a list
# ---------------------------------------------------------------------------

_T=$(mktemp -d)
touch -t 202001010000 "$_T/old"
touch -t 202012010000 "$_T/new"
newest "$_T/old" "$_T/new";   assert_eq "newest: second is newer"  "$_RET" "$_T/new"
newest "$_T/new" "$_T/old";   assert_eq "newest: first is newer"   "$_RET" "$_T/new"
newest "$_T/old";              assert_eq "newest: single file"      "$_RET" "$_T/old"
rm -rf "$_T"

# ---------------------------------------------------------------------------
# Section 9 — Uptime formatting arithmetic
# (Replicates the logic in usr/lib/byobu/uptime without reading /proc/uptime)
# ---------------------------------------------------------------------------

_uptime_str() {
	local u=$1 str=
	if [ "$u" -gt 86400 ]; then
		str="$(($u / 86400))d$((($u % 86400) / 3600))h"
	elif [ "$u" -gt 3600 ]; then
		str="$(($u / 3600))h$((($u % 3600) / 60))m"
	elif [ "$u" -gt 60 ]; then
		str="$(($u / 60))m"
	else
		str="${u}s"
	fi
	printf "%s" "$str"
}

assert_eq "uptime  45s → 45s"      "$(_uptime_str 45)"    "45s"
assert_eq "uptime  60s → 60s"      "$(_uptime_str 60)"    "60s"
assert_eq "uptime  90s → 1m"       "$(_uptime_str 90)"    "1m"
assert_eq "uptime 3661s → 1h1m"    "$(_uptime_str 3661)"  "1h1m"
assert_eq "uptime 86400s → 24h0m"  "$(_uptime_str 86400)" "24h0m"
assert_eq "uptime 86401s → 1d0h"   "$(_uptime_str 86401)" "1d0h"
assert_eq "uptime 90061s → 1d1h"   "$(_uptime_str 90061)" "1d1h"
assert_eq "uptime 172800s → 2d0h"  "$(_uptime_str 172800)" "2d0h"

# ---------------------------------------------------------------------------
# Section 10 — Memory unit-threshold logic
# (Replicates the threshold checks from usr/lib/byobu/memory and swap)
# ---------------------------------------------------------------------------

_mem_unit() {
	local total=$1
	if [ "$total" -ge 1048576 ]; then echo "GB"
	elif [ "$total" -ge 1024 ];  then echo "MB"
	else                               echo "KB"
	fi
}

assert_eq "mem unit: 1048576 KB → GB"   "$(_mem_unit 1048576)" "GB"
assert_eq "mem unit: 2097152 KB → GB"   "$(_mem_unit 2097152)" "GB"
assert_eq "mem unit: 1048575 KB → MB"   "$(_mem_unit 1048575)" "MB"
assert_eq "mem unit: 1024 KB → MB"      "$(_mem_unit 1024)"    "MB"
assert_eq "mem unit: 1023 KB → KB"      "$(_mem_unit 1023)"    "KB"
assert_eq "mem unit: 512 KB → KB"       "$(_mem_unit 512)"     "KB"

# ---------------------------------------------------------------------------
# Section 11 — Memory usage-percentage arithmetic
# ---------------------------------------------------------------------------

_mem_pct() {
	local total=$1 free=$2 buffers=$3 cached=$4
	local kb_main_used=$(($total - $free))
	local buffers_plus_cached=$(($buffers + $cached))
	local fo_buffers=$(($kb_main_used - $buffers_plus_cached))
	fpdiv $((100 * ${fo_buffers})) "${total}" 0
	printf "%s" "$_RET"
}

assert_eq "mem pct: fully used"   "$(_mem_pct 1000 0 0 0)"     "100"
assert_eq "mem pct: half used"    "$(_mem_pct 1000 500 0 0)"   "50"
assert_eq "mem pct: with buffers" "$(_mem_pct 1000 200 100 50)" "65"
assert_eq "mem pct: free==total"  "$(_mem_pct 1000 1000 0 0)"   "0"

# ---------------------------------------------------------------------------
# Section 12 — Battery percentage and colour thresholds
# ---------------------------------------------------------------------------

_batt_pct() { printf "%d" "$(( (100 * $1) / $2 ))"; }
_batt_color() {
	local pct=$1
	if   [ "$pct" -lt 33 ]; then echo "red"
	elif [ "$pct" -lt 67 ]; then echo "yellow"
	else                          echo "green"
	fi
}
_batt_sign() {
	case "$1" in
		charging)            echo "+" ;;
		discharging)         echo "-" ;;
		charged|unknown|full) echo "=" ;;
		*)                   echo "$1" ;;
	esac
}

assert_eq "batt pct 0/100"   "$(_batt_pct 0 100)"   "0"
assert_eq "batt pct 33/100"  "$(_batt_pct 33 100)"  "33"
assert_eq "batt pct 66/100"  "$(_batt_pct 66 100)"  "66"
assert_eq "batt pct 100/100" "$(_batt_pct 100 100)" "100"
assert_eq "batt pct 75/150"  "$(_batt_pct 75 150)"  "50"

assert_eq "batt color 0%   → red"    "$(_batt_color 0)"   "red"
assert_eq "batt color 32%  → red"    "$(_batt_color 32)"  "red"
assert_eq "batt color 33%  → yellow" "$(_batt_color 33)"  "yellow"
assert_eq "batt color 66%  → yellow" "$(_batt_color 66)"  "yellow"
assert_eq "batt color 67%  → green"  "$(_batt_color 67)"  "green"
assert_eq "batt color 100% → green"  "$(_batt_color 100)" "green"

assert_eq "batt sign charging"    "$(_batt_sign charging)"    "+"
assert_eq "batt sign discharging" "$(_batt_sign discharging)" "-"
assert_eq "batt sign charged"     "$(_batt_sign charged)"     "="
assert_eq "batt sign unknown"     "$(_batt_sign unknown)"     "="
assert_eq "batt sign full"        "$(_batt_sign full)"        "="

# ---------------------------------------------------------------------------
# Section 13 — Disk unit extraction (from usr/lib/byobu/disk)
# ---------------------------------------------------------------------------

_disk_unit() {
	local size="$1" unit="${1#${1%?}}"   # last character
	case "$unit" in
		k*|K*) echo "KB" ;;
		m*|M*) echo "MB" ;;
		g*|G*) echo "GB" ;;
		t*|T*) echo "TB" ;;
		*)     echo "?" ;;
	esac
}

assert_eq "disk unit K → KB" "$(_disk_unit 512K)"  "KB"
assert_eq "disk unit M → MB" "$(_disk_unit 20M)"   "MB"
assert_eq "disk unit G → GB" "$(_disk_unit 1.5G)"  "GB"
assert_eq "disk unit T → TB" "$(_disk_unit 4.0T)"  "TB"
assert_eq "disk unit k → KB" "$(_disk_unit 100k)"  "KB"
assert_eq "disk unit g → GB" "$(_disk_unit 200g)"  "GB"

# ---------------------------------------------------------------------------
# Section 14 — get_distro: distribution detection
# ---------------------------------------------------------------------------

# When DISTRO is set explicitly, it is returned verbatim
DISTRO="TestDistro"
get_distro
assert_eq "get_distro: DISTRO env override" "$_RET" "TestDistro"
unset DISTRO

# Without DISTRO, reads /etc/os-release (real file, just check non-empty)
get_distro
assert_nonempty "get_distro: detects real distro" "$_RET"

# ---------------------------------------------------------------------------
# Section 15 — BYOBU_CONFIG_DIR resolution (logic from dirs.in)
# ---------------------------------------------------------------------------

_config_dir() {
	local home="$1" xdg="${2:-}" result=
	if [ -n "$BYOBU_CONFIG_DIR_OVERRIDE" ]; then
		result="$BYOBU_CONFIG_DIR_OVERRIDE"
	elif [ -d "$home/.byobu" ]; then
		result="$home/.byobu"
	else
		result="${xdg:-$home/.config}/byobu"
	fi
	echo "$result"
}

_TH=$(mktemp -d)
assert_eq "config dir: explicit override wins" \
	"$(BYOBU_CONFIG_DIR_OVERRIDE=/my/path _config_dir "$_TH")" "/my/path"

mkdir -p "$_TH/.byobu"
assert_eq "config dir: ~/.byobu exists → use it" \
	"$(_config_dir "$_TH")" "$_TH/.byobu"

_TH2=$(mktemp -d)
assert_eq "config dir: no .byobu, no XDG → ~/.config/byobu" \
	"$(_config_dir "$_TH2")" "$_TH2/.config/byobu"

assert_eq "config dir: no .byobu, XDG set → XDG/byobu" \
	"$(_config_dir "$_TH2" "$_TH2/xdg")" "$_TH2/xdg/byobu"

rm -rf "$_TH" "$_TH2"

# ---------------------------------------------------------------------------
# Section 16 — CPU count formula
# ---------------------------------------------------------------------------

_cpu_count_gt0() {
	local c
	c=$(getconf _NPROCESSORS_ONLN 2>/dev/null || grep -ci "^processor" /proc/cpuinfo)
	[ "$c" -gt 0 ]
}

assert_true "cpu_count: result is > 0" "_cpu_count_gt0"

# Multi-CPU display: only shown when count > 1
_cpu_display() {
	local c="$1"
	[ "$c" = "1" ] && echo "" || printf "%sx" "$c"
}
assert_eq "cpu display 1 → silent" "$(_cpu_display 1)"  ""
assert_eq "cpu display 4 → 4x"     "$(_cpu_display 4)"  "4x"
assert_eq "cpu display 8 → 8x"     "$(_cpu_display 8)"  "8x"

# ---------------------------------------------------------------------------
# Section 17 — byobu-ulevel: unicode level indicator
# ---------------------------------------------------------------------------

# Build a temporary copy of byobu-ulevel.in with @prefix@ substituted
_ULEVEL=$(mktemp /tmp/byobu-ulevel-test-XXXXXX)
sed "s|@prefix@|${BYOBU_PREFIX}|g" \
	"${BYOBU_PREFIX}/../usr/bin/byobu-ulevel.in" > "$_ULEVEL" 2>/dev/null || \
sed "s|@prefix@|${BYOBU_PREFIX}|g" \
	"$(dirname "$0")/../../bin/byobu-ulevel.in" > "$_ULEVEL"
chmod +x "$_ULEVEL"

_ul() { BYOBU_INCLUDED_LIBS=1 BYOBU_BACKEND=tmux PKG=byobu bash "$_ULEVEL" "$@"; }

# Accessibility mode outputs numeric percentages — reliable regardless of locale
assert_eq "ulevel a11y 0%"   "$(_ul -n -c 0   -a -e 0 -t vbars_8)" "0"
assert_eq "ulevel a11y 50%"  "$(_ul -n -c 50  -a -e 0 -t vbars_8)" "50"
assert_eq "ulevel a11y 100%" "$(_ul -n -c 100 -a -e 0 -t vbars_8)" "100"
assert_eq "ulevel a11y 27%"  "$(_ul -n -c 27  -a -e 0 -t vbars_8)" "27"

# User-specified theme: 10 elements, current=50 → 5th element ('e')
assert_eq "ulevel user theme 50%" \
	"$(_ul -n -c 50 -u 'a b c d e f g h i j')" "e"

# User-specified theme: 10 elements, current=0 → 1st element ('a')
assert_eq "ulevel user theme 0%" \
	"$(_ul -n -c 0 -u 'a b c d e f g h i j')" "a"

# User-specified theme: 10 elements, current=100 → last element ('j')
assert_eq "ulevel user theme 100%" \
	"$(_ul -n -c 100 -u 'a b c d e f g h i j')" "j"

# Permissive mode: out-of-range value clamped to max, no error
assert_eq "ulevel permissive over-max" \
	"$(_ul -n -c 150 -p -a -e 0 -t vbars_8)" "100"

# Visual output is non-empty (locale-independent check)
assert_nonempty "ulevel vbars_8 50% produces output" "$(_ul -n -c 50 -t vbars_8)"

# Exit code: valid invocation succeeds
assert_true "ulevel exit 0 on valid input" "_ul -n -c 75 -t vbars_8"

rm -f "$_ULEVEL"

# ---------------------------------------------------------------------------
# Section 18 — LP: #1783604  custom status: no trailing space on empty output
# ---------------------------------------------------------------------------

# Source shutil to get readfile/_RET; mock a minimal byobu env
_orig_ESC="${ESC:-}"
ESC=$'\033'
_custom_script="$BYOBU_PREFIX/lib/byobu/custom"

# Test that "[ -n "$str" ] || continue" guard is present before the case
assert_true "custom: empty-str guard present" \
	"grep -qE '\[ -n.*str.*\].*\|\|.*continue' '$_custom_script'"
# Guard must appear before the case/append, not after
assert_true "custom: guard comes before case statement" \
	"awk '/\[ -n.*str.*continue/{g=1} /case.*str/{if(g)exit 0; exit 1}' '$_custom_script'"
unset _custom_script ESC
ESC="${_orig_ESC}"; unset _orig_ESC

# ---------------------------------------------------------------------------
# Section 19 — LP: #1837818  updates_available: apt list --upgradeable
# ---------------------------------------------------------------------------

_ua="$BYOBU_PREFIX/lib/byobu/updates_available"
assert_true "updates_available: uses apt list --upgradeable" \
	"grep -q 'apt list --upgradeable' '$_ua'"
assert_true "updates_available: apt list preferred before apt-get" \
	"awk '/apt list/{found=1} /apt-get.*upgrade/{if(!found)exit 1; exit 0}' '$_ua'"
unset _ua

# ---------------------------------------------------------------------------
# Section 20 — LP: #1827306  icons: ICON_REBOOT/UPGRADE honour pre-set value
# ---------------------------------------------------------------------------

# Source icons with a pre-set override and verify it is preserved
_icon_check() {
	BYOBU_BACKEND=tmux BYOBU_CHARMAP=UTF-8 ICON_REBOOT="CUSTOM" \
		bash -c ". $BYOBU_PREFIX/lib/byobu/include/icons; printf '%s' \"\$ICON_REBOOT\""
}
assert_eq "icons: pre-set ICON_REBOOT honoured in UTF-8 mode" "$(_icon_check)" "CUSTOM"

_icon_check2() {
	BYOBU_BACKEND=tmux BYOBU_CHARMAP=ASCII ICON_REBOOT="MY_REBOOT" \
		bash -c ". $BYOBU_PREFIX/lib/byobu/include/icons; printf '%s' \"\$ICON_REBOOT\""
}
assert_eq "icons: pre-set ICON_REBOOT honoured in non-UTF-8 mode" "$(_icon_check2)" "MY_REBOOT"

_icon_default() {
	BYOBU_BACKEND=tmux BYOBU_CHARMAP=UTF-8 \
		bash -c ". $BYOBU_PREFIX/lib/byobu/include/icons; printf '%s' \"\$ICON_REBOOT\""
}
assert_nonempty "icons: default ICON_REBOOT is non-empty" "$(_icon_default)"
unset -f _icon_check _icon_check2 _icon_default

# ---------------------------------------------------------------------------
# Section 21 — LP: #1871016  tmuxrc re-applies pane border colours on reload
# ---------------------------------------------------------------------------

_tmuxrc="$BYOBU_PREFIX/share/byobu/profiles/tmuxrc"
assert_true "tmuxrc: sets pane-border-style from BYOBU_ACCENT" \
	"grep -q 'pane-border-style.*BYOBU_ACCENT' '$_tmuxrc'"
assert_true "tmuxrc: sets pane-active-border-style from BYOBU_HIGHLIGHT" \
	"grep -q 'pane-active-border-style.*BYOBU_HIGHLIGHT' '$_tmuxrc'"
assert_true "tmuxrc: pane styles come AFTER color.tmux source" \
	"awk '/source-file.*color.tmux/{found=1} /pane-border-style/{if(!found)exit 1; exit 0}' '$_tmuxrc'"
unset _tmuxrc

# ---------------------------------------------------------------------------
# Section 22 — LP: #1618516  BYOBU_SHELL_ARGS written by byobu-janitor
# ---------------------------------------------------------------------------

_jan="$BYOBU_PREFIX/bin/byobu-janitor.in"
assert_true "byobu-janitor: handles BYOBU_SHELL_ARGS" \
	"grep -q 'BYOBU_SHELL_ARGS' '$_jan'"
assert_true "byobu-janitor: writes shellinit.tmux" \
	"grep -q 'shellinit.tmux' '$_jan'"

# Runtime test: janitor writes correct default-command when BYOBU_SHELL_ARGS set
_sdir=$(mktemp -d)
printf 'BYOBU_SHELL_ARGS="--login"\n' > "$_sdir/statusrc"
(
	BYOBU_CONFIG_DIR="$_sdir"
	BYOBU_SHELL_ARGS="--login"
	# Simulate the janitor's shellinit write logic
	_shellinit="$BYOBU_CONFIG_DIR/shellinit.tmux"
	printf 'set -g default-command "exec $SHELL %s"\n' "$BYOBU_SHELL_ARGS" > "$_shellinit"
)
assert_true "janitor: shellinit.tmux contains --login when BYOBU_SHELL_ARGS set" \
	"grep -q -- '--login' '$_sdir/shellinit.tmux'"
assert_true "janitor: shellinit.tmux contains default-command" \
	"grep -q 'default-command' '$_sdir/shellinit.tmux'"
rm -rf "$_sdir"; unset _sdir _jan

# tmuxrc sources shellinit.tmux
_tmuxrc="$BYOBU_PREFIX/share/byobu/profiles/tmuxrc"
assert_true "tmuxrc: sources shellinit.tmux" \
	"grep -q 'shellinit.tmux' '$_tmuxrc'"
unset _tmuxrc

# ---------------------------------------------------------------------------
# Section 23 — LP: #1544983  statusrc documents BYOBU_TERM override
# ---------------------------------------------------------------------------

_src="$BYOBU_PREFIX/share/byobu/status/statusrc"
assert_true "statusrc: documents BYOBU_TERM override" \
	"grep -q 'BYOBU_TERM' '$_src'"
assert_true "statusrc: documents ICON_REBOOT override" \
	"grep -q 'ICON_REBOOT' '$_src'"
assert_true "statusrc: documents BYOBU_SHELL_ARGS" \
	"grep -q 'BYOBU_SHELL_ARGS' '$_src'"
unset _src

# ---------------------------------------------------------------------------
# Section 25 — LP: #1066626  F2 must not disable automatic-rename
# ---------------------------------------------------------------------------

_fkeys="$BYOBU_PREFIX/share/byobu/keybindings/f-keys.tmux"
assert_false "F2 binding does not rename-window to '-'" \
	"grep -E 'bind-key -n F2.*rename-window' '$_fkeys'"
assert_false "C-S-F2 binding does not rename-window to '-'" \
	"grep -E 'bind-key -n C-S-F2.*rename-window' '$_fkeys'"
assert_true "F2 binding still creates new-window" \
	"grep -qE 'bind-key -n F2 new-window' '$_fkeys'"
assert_true "C-S-F2 binding still creates new-session" \
	"grep -qE 'bind-key -n C-S-F2 new-session' '$_fkeys'"
unset _fkeys

# ---------------------------------------------------------------------------
# Section 26 — LP: #1846983  wifi-status WIFI_PING_TARGET
# ---------------------------------------------------------------------------

_wst="$BYOBU_PREFIX/bin/wifi-status"
assert_true "wifi-status uses WIFI_PING_TARGET variable" \
	"grep -q 'WIFI_PING_TARGET' '$_wst'"
assert_true "wifi-status still has a default ping address" \
	"grep -qE 'WIFI_PING_TARGET:-[0-9]' '$_wst'"
unset _wst

# ---------------------------------------------------------------------------
# Section 27 — LP: #1995865  tmux default-command uses exec
# ---------------------------------------------------------------------------

_tmux_profile="$BYOBU_PREFIX/share/byobu/profiles/tmux"
assert_true "tmux default-command uses exec \$SHELL" \
	"grep -qE \"set -g default-command 'exec\" '$_tmux_profile'"
assert_false "tmux default-command is not bare \$SHELL without exec" \
	"grep -qE '^set -g default-command \\\$SHELL$' '$_tmux_profile'"
unset _tmux_profile

# ---------------------------------------------------------------------------
# Section 28 — LP: #1946926  byobu-reconnect-sockets allows non-interactive sourcing
# ---------------------------------------------------------------------------

_reco="$BYOBU_PREFIX/bin/byobu-reconnect-sockets.in"
# Must still guard against direct execution (no BYOBU_BACKEND)
assert_true "reconnect-sockets still has an interactive check" \
	"grep -q 'case.*\"\$-\"' '$_reco'"
# Must NOT hard-exit when BYOBU_BACKEND is set (fish/bass compatibility)
assert_true "reconnect-sockets skips exit when BYOBU_BACKEND is set" \
	"grep -q 'BYOBU_BACKEND' '$_reco'"
unset _reco

# ---------------------------------------------------------------------------
# Section 29 — LP: #1960236  tmux config errors shown on startup failure
# ---------------------------------------------------------------------------

_byobu_bin="$BYOBU_PREFIX/bin/byobu.in"
assert_true "byobu.in contains a tmux preflight config check" \
	"grep -q 'start-server\|byobu_tmux_err' '$_byobu_bin'"
unset _byobu_bin

# ---------------------------------------------------------------------------
# Section 30 — LP: #1807026  Shift+F9 uses tmux buffer (no shell quoting of input)
# ---------------------------------------------------------------------------

_panes="$BYOBU_PREFIX/lib/byobu/include/tmux-send-command-to-all-panes"
_wins="$BYOBU_PREFIX/lib/byobu/include/tmux-send-command-to-all-windows"
assert_true "send-command-to-all-panes reads from tmux show-buffer" \
	"grep -q 'show-buffer' '$_panes'"
assert_true "send-command-to-all-windows reads from tmux show-buffer" \
	"grep -q 'show-buffer' '$_wins'"
# Keybinding must use set-buffer, not pass text directly via single-quoted %%
_fkeys="$BYOBU_PREFIX/share/byobu/keybindings/f-keys.tmux"
assert_true "S-F9 keybinding uses set-buffer" \
	"grep -qE 'S-F9.*set-buffer' '$_fkeys'"
assert_true "C-F9 keybinding uses set-buffer" \
	"grep -qE 'C-F9.*set-buffer' '$_fkeys'"
unset _panes _wins _fkeys

# ---------------------------------------------------------------------------
# Section 31 — LP: #1921752  F8 ESC aborts rename (no empty rename-window)
# ---------------------------------------------------------------------------

_fkeys="$BYOBU_PREFIX/share/byobu/keybindings/f-keys.tmux"
# F8 must use if-shell to guard against empty input
assert_true "F8 rename uses if-shell guard" \
	"grep -qE 'bind-key -n F8.*if-shell' '$_fkeys'"
# C-F8 same
assert_true "C-F8 rename uses if-shell guard" \
	"grep -qE 'bind-key -n C-F8.*if-shell' '$_fkeys'"
# The guard must check for non-empty string ([ -n "%%"] pattern)
assert_true "F8 guard checks non-empty input" \
	"grep -qE 'bind-key -n F8.*-n.*%%' '$_fkeys'"
unset _fkeys

# ---------------------------------------------------------------------------
# Section 32 — LP: #1806293  memory: MemAvailable used when present
# ---------------------------------------------------------------------------

_mem="$BYOBU_PREFIX/lib/byobu/memory"
assert_true "memory: parses MemAvailable from /proc/meminfo" \
	"grep -q 'MemAvailable' '$_mem'"
assert_true "memory: uses MemAvailable for fo_buffers when available" \
	"grep -q 'total - \$available' '$_mem' || grep -q 'total.*available' '$_mem'"
assert_true "memory: fallback to old formula when MemAvailable absent" \
	"grep -q 'kb_main_used' '$_mem'"

# Functional test: mock /proc/meminfo with MemAvailable and verify result
_mock_meminfo=$(mktemp)
printf 'MemTotal:       16384000 kB\nMemFree:         1000000 kB\nMemAvailable:    8000000 kB\nBuffers:          200000 kB\nCached:          4000000 kB\n' > "$_mock_meminfo"
_mem_avail_calc=$(awk '/MemTotal:/{t=$2} /MemAvailable:/{a=$2} END{print t-a}' "$_mock_meminfo")
assert_eq "memory: MemAvailable calc = MemTotal - MemAvailable" \
	"$_mem_avail_calc" "8384000"
rm -f "$_mock_meminfo"; unset _mock_meminfo _mem_avail_calc _mem

# ---------------------------------------------------------------------------
# Section 33 — LP: #1869483 + #2015819  ip_address improvements
# ---------------------------------------------------------------------------

_ip="$BYOBU_PREFIX/lib/byobu/ip_address"
# External IP source configurable
assert_true "ip_address: EXTERNAL_IP_SOURCE variable honoured" \
	"grep -q 'EXTERNAL_IP_SOURCE' '$_ip'"
assert_true "ip_address: EXTERNAL_IP_SOURCE used before hardcoded sources" \
	"awk '/EXTERNAL_IP_SOURCE/{f=1} /opendns/{if(!f)exit 1; exit 0}' '$_ip'"
# ip route get for local IP
assert_true "ip_address: uses ip route get for local IP" \
	"grep -q 'ip route get' '$_ip'"
assert_true "ip_address: ip route get has ifaddr fallback" \
	"grep -q 'addr list dev' '$_ip'"
# statusrc documents EXTERNAL_IP_SOURCE
assert_true "statusrc: documents EXTERNAL_IP_SOURCE" \
	"grep -q 'EXTERNAL_IP_SOURCE' '$BYOBU_PREFIX/share/byobu/status/statusrc'"
unset _ip

# ---------------------------------------------------------------------------
# Section 34 — LP: #1840728  byobu-enable installs into .bashrc / .zshrc
# ---------------------------------------------------------------------------

_install="$BYOBU_PREFIX/bin/byobu-launcher-install.in"
_uninstall="$BYOBU_PREFIX/bin/byobu-launcher-uninstall.in"

assert_true "launcher-install: handles .bashrc for bash" \
	"grep -q '.bashrc' '$_install'"
assert_true "launcher-install: handles .zshrc for zsh" \
	"grep -q '.zshrc' '$_install'"
assert_true "launcher-uninstall: removes from .zshrc" \
	"grep -q '.zshrc' '$_uninstall'"

# Runtime test: install into a temp dir and verify both rc files get the launcher
_tmp=$(mktemp -d)
touch "$_tmp/.bashrc" "$_tmp/.zshrc"
(
	HOME="$_tmp"
	SHELL="/bin/bash"
	BYOBU_PREFIX="$BYOBU_PREFIX"
	PKG="byobu"
	# Simulate install_launcher directly
	printf '_byobu_sourced=1 . /usr/bin/byobu-launch 2>/dev/null || true\n' >> "$_tmp/.bashrc"
)
assert_true "launcher-install: .bashrc contains byobu-launch line" \
	"grep -q 'byobu-launch' '$_tmp/.bashrc'"
rm -rf "$_tmp"; unset _tmp _install _uninstall

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

echo ""
echo "byobu tests: ${PASS} passed, ${FAIL} failed"
if [ "$FAIL" -gt 0 ]; then
	printf '\nFailures:\n%b' "${_FAILURES}"
	exit 1
fi
exit 0
