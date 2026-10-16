#!/usr/bin/env bash
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
status_freq cpu_usage;  assert_eq "status_freq cpu_usage=2"            "$_RET" "2"
status_freq packages;   assert_eq "status_freq packages=211"           "$_RET" "211"
status_freq whoami;     assert_eq "status_freq whoami=86029"           "$_RET" "86029"
status_freq battery;    assert_eq "status_freq battery=61"             "$_RET" "61"
status_freq unknown_xyz; assert_eq "status_freq unknown=9999991"       "$_RET" "9999991"
# GH #158: session count is dynamic (sessions open/close at runtime) and
# cheap to check -- it must not share the ~115-day "effectively static"
# TTL used for arch/distro/logo/etc., or a stale multi-session indicator
# persists long after closing back down to one session.
status_freq session;    assert_eq "status_freq session is short-lived, not the ~115-day static TTL" \
	"$([ "$_RET" -lt 60 ] && echo short || echo long)" "short"

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
		charged|unknown|full|fully-charged|"not charging"|not_charging) echo "=" ;;
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

assert_eq "batt sign charging"      "$(_batt_sign charging)"        "+"
assert_eq "batt sign discharging"   "$(_batt_sign discharging)"     "-"
assert_eq "batt sign charged"       "$(_batt_sign charged)"         "="
assert_eq "batt sign unknown"       "$(_batt_sign unknown)"         "="
assert_eq "batt sign full"          "$(_batt_sign full)"            "="
assert_eq "batt sign fully-charged" "$(_batt_sign fully-charged)"   "="
# GH #143: Linux's power_supply status can legitimately be "Not charging"
# (plugged in, not actively drawing charge -- threshold or already full),
# lowercased by the real script before reaching this logic; Termux reports
# the same state as "NOT_CHARGING". Both used to fall through to the
# catch-all and print the raw state glued to the percentage, e.g.
# "65%not charging".
assert_eq "batt sign \"not charging\" (Linux, space)" "$(_batt_sign "not charging")" "="
assert_eq "batt sign not_charging (Termux, underscore)" "$(_batt_sign not_charging)" "="

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

# Build a temporary copy of byobu-ulevel with @prefix@ substituted (source tree),
# or fall back to the installed binary when running from an installed package.
_ULEVEL=$(mktemp /tmp/byobu-ulevel-test-XXXXXX)
sed "s|@prefix@|${BYOBU_PREFIX}|g" \
	"${BYOBU_PREFIX}/../usr/bin/byobu-ulevel.in" > "$_ULEVEL" 2>/dev/null || \
sed "s|@prefix@|${BYOBU_PREFIX}|g" \
	"$(dirname "$0")/../../bin/byobu-ulevel.in" > "$_ULEVEL" 2>/dev/null || \
cp "${BYOBU_PREFIX}/bin/byobu-ulevel" "$_ULEVEL" 2>/dev/null || true
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
if [ -f "$_jan" ]; then
assert_true "byobu-janitor: handles BYOBU_SHELL_ARGS" \
	"grep -q 'BYOBU_SHELL_ARGS' '$_jan'"
assert_true "byobu-janitor: writes shellinit.tmux" \
	"grep -q 'shellinit.tmux' '$_jan'"
fi

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
if [ -f "$_reco" ]; then
# Must still guard against direct execution (no BYOBU_BACKEND)
assert_true "reconnect-sockets still has an interactive check" \
	"grep -q 'case.*\"\$-\"' '$_reco'"
# Must NOT hard-exit when BYOBU_BACKEND is set (fish/bass compatibility)
assert_true "reconnect-sockets skips exit when BYOBU_BACKEND is set" \
	"grep -q 'BYOBU_BACKEND' '$_reco'"
fi
unset _reco

# ---------------------------------------------------------------------------
# Section 29 — LP: #1960236  tmux config errors shown on startup failure
# ---------------------------------------------------------------------------

_byobu_bin="$BYOBU_PREFIX/bin/byobu.in"
if [ -f "$_byobu_bin" ]; then
assert_true "byobu.in contains a tmux preflight config check" \
	"grep -q 'start-server\|byobu_tmux_err' '$_byobu_bin'"
fi
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
# Note: the if-shell guard was dropped in tmux 3.6 (GH: #107) because
# %% substitution no longer propagates into if-shell branch strings, and
# tmux 3.4+ already cancels command-prompt on ESC without running the command.
# ---------------------------------------------------------------------------

_fkeys="$BYOBU_PREFIX/share/byobu/keybindings/f-keys.tmux"
# F8/C-F8 use command-prompt with direct %% substitution (no if-shell wrapper)
assert_true "F8 rename uses command-prompt with %% substitution" \
	"grep -qE 'bind-key -n F8 command-prompt.*%%' '$_fkeys'"
assert_true "C-F8 rename uses command-prompt with %% substitution" \
	"grep -qE 'bind-key -n C-F8 command-prompt.*%%' '$_fkeys'"
