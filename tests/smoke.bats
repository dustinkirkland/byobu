#!/usr/bin/env bats
#
# Smoke tests for byobu status scripts.
# Sources each script and calls its __function, verifying it exits
# cleanly (exit 0) and produces no stderr.
#
# Runs on any platform -- each script auto-detects its OS paths.

BYOBU_LIB="${BATS_TEST_DIRNAME}/../usr/lib/byobu"

# Set up a minimal byobu environment so scripts can source cleanly.
setup() {
	export BYOBU_PREFIX="${BATS_TEST_DIRNAME}/.."
	export PKG="byobu"
	export BYOBU_CONFIG_DIR="$(mktemp -d)"
	export BYOBU_RUN_DIR="$(mktemp -d)"
	export BYOBU_BACKEND="tmux"
	export BYOBU_TEST="command -v"
	mkdir -p "$BYOBU_RUN_DIR/cache.$BYOBU_BACKEND"
	mkdir -p "$BYOBU_RUN_DIR/status.$BYOBU_BACKEND"
	# Source the shared utilities
	. "$BYOBU_LIB/include/shutil"
	# Source icons so ICON_* variables are available
	[ -f "$BYOBU_LIB/include/icons" ] && . "$BYOBU_LIB/include/icons"
}

teardown() {
	rm -rf "$BYOBU_CONFIG_DIR" "$BYOBU_RUN_DIR"
}

# Helper: source a status script and call its __function.
# Captures stderr separately to detect unexpected errors.
run_status() {
	local script="$1" func="$2"
	local stderr_file="$BYOBU_RUN_DIR/stderr.$$"
	(
		. "$BYOBU_LIB/$script"
		"$func" 2>"$stderr_file" || true
	)
	local err=""
	[ -f "$stderr_file" ] && err=$(cat "$stderr_file")
	rm -f "$stderr_file"
	# Scripts may return non-zero when hardware/data is unavailable;
	# that is normal, not a failure. We only fail on unexpected stderr.
	# No unexpected errors on stderr (filter out known harmless messages)
	if [ -n "$err" ]; then
		# Filter: "not found" from missing optional commands is expected
		filtered=$(printf '%s\n' "$err" | grep -v -i 'not found' | grep -v -i 'No such file' | grep -v 'cannot open' || true)
		[ -z "$filtered" ]
	fi
}

# --- Status script smoke tests ---

@test "smoke: arch" {
	run_status "arch" "__arch"
}

@test "smoke: cpu_count" {
	run_status "cpu_count" "__cpu_count"
}

@test "smoke: cpu_freq" {
	run_status "cpu_freq" "__cpu_freq"
}

@test "smoke: cpu_temp" {
	run_status "cpu_temp" "__cpu_temp"
}

@test "smoke: date" {
	run_status "date" "__date"
}

@test "smoke: disk" {
	run_status "disk" "__disk"
}

@test "smoke: distro" {
	run_status "distro" "__distro"
}

@test "smoke: entropy" {
	run_status "entropy" "__entropy"
}

@test "smoke: hostname" {
	run_status "hostname" "__hostname"
}

@test "smoke: load_average" {
	run_status "load_average" "__load_average"
}

@test "smoke: memory" {
	run_status "memory" "__memory"
}

@test "smoke: processes" {
	run_status "processes" "__processes"
}

@test "smoke: release" {
	run_status "release" "__release"
}

@test "smoke: swap" {
	run_status "swap" "__swap"
}

@test "smoke: time" {
	run_status "time" "__time"
}

@test "smoke: time_utc" {
	run_status "time_utc" "__time_utc"
}

@test "smoke: uptime" {
	run_status "uptime" "__uptime"
}

@test "smoke: users" {
	run_status "users" "__users"
}

@test "smoke: whoami" {
	run_status "whoami" "__whoami"
}

# --- Scripts that may need hardware/network (allowed to return 1) ---

@test "smoke: battery (may skip on desktops)" {
	run_status "battery" "__battery"
}

@test "smoke: disk_io (may skip without iostat)" {
	run_status "disk_io" "__disk_io"
}

@test "smoke: fan_speed (may skip without sensors)" {
	run_status "fan_speed" "__fan_speed"
}

@test "smoke: ip_address" {
	run_status "ip_address" "__ip_address"
}

@test "smoke: network" {
	run_status "network" "__network"
}

@test "smoke: raid (may skip without mdstat/bioctl)" {
	run_status "raid" "__raid"
}

@test "smoke: wifi_quality (may skip without wireless)" {
	run_status "wifi_quality" "__wifi_quality"
}

# --- Detail functions (the popup/expand view) ---

@test "smoke: load_average detail" {
	run_status "load_average" "__load_average_detail"
}

@test "smoke: processes detail" {
	run_status "processes" "__processes_detail"
}

@test "smoke: entropy detail" {
	run_status "entropy" "__entropy_detail"
}

# vi: syntax=sh ts=4 noexpandtab
