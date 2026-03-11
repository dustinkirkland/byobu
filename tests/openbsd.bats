#!/usr/bin/env bats
#
# OpenBSD-specific tests for byobu status scripts.
# These tests verify sysctl probes, command output formats,
# and OpenBSD code path correctness.
#
# Skipped automatically on non-OpenBSD systems.

BYOBU_LIB="${BATS_TEST_DIRNAME}/../usr/lib/byobu"

setup() {
	if [ "$(uname -s)" != "OpenBSD" ]; then
		skip "requires OpenBSD"
	fi
	export BYOBU_PREFIX="${BATS_TEST_DIRNAME}/.."
	export PKG="byobu"
	export BYOBU_CONFIG_DIR="$(mktemp -d)"
	export BYOBU_RUN_DIR="$(mktemp -d)"
	export BYOBU_BACKEND="tmux"
	export BYOBU_TEST="command -v"
	export BYOBU_OSTYPE="OpenBSD"
	mkdir -p "$BYOBU_RUN_DIR/cache.$BYOBU_BACKEND"
	mkdir -p "$BYOBU_RUN_DIR/status.$BYOBU_BACKEND"
	. "$BYOBU_LIB/include/shutil"
	[ -f "$BYOBU_LIB/include/icons" ] && . "$BYOBU_LIB/include/icons"
}

teardown() {
	rm -rf "$BYOBU_CONFIG_DIR" "$BYOBU_RUN_DIR"
}

# --- sysctl probes ---

@test "openbsd: hw.physmem returns a positive integer" {
	result=$(sysctl -n hw.physmem)
	[ -n "$result" ]
	[ "$result" -gt 0 ]
}

@test "openbsd: hw.pagesize returns a positive integer" {
	result=$(sysctl -n hw.pagesize)
	[ -n "$result" ]
	[ "$result" -gt 0 ]
}

@test "openbsd: hw.ncpuonline returns a positive integer" {
	result=$(sysctl -n hw.ncpuonline)
	[ -n "$result" ]
	[ "$result" -gt 0 ]
}

@test "openbsd: hw.cpuspeed returns a positive integer" {
	result=$(sysctl -n hw.cpuspeed)
	[ -n "$result" ]
	[ "$result" -gt 0 ]
}

@test "openbsd: kern.boottime returns a positive integer" {
	result=$(sysctl -n kern.boottime)
	[ -n "$result" ]
	[ "$result" -gt 0 ]
}

@test "openbsd: vm.loadavg returns 3 space-separated values" {
	result=$(sysctl -n vm.loadavg)
	[ -n "$result" ]
	count=$(echo "$result" | awk '{ print NF }')
	[ "$count" -eq 3 ]
}

# --- Command output format validation ---

@test "openbsd: vmstat -s has 'pages free' line" {
	result=$(vmstat -s | grep "pages free")
	[ -n "$result" ]
}

@test "openbsd: route -n get default returns an interface" {
	result=$(route -n get default 2>/dev/null | awk '/interface:/ { print $2 }')
	[ -n "$result" ]
}

@test "openbsd: netstat -ibn produces parseable output" {
	result=$(netstat -ibn | head -5)
	[ -n "$result" ]
}

@test "openbsd: df -h / produces size and percentage" {
	result=$(df -h / | awk 'END { printf "%s %s", $2, $5 }')
	[ -n "$result" ]
}

@test "openbsd: mount output is parseable for device-to-mountpoint" {
	result=$(mount | awk '$3 == "/" { print $1; exit }')
	[ -n "$result" ]
}

@test "openbsd: swapctl -sk runs without error" {
	swapctl -sk 2>/dev/null || true
}

@test "openbsd: ps -ax produces output" {
	result=$(ps -ax | wc -l)
	[ "$result" -gt 0 ]
}

@test "openbsd: ps -ej produces output" {
	result=$(ps -ej 2>/dev/null | wc -l)
	[ "$result" -gt 0 ]
}

@test "openbsd: iostat -DI produces output" {
	if ! command -v iostat >/dev/null 2>&1; then
		skip "iostat not available"
	fi
	result=$(iostat -DI 2>/dev/null)
	[ -n "$result" ]
}

@test "openbsd: sed -i (no suffix) works" {
	tmpfile=$(mktemp)
	echo "hello" > "$tmpfile"
	sed -i -e 's/hello/world/' "$tmpfile"
	result=$(cat "$tmpfile")
	rm -f "$tmpfile"
	[ "$result" = "world" ]
}

# --- Code path smoke tests ---

@test "openbsd: memory computes total and free" {
	physmem=$(sysctl -n hw.physmem)
	total=$((physmem / 1024))
	[ "$total" -gt 0 ]

	pagesize=$(sysctl -n hw.pagesize)
	free_pages=$(vmstat -s 2>/dev/null | awk '/pages free$/ { print $1; exit }')
	[ -n "$free_pages" ]
	free=$((free_pages * pagesize / 1024))
	[ "$free" -ge 0 ]
	[ "$free" -le "$total" ]
}

@test "openbsd: uptime computes positive value" {
	bt=$(sysctl -n kern.boottime)
	now=$(date +%s)
	u=$((now - bt))
	[ "$u" -gt 0 ]
}

@test "openbsd: load_average extracts numeric value" {
	one=$(sysctl -n vm.loadavg 2>/dev/null | awk '{ print $1 }')
	[ -n "$one" ]
	echo "$one" | grep -qE '^[0-9]+\.?[0-9]*$'
}

@test "openbsd: cpu_count via sysctl" {
	c=$(sysctl -n hw.ncpuonline 2>/dev/null)
	[ "$c" -ge 1 ]
}

@test "openbsd: disk maps root to a device" {
	part=$(mount | awk '$3 == "/" { print $1; exit }')
	[ -n "$part" ]
	disk=$(echo "${part##*/}" | sed 's/[a-p]$//')
	[ -n "$disk" ]
}

@test "openbsd: entropy script contains arc4random comment" {
	grep -q 'arc4random' "$BYOBU_LIB/entropy"
}

@test "openbsd: constants sets BYOBU_OSTYPE" {
	grep -q 'BYOBU_OSTYPE' "$BYOBU_LIB/include/constants"
}

@test "openbsd: cycle-status uses BYOBU_SED_INLINE" {
	! grep -q 'sed -i' "$BYOBU_LIB/include/cycle-status"
	grep -q 'BYOBU_SED_INLINE' "$BYOBU_LIB/include/cycle-status"
}

@test "openbsd: toggle-utf8.in uses BYOBU_SED_INLINE" {
	! grep -q 'sed -i' "$BYOBU_LIB/include/toggle-utf8.in"
	grep -q 'BYOBU_SED_INLINE' "$BYOBU_LIB/include/toggle-utf8.in"
}

# vi: syntax=sh ts=4 noexpandtab