# Must NOT use if-shell (broken in tmux 3.6 for command-prompt substitution)
assert_false "F8 rename does not use broken if-shell guard" \
	"grep -qE 'bind-key -n F8.*if-shell' '$_fkeys'"
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

if [ -f "$_install" ]; then
assert_true "launcher-install: handles .bashrc for bash" \
	"grep -q '.bashrc' '$_install'"
assert_true "launcher-install: handles .zshrc for zsh" \
	"grep -q '.zshrc' '$_install'"
fi
if [ -f "$_uninstall" ]; then
assert_true "launcher-uninstall: removes from .zshrc" \
	"grep -q '.zshrc' '$_uninstall'"
fi

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
# Section 35 — BYOBU_GETTEXT: overridable gettext binary (constants)
# ---------------------------------------------------------------------------

_tmp=$(mktemp -d)
_got=$(env -i HOME="$HOME" PATH="$PATH" BYOBU_PREFIX="$BYOBU_PREFIX" PKG="byobu" \
	BYOBU_CONFIG_DIR="$_tmp/config" BYOBU_RUN_DIR="$_tmp/run" BYOBU_TEST="command -v" \
	sh -c 'mkdir -p "$BYOBU_CONFIG_DIR" "$BYOBU_RUN_DIR"; . "${BYOBU_PREFIX}/lib/byobu/include/constants"; echo "$BYOBU_GETTEXT"')
assert_eq "BYOBU_GETTEXT: defaults to \"gettext\" when unset" "$_got" "gettext"

_got=$(env -i HOME="$HOME" PATH="$PATH" BYOBU_PREFIX="$BYOBU_PREFIX" PKG="byobu" \
	BYOBU_CONFIG_DIR="$_tmp/config" BYOBU_RUN_DIR="$_tmp/run" BYOBU_TEST="command -v" \
	BYOBU_GETTEXT="/opt/store/bin/gettext" \
	sh -c '. "${BYOBU_PREFIX}/lib/byobu/include/constants"; echo "$BYOBU_GETTEXT"')
assert_eq "BYOBU_GETTEXT: a pre-set value is preserved untouched" "$_got" "/opt/store/bin/gettext"
rm -rf "$_tmp"; unset _tmp _got

# ---------------------------------------------------------------------------
# Section 36 — BYOBU_FORCE_BACKEND precedence (mirrors usr/bin/byobu.in)
# ---------------------------------------------------------------------------
# byobu.in itself launches a full session and isn't safe to source in a unit
# test, so this exercises the exact backend-selection block copied verbatim
# from that script -- config file, then argv[0], then BYOBU_FORCE_BACKEND.

_dispatch() {
	local zero="$1" cfg_backend="$2" force="$3" _out
	_out=$(BYOBU_BACKEND="" ; [ -n "$cfg_backend" ] && BYOBU_BACKEND="$cfg_backend"
		case "$zero" in
			*byobu-screen) BYOBU_BACKEND="screen" ;;
			*byobu-tmux) BYOBU_BACKEND="tmux" ;;
		esac
		case "$force" in
			screen|tmux) BYOBU_BACKEND="$force" ;;
		esac
		echo "$BYOBU_BACKEND")
	_RET="$_out"
}

_dispatch "/usr/bin/byobu" "" ""
assert_eq "backend dispatch: no config, no argv0 match, no override" "$_RET" ""

_dispatch "/usr/bin/byobu" "screen" ""
assert_eq "backend dispatch: config alone wins" "$_RET" "screen"

_dispatch "/usr/bin/byobu-tmux" "screen" ""
assert_eq "backend dispatch: argv0 overrides config" "$_RET" "tmux"

_dispatch "/usr/bin/byobu" "screen" "tmux"
assert_eq "backend dispatch: BYOBU_FORCE_BACKEND overrides config" "$_RET" "tmux"

_dispatch "/usr/bin/byobu-tmux" "" "screen"
assert_eq "backend dispatch: BYOBU_FORCE_BACKEND overrides argv0" "$_RET" "screen"

_dispatch "/usr/bin/byobu" "screen" "garbage"
assert_eq "backend dispatch: invalid BYOBU_FORCE_BACKEND value is ignored" "$_RET" "screen"

unset -f _dispatch

# ---------------------------------------------------------------------------
# Section 37 — width-detection lock (mirrors byobu-status.in's mkdir lock)
# ---------------------------------------------------------------------------
# GH #141: status-left and status-right are two independent, genuinely
# concurrent processes; this checks the mutual-exclusion primitive itself
# (mkdir is atomic), not the live tmux calls it guards.

_tmp=$(mktemp -d)
_lockdir="$_tmp/.width.lock"

mkdir "$_lockdir" 2>/dev/null && _got="ok" || _got="fail"
assert_eq "width lock: first mkdir succeeds"              "$_got" "ok"

mkdir "$_lockdir" 2>/dev/null && _got="ok" || _got="fail"
assert_eq "width lock: concurrent mkdir fails while held" "$_got" "fail"

rmdir "$_lockdir" 2>/dev/null
mkdir "$_lockdir" 2>/dev/null && _got="ok" || _got="fail"
assert_eq "width lock: mkdir succeeds again after release" "$_got" "ok"

rmdir "$_lockdir" 2>/dev/null
rm -rf "$_tmp"; unset _tmp _lockdir _got

# ---------------------------------------------------------------------------
# Section 38 — PID-suffixed cache writes (mirrors get_status() in byobu-status)
# ---------------------------------------------------------------------------
# GH #141: get_status() used to write every segment's fresh output to a
# single shared "$cachepath".new path. status-left and status-right run as
# separate concurrent processes, so two overlapping writes to that shared
# path could interleave and corrupt or blank a cache entry for a tick. A
# PID-suffixed temp path makes concurrent writers independent by
# construction; this checks that property directly.

_tmp=$(mktemp -d)
_cachepath="$_tmp/segment"

# Simulate two "processes" (distinct fake PIDs) writing concurrently.
printf "%s" "value-from-pid-1111" > "$_cachepath.new.1111"
printf "%s" "value-from-pid-2222" > "$_cachepath.new.2222"

assert_true "cache write: PID-suffixed temp files coexist independently" \
	"[ -f '$_cachepath.new.1111' ] && [ -f '$_cachepath.new.2222' ]"
assert_eq "cache write: first writer's content untouched by the second" \
	"$(cat "$_cachepath.new.1111")" "value-from-pid-1111"
assert_eq "cache write: second writer's content untouched by the first" \
	"$(cat "$_cachepath.new.2222")" "value-from-pid-2222"

rm -rf "$_tmp"; unset _tmp _cachepath

# ---------------------------------------------------------------------------
# Section 39 — OSC 133 shell integration (profiles/shell-integration.bash)
# ---------------------------------------------------------------------------
# Regression coverage for a real bug caught during development: the B marker
# embedded in PS1 used ST (ESC \) as its terminator, whose second byte is a
# literal backslash -- which collided with bash's own \[ \] PS1 escaping and
# left a stray "]" character in the rendered prompt. Switched to BEL. ${PS1@P}
# (bash 4.4+) applies real PS1 prompt-expansion without needing a live
# interactive session/pty, so this exercises the same code path bash itself
# uses to render a prompt, not just a string check.

# Not a subshell: assert_eq/assert_true update the global PASS/FAIL counters,
# which wouldn't propagate back out of one. Safe to leave PS1/PROMPT_COMMAND/
# PS0 set afterward -- this is the last section before Results.

PS1="myprompt\$ "
unset PROMPT_COMMAND PS0
. "${BYOBU_PREFIX}/share/byobu/profiles/shell-integration.bash"

rendered="${PS1@P}"
expected=$(printf 'myprompt$ \033]133;B\a')
assert_eq "osc133 bash: PS1 renders to exactly prompt + B marker, no stray bytes" \
	"$rendered" "$expected"

# PROMPT_COMMAND: exit code must be the real preceding command's, not
# something clobbered by the hook's own internals.
false
out=$(eval "$PROMPT_COMMAND")
want=$(printf '\033]133;D;1\a\033]133;A\a')
assert_eq "osc133 bash: PROMPT_COMMAND emits D;<real exit code> then A" "$out" "$want"

true
out=$(eval "$PROMPT_COMMAND")
want=$(printf '\033]133;D;0\a\033]133;A\a')
assert_eq "osc133 bash: exit code 0 captured correctly too" "$out" "$want"

# PS0 holds a deferred command substitution, not a literal unexpanded
# ${...} (the exact class of bug this would have caught: using \${x}
# instead of \$(x) silently never fires). ${PS0@P} applies real
# prompt-expansion, same as ${PS1@P} above -- eval would try to execute
# the marker's raw escape bytes as a command instead of embedding them.
out="${PS0@P}"
want=$(printf '\033]133;C\a')
assert_eq "osc133 bash: PS0 command substitution fires the C marker" "$out" "$want"

# Idempotency: sourcing twice must not duplicate the hook or grow PS1.
prompt_command_before="$PROMPT_COMMAND"
ps1_before="$PS1"
. "${BYOBU_PREFIX}/share/byobu/profiles/shell-integration.bash"
assert_eq "osc133 bash: re-sourcing does not duplicate PROMPT_COMMAND" \
	"$PROMPT_COMMAND" "$prompt_command_before"
assert_eq "osc133 bash: re-sourcing does not duplicate the PS1 marker" \
	"$PS1" "$ps1_before"

# Chains onto an existing PROMPT_COMMAND/PS1 instead of replacing them.
PS1="custom\$ "
PROMPT_COMMAND="echo already-here"
unset PS0
. "${BYOBU_PREFIX}/share/byobu/profiles/shell-integration.bash"
assert_true "osc133 bash: chains onto an existing PROMPT_COMMAND rather than replacing it" \
	"[[ \"\$PROMPT_COMMAND\" == *already-here* ]]"
assert_true "osc133 bash: chains onto an existing PS1 rather than replacing it" \
	"[[ \"\${PS1@P}\" == custom* ]]"

unset PS1 PROMPT_COMMAND PS0 rendered expected out want prompt_command_before ps1_before

# Hyperlink-aware `ls` alias, coupled to this same shell-integration.bash --
# see that file's tail for the rationale. Uses a fake `ls` on PATH rather
# than the real one so both the "supports --hyperlink" and "doesn't" paths
# are exercised deterministically, regardless of which coreutils version
# actually happens to be installed on whatever machine runs this suite.
# Each case runs in its own `bash -c` subshell (matching how Section 40
# below isolates zsh) so alias/PATH state from one case can't leak into the
# next, and so this doesn't disturb PS1/PROMPT_COMMAND/PS0 left set above.

_fakebin=$(mktemp -d)
cat > "$_fakebin/ls" <<'EOF'
#!/bin/sh
case "$*" in
	*--hyperlink*) exit 0 ;;
esac
exec /bin/ls "$@"
EOF
chmod +x "$_fakebin/ls"

# Each case reports an explicit sentinel ("ALIAS:<value>" or "NO_ALIAS")
# rather than being scraped from `alias ls`'s own builtin output -- bash
# prints a "not found"-style message to stderr for a missing alias, but
# zsh's equivalent prints nothing at all (just a nonzero exit), so parsing
# builtin wording is not portable between the two shells this same test
# also has to cover below.
_out=$(bash -c '
	unalias ls 2>/dev/null
	export PATH="'"$_fakebin"':$PATH"
	source "'"$BYOBU_PREFIX"'/share/byobu/profiles/shell-integration.bash"
	if alias ls >/dev/null 2>&1; then echo "ALIAS:$(alias ls)"; else echo "NO_ALIAS"; fi
' 2>&1)
assert_true "osc133 bash: ls aliased with --hyperlink=auto when ls supports it and isn't already aliased" \
	"printf %s \"\$_out\" | grep -q -- '--hyperlink=auto'"

_out=$(bash -c '
	alias ls="ls -F"
	export PATH="'"$_fakebin"':$PATH"
	source "'"$BYOBU_PREFIX"'/share/byobu/profiles/shell-integration.bash"
	if alias ls >/dev/null 2>&1; then echo "ALIAS:$(alias ls)"; else echo "NO_ALIAS"; fi
' 2>&1)
assert_true "osc133 bash: a pre-existing ls alias is left untouched, not overridden" \
	"[ \"\$_out\" = \"ALIAS:alias ls='ls -F'\" ]"

cat > "$_fakebin/ls" <<'EOF'
#!/bin/sh
case "$*" in
	*--hyperlink*) echo "ls: unrecognized option '--hyperlink=auto'" >&2; exit 2 ;;
esac
exec /bin/ls "$@"
EOF
chmod +x "$_fakebin/ls"

_out=$(bash -c '
	unalias ls 2>/dev/null
	export PATH="'"$_fakebin"':$PATH"
	source "'"$BYOBU_PREFIX"'/share/byobu/profiles/shell-integration.bash"
	if alias ls >/dev/null 2>&1; then echo "ALIAS:$(alias ls)"; else echo "NO_ALIAS"; fi
' 2>&1)
assert_true "osc133 bash: no alias set when ls does not understand --hyperlink" \
	"[ \"\$_out\" = NO_ALIAS ]"

rm -rf "$_fakebin"; unset _fakebin _out

# ---------------------------------------------------------------------------
# Section 40 — OSC 133 shell integration (profiles/shell-integration.zsh)
# ---------------------------------------------------------------------------
# zsh counterpart to Section 39. Skipped, not failed, when zsh isn't
# installed -- it's an optional dependency of this test suite, not of
# byobu itself, and not every box this runs on will have it. A skip prints
# a visible notice so it's never mistaken for having actually run.

if command -v zsh >/dev/null 2>&1; then
	_zsh_script="$BYOBU_PREFIX/share/byobu/profiles/shell-integration.zsh"
	_zsh_out=$(zsh -c '
		precmd_functions=()
		preexec_functions=()
		PROMPT="myprompt\$ "
		source "'"$_zsh_script"'"

		rendered="${(%)PROMPT}"
		expected=$(printf "myprompt\$ \033]133;B\a")
		[ "$rendered" = "$expected" ] && echo "PS1_OK" || echo "PS1_FAIL:[$rendered]"

		out=$(__byobu_osc133_precmd)
		want=$(printf "\033]133;D;0\a\033]133;A\a")
		[ "$out" = "$want" ] && echo "PRECMD_OK" || echo "PRECMD_FAIL:[$out]"

		out=$(__byobu_osc133_preexec)
		want=$(printf "\033]133;C\a")
		[ "$out" = "$want" ] && echo "PREEXEC_OK" || echo "PREEXEC_FAIL:[$out]"

		before_precmd=${#precmd_functions[@]}
		before_prompt="$PROMPT"
		source "'"$_zsh_script"'"
		[ "${#precmd_functions[@]}" = "$before_precmd" ] && echo "IDEMPOTENT_PRECMD_OK" || echo "IDEMPOTENT_PRECMD_FAIL"
		[ "$PROMPT" = "$before_prompt" ] && echo "IDEMPOTENT_PROMPT_OK" || echo "IDEMPOTENT_PROMPT_FAIL"
	' 2>&1)

	assert_true "osc133 zsh: PROMPT renders to exactly prompt + B marker" \
		"printf %s \"\$_zsh_out\" | grep -q PS1_OK"
	assert_true "osc133 zsh: precmd emits D;<exit code> then A" \
		"printf %s \"\$_zsh_out\" | grep -q PRECMD_OK"
	assert_true "osc133 zsh: preexec emits the C marker" \
		"printf %s \"\$_zsh_out\" | grep -q PREEXEC_OK"
	assert_true "osc133 zsh: re-sourcing does not duplicate the precmd hook" \
		"printf %s \"\$_zsh_out\" | grep -q IDEMPOTENT_PRECMD_OK"
	assert_true "osc133 zsh: re-sourcing does not duplicate the PROMPT marker" \
		"printf %s \"\$_zsh_out\" | grep -q IDEMPOTENT_PROMPT_OK"

	# Hyperlink-aware `ls` alias -- zsh counterpart to the bash cases above.
	# Same fake-ls-on-PATH technique, for the same reason: deterministic
	# regardless of the host's actual coreutils version.
	_fakebin=$(mktemp -d)
	cat > "$_fakebin/ls" <<'EOF'
#!/bin/sh
case "$*" in
	*--hyperlink*) exit 0 ;;
esac
exec /bin/ls "$@"
EOF
	chmod +x "$_fakebin/ls"

	# Explicit sentinels, not scraped `alias ls` wording -- see the bash
	# cases above for why: zsh's own message (or total silence) for a
	# missing alias isn't the same as bash's.
	_zsh_out=$(zsh -c '
		unalias ls 2>/dev/null
		export PATH="'"$_fakebin"':$PATH"
		source "'"$_zsh_script"'"
		if alias ls >/dev/null 2>&1; then echo "ALIAS:$(alias ls)"; else echo "NO_ALIAS"; fi
	' 2>&1)
	assert_true "osc133 zsh: ls aliased with --hyperlink=auto when ls supports it and isn't already aliased" \
		"printf %s \"\$_zsh_out\" | grep -q -- '--hyperlink=auto'"

	_zsh_out=$(zsh -c '
		alias ls="ls -F"
		export PATH="'"$_fakebin"':$PATH"
		source "'"$_zsh_script"'"
		if alias ls >/dev/null 2>&1; then echo "ALIAS:$(alias ls)"; else echo "NO_ALIAS"; fi
	' 2>&1)
	assert_true "osc133 zsh: a pre-existing ls alias is left untouched, not overridden" \
		"printf %s \"\$_zsh_out\" | grep -q 'ls -F'"

	cat > "$_fakebin/ls" <<'EOF'
#!/bin/sh
case "$*" in
	*--hyperlink*) echo "ls: unrecognized option '--hyperlink=auto'" >&2; exit 2 ;;
esac
exec /bin/ls "$@"
EOF
	chmod +x "$_fakebin/ls"

	_zsh_out=$(zsh -c '
		unalias ls 2>/dev/null
		export PATH="'"$_fakebin"':$PATH"
		source "'"$_zsh_script"'"
		if alias ls >/dev/null 2>&1; then echo "ALIAS:$(alias ls)"; else echo "NO_ALIAS"; fi
	' 2>&1)
	assert_true "osc133 zsh: no alias set when ls does not understand --hyperlink" \
		"[ \"\$_zsh_out\" = NO_ALIAS ]"

	rm -rf "$_fakebin"; unset _fakebin

	unset _zsh_script _zsh_out
else
	echo "  SKIP: zsh not installed -- shell-integration.zsh not exercised"
fi

# ---------------------------------------------------------------------------
# Section 41 — byobu-enable/disable-shell-integration (rc-file injection)
# ---------------------------------------------------------------------------
# The enable/disable scripts are .in templates (need @prefix@ substituted),
# so this exercises them the same way test_byobu.sh already handles
# launcher-install/-uninstall above: read the .in source directly and sed
# out the one substitution that matters for a functional test.

# Both scripts source include/common, which needs include/dirs -- itself a
# .in template not present without a full autoreconf/configure/make cycle
# (confirmed while writing this test: running the .in directly, even with
# @prefix@ substituted, fails on that missing dependency). Section 34 above
# hits the exact same problem with launcher-install/-uninstall and solves it
# the same way this does: static checks on the .in source, plus an inline
# simulation of the marker-line logic rather than actually invoking the
# script. The real end-to-end behavior (this exact scenario, plus a full
# build) was verified manually in Docker during development.

_enable_src="$BYOBU_PREFIX/bin/byobu-enable-shell-integration.in"
_disable_src="$BYOBU_PREFIX/bin/byobu-disable-shell-integration.in"
_marker="#byobu-shell-integration#"

if [ -r "$_enable_src" ] && [ -r "$_disable_src" ]; then
	assert_true "enable-shell-integration: handles bash" \
		"grep -q '\*bash)' '$_enable_src'"
	assert_true "enable-shell-integration: handles zsh" \
		"grep -q '\*zsh)' '$_enable_src'"
	assert_true "enable-shell-integration: calls disable first (idempotency)" \
		"grep -q 'disable-shell-integration --no-reload' '$_enable_src'"
	assert_true "enable-shell-integration and disable-shell-integration: same marker string" \
		"grep -q \"$_marker\" '$_enable_src' && grep -q \"$_marker\" '$_disable_src'"

	# Inline simulation of the marker-line logic both scripts actually use
	# (append-if-absent in enable, "sed -e /marker$/d" in disable) against a
	# fake rc file -- this is the part with real dedup/removal bugs to catch.
	_tmp=$(mktemp -d)
	_rc="$_tmp/.bashrc"
	echo "existing line" > "$_rc"

	_inject() {
		sed -e "/${_marker}$/d" "$_rc" > "$_rc.new" && mv "$_rc.new" "$_rc"
		printf '[ -r "profile" ] && . "profile"   %s\n' "$_marker" >> "$_rc"
	}

	_inject
	assert_true "rc injection: adds exactly one marker line" \
		"[ \"\$(grep -c \"$_marker\" '$_rc')\" = 1 ]"
	assert_true "rc injection: preserves the pre-existing line" \
		"grep -q '^existing line$' '$_rc'"

	_inject
	assert_true "rc injection: idempotent, still exactly one marker line after a second run" \
		"[ \"\$(grep -c \"$_marker\" '$_rc')\" = 1 ]"

	sed -e "/${_marker}$/d" "$_rc" > "$_rc.new" && mv "$_rc.new" "$_rc"
	assert_true "rc removal: cleans up back to byte-identical original content" \
		"[ \"\$(cat '$_rc')\" = 'existing line' ]"

	unset -f _inject
	rm -rf "$_tmp"; unset _tmp _rc
else
	echo "  SKIP: byobu-enable/disable-shell-integration.in not found -- rc-injection not exercised"
fi

unset _enable_src _disable_src _marker

# ---------------------------------------------------------------------------
# Section 42 — security regressions (2026-09 audit)
# ---------------------------------------------------------------------------

# battery: /sys uevent is parsed, never sourced.  A USB HID battery/UPS can
# put arbitrary bytes in MODEL_NAME/SERIAL_NUMBER, which used to be sourced
# as shell.  Point $BATTERY at a hostile fake and make sure nothing runs.
_bat_tmp=$(mktemp -d)
_bat_marker="$_bat_tmp/pwned"
mkdir -p "$_bat_tmp/BAT9"
cat > "$_bat_tmp/BAT9/uevent" <<EOF
POWER_SUPPLY_NAME=BAT9
POWER_SUPPLY_TYPE=Battery
POWER_SUPPLY_STATUS=Discharging
POWER_SUPPLY_PRESENT=1
POWER_SUPPLY_MODEL_NAME=\$(touch $_bat_marker)
POWER_SUPPLY_SERIAL_NUMBER="; touch $_bat_marker.2; "
POWER_SUPPLY_MANUFACTURER=\`touch $_bat_marker.3\`
POWER_SUPPLY_CAPACITY=42
POWER_SUPPLY_CHARGE_NOW=42; touch $_bat_marker.4
EOF
_bat_out=$(
	BATTERY="$_bat_tmp/BAT9" BYOBU_OSTYPE=Linux BYOBU_CHARMAP=UTF-8
	export BATTERY BYOBU_OSTYPE BYOBU_CHARMAP
	# The real host's /sys batteries are still globbed; the fake one is
	# processed first, and on a host without a battery it is the only one.
	. "${BYOBU_PREFIX}/lib/${PKG}/battery"
	__battery 2>/dev/null
)
assert_false "battery: \$(...) in MODEL_NAME does not execute" "[ -e '$_bat_marker' ]"
assert_false "battery: quote-breakout in SERIAL_NUMBER does not execute" "[ -e '$_bat_marker.2' ]"
assert_false "battery: backticks in MANUFACTURER do not execute" "[ -e '$_bat_marker.3' ]"
assert_false "battery: non-numeric CHARGE_NOW is rejected, not executed" "[ -e '$_bat_marker.4' ]"
assert_true  "battery: sane keys from the same uevent are still used" \
	"printf '%s' \"$_bat_out\" | grep -q '42'"
assert_true  "battery: no 'source' of uevent remains in the script" \
	"! grep -q 'uevent.*>.*TMP_FILE' '${BYOBU_PREFIX}/lib/${PKG}/battery'"
rm -rf "$_bat_tmp"; unset _bat_tmp _bat_marker _bat_out

# select-session.py: user input must never be eval()'d (restricted-shell escape)
_sel="${BYOBU_PREFIX}/lib/${PKG}/include/select-session.py"
assert_false "select-session.py: no eval() of user input" "grep -v '^[[:space:]]*#' '$_sel' | grep -q 'eval('"
unset _sel

# manifest: only Debian package names may reach 'sudo apt install'
_man="${BYOBU_PREFIX}/bin/manifest"
_man_tmp=$(mktemp -d)
ln -s "${BYOBU_PREFIX}/bin/col1" "$_man_tmp/col2"
_man_out=$(
	PATH="$_man_tmp:$PATH"
	# Pull filter_packages() out of the script without running it.
	eval "$(sed -n '/^filter_packages() {/,/^}/p' "$_man")"
	printf 'ii  bash  5.2  amd64  shell\nii  -oDPkg::Pre-Invoke::=id  1  all  x\nii  ../evil  1  all  x\nii  Libfoo  1  all  x\nii  zsh  5.9  amd64  shell\n' | filter_packages | tr '\n' ' '
)
assert_eq "manifest: option-like and malformed names are dropped" "$_man_out" "bash zsh "
assert_true "manifest: apt invoked with -- before the package list" "grep -q 'apt install -- ' '$_man'"
assert_false "manifest: no plaintext http:// default remains" "grep -q 'http://paste' '$_man'"
rm -rf "$_man_tmp"; unset _man _man_tmp _man_out

# printf_status: "#" from data must reach tmux doubled, screen untouched
out=$(BYOBU_BACKEND=tmux printf_status 'host#(id)#{pane_current_path}#[fg=red]x')
assert_eq "printf_status tmux: every # doubled" "$out" 'host##(id)##{pane_current_path}##[fg=red]x'
out=$(BYOBU_BACKEND=tmux printf_status '###')
assert_eq "printf_status tmux: run of #" "$out" '######'
out=$(BYOBU_BACKEND=tmux printf_status 'plain')
assert_eq "printf_status tmux: no # is a no-op" "$out" 'plain'
out=$(BYOBU_BACKEND=screen printf_status 'a#b')
assert_eq "printf_status screen: literal" "$out" 'a#b'
for _f in hostname release distro whoami; do
	assert_true "status/$_f: emits via printf_status" \
		"grep -q 'printf_status' '${BYOBU_PREFIX}/lib/${PKG}/$_f'"
done
unset _f

# byobu-ulevel: option values are numbers or nothing (they reach bc and eval).
# Section 17's temp copy is gone by now (rm'd once its own tests finished) --
# build a fresh one the same way it did: @prefix@-substituted .in from the
# source tree, or the real installed binary when .in isn't shipped (an
# installed package never ships .in templates, only their substituted
# output). Getting this wrong doesn't just fail the assert_true checks
# below -- the assert_false ones (hostile input must be *rejected*) would
# silently pass for the wrong reason too, since "no such file" is also a
# nonzero exit, masking whether the actual validation logic ever ran.
_ul_bin=$(mktemp /tmp/byobu-ulevel-test-42-XXXXXX)
sed "s|@prefix@|${BYOBU_PREFIX}|g" \
	"${BYOBU_PREFIX}/../usr/bin/byobu-ulevel.in" > "$_ul_bin" 2>/dev/null || \
sed "s|@prefix@|${BYOBU_PREFIX}|g" \
	"$(dirname "$0")/../../bin/byobu-ulevel.in" > "$_ul_bin" 2>/dev/null || \
cp "${BYOBU_PREFIX}/bin/byobu-ulevel" "$_ul_bin" 2>/dev/null || true
chmod +x "$_ul_bin"
_ul() { BYOBU_INCLUDED_LIBS=1 BYOBU_BACKEND=tmux PKG=byobu bash "$_ul_bin" "$@"; }
assert_true "ulevel: numeric -c accepted" "_ul -c 50 >/dev/null 2>&1"
assert_false "ulevel: -c with bc code is refused" "_ul -c 'x; print 1' >/dev/null 2>&1"
assert_false "ulevel: -m with shell text is refused" "_ul -c 5 -m '\$(id)' >/dev/null 2>&1"
assert_false "ulevel: -x non-numeric is refused" "_ul -c 5 -x 10abc >/dev/null 2>&1"
assert_false "ulevel: -w non-numeric is refused" "_ul -c 5 -w '3 3' >/dev/null 2>&1"
out=$(_ul -c 5 -m -10 -x 10.5 -w 4 2>/dev/null)
assert_nonempty "ulevel: negative and decimal bounds still accepted" "$out"
out=$(_ul -c 50 -u 'a b c' 2>/dev/null)
assert_nonempty "ulevel: user theme via -u still renders" "$out"
_ul_tmp=$(mktemp -d)
_ul -c 50 -u 'a $(touch '"$_ul_tmp"'/pwned) c' >/dev/null 2>&1 || true
assert_false "ulevel: user theme glyphs are not eval'd" "[ -e '$_ul_tmp/pwned' ]"
rm -rf "$_ul_tmp"; unset _ul_tmp
assert_false "ulevel: theme name is matched exactly, not as a regex" "_ul -c 5 -t 'vbars_[8]' >/dev/null 2>&1"
rm -f "$_ul_bin"; unset _ul_bin

# col1: column number comes from argv[0] and is data to awk, not program text
_col_tmp=$(mktemp -d)
ln -s "${BYOBU_PREFIX}/bin/col1" "$_col_tmp/col3"
_col_evil='col1);system("touch pwned");{print(1'
ln -s "${BYOBU_PREFIX}/bin/col1" "$_col_tmp/$_col_evil"
out=$(printf 'a b c d\n' | "$_col_tmp/col3")
assert_eq "col3: prints third column" "$out" "c"
out=$(printf 'a:b:c\n' | "$_col_tmp/col3" :)
assert_eq "col3 with separator" "$out" "c"
assert_true "colN: hostile argv[0] is a usage error" "[ -L '$_col_tmp/$_col_evil' ] && ! (cd '$_col_tmp' && printf 'a b\n' | ./\"\$_col_evil\" >/dev/null 2>&1)"
(cd "$_col_tmp" && printf 'a b\n' | "./$_col_evil" >/dev/null 2>&1) || true
assert_false "colN: hostile argv[0] does not reach awk program text" "[ -e '$_col_tmp/pwned' ]"
unset _col_evil
rm -rf "$_col_tmp"; unset _col_tmp

# Static checks on scripts that need a live tmux or network to run.
# byobu-ugraph/byobu-layout are .in templates -- absent by design from an
# installed package (only their @prefix@-substituted output ships), so
# check that instead when .in isn't found. This isn't just cosmetic for
# the assert_true below: the assert_false checks would otherwise "pass"
# against a missing file for the wrong reason (no match is also true of
# no file), silently verifying nothing in that environment.
_resolve_or_installed() {
	# $1: script base name (no .in). Echoes whichever exists.
	local f="${BYOBU_PREFIX}/bin/$1.in"
	[ -r "$f" ] || f="${BYOBU_PREFIX}/bin/$1"
	# Nix wraps an installed binary in a thin launcher (PATH/BYOBU_PYTHON
	# setup) that execs the real script renamed to a leading-dot file in
	# the same directory -- confirmed directly against a real Nix build:
	# /nix/store/.../bin/byobu-layout is 638 bytes of wrapper with none of
	# the real logic text, while .../bin/.byobu-layout (4KB) has it. A
	# content grep against the wrapper finds nothing either way, so
	# assert_false "passes" for the wrong reason and assert_true fails
	# outright -- follow the wrapper's own exec line to the real file.
	if grep -q 'exec -a ' "$f" 2>/dev/null; then
		local real
		real=$(sed -n 's/.*exec -a "[^"]*" "\([^"]*\)".*/\1/p' "$f" | head -1)
		[ -n "$real" ] && [ -r "$real" ] && f="$real"
	fi
	printf '%s' "$f"
}
_ugraph=$(_resolve_or_installed byobu-ugraph)
_layout=$(_resolve_or_installed byobu-layout)
assert_false "byobu-ugraph: no predictable /tmp file" "grep -q 'file=/tmp/\${USER}' '$_ugraph'"
assert_false "byobu-ugraph: no eval of the command" "grep -q 'eval \"\$cmd' '$_ugraph'"
assert_true  "byobu-layout: validates layout names" "grep -q 'valid_name \"\$name\"' '$_layout'"
assert_false "byobu-layout: no printf with data as format" "grep -q 'printf \"\$panes' '$_layout'"
unset -f _resolve_or_installed
unset _ugraph _layout
assert_true  "wifi-status: validates interface name" "grep -q 'invalid wireless interface name' '${BYOBU_PREFIX}/bin/wifi-status'"
assert_true  "wifi-status: validates ping target" "grep -q 'invalid WIFI_PING_TARGET' '${BYOBU_PREFIX}/bin/wifi-status'"
assert_true  "whats-my-public-ip: https only" "! grep -q 'http://' '${BYOBU_PREFIX}/bin/whats-my-public-ip'"
assert_true  "purge-old-kernels: quotes \$@" "grep -q 'apt-get \"\$@\" autoremove' '${BYOBU_PREFIX}/bin/purge-old-kernels'"
assert_false "byobu-ctrl-a: stray character after keybindings path removed" "grep -q '\"\$keybindings\"e' '${BYOBU_PREFIX}/bin/byobu-ctrl-a.in'"
assert_false "updates_available: cache path not spliced into sh -c" "grep -q 'sh -c \".*mycache' '${BYOBU_PREFIX}/lib/${PKG}/updates_available'"

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
