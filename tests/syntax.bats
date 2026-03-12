#!/usr/bin/env bats
#
# Syntax validation for all byobu status scripts and includes.
# Platform-independent -- runs on any system with bash and bats.

BYOBU_LIB="${BATS_TEST_DIRNAME}/../usr/lib/byobu"
BYOBU_INCLUDE="${BYOBU_LIB}/include"

# --- Status scripts ---

@test "syntax: apport" {
	bash -n "$BYOBU_LIB/apport"
}

@test "syntax: arch" {
	bash -n "$BYOBU_LIB/arch"
}

@test "syntax: battery" {
	bash -n "$BYOBU_LIB/battery"
}

@test "syntax: color" {
	bash -n "$BYOBU_LIB/color"
}

@test "syntax: cpu_count" {
	bash -n "$BYOBU_LIB/cpu_count"
}

@test "syntax: cpu_freq" {
	bash -n "$BYOBU_LIB/cpu_freq"
}

@test "syntax: cpu_temp" {
	bash -n "$BYOBU_LIB/cpu_temp"
}

@test "syntax: custom" {
	bash -n "$BYOBU_LIB/custom"
}

@test "syntax: date" {
	bash -n "$BYOBU_LIB/date"
}

@test "syntax: disk" {
	bash -n "$BYOBU_LIB/disk"
}

@test "syntax: disk_io" {
	bash -n "$BYOBU_LIB/disk_io"
}

@test "syntax: distro" {
	bash -n "$BYOBU_LIB/distro"
}

@test "syntax: entropy" {
	bash -n "$BYOBU_LIB/entropy"
}

@test "syntax: fan_speed" {
	bash -n "$BYOBU_LIB/fan_speed"
}

@test "syntax: hostname" {
	bash -n "$BYOBU_LIB/hostname"
}

@test "syntax: ip_address" {
	bash -n "$BYOBU_LIB/ip_address"
}

@test "syntax: load_average" {
	bash -n "$BYOBU_LIB/load_average"
}

@test "syntax: logo" {
	bash -n "$BYOBU_LIB/logo"
}

@test "syntax: mail" {
	bash -n "$BYOBU_LIB/mail"
}

@test "syntax: memory" {
	bash -n "$BYOBU_LIB/memory"
}

@test "syntax: menu" {
	bash -n "$BYOBU_LIB/menu"
}

@test "syntax: network" {
	bash -n "$BYOBU_LIB/network"
}

@test "syntax: notify_osd" {
	bash -n "$BYOBU_LIB/notify_osd"
}

@test "syntax: packages" {
	bash -n "$BYOBU_LIB/packages"
}

@test "syntax: processes" {
	bash -n "$BYOBU_LIB/processes"
}

@test "syntax: raid" {
	bash -n "$BYOBU_LIB/raid"
}

@test "syntax: reboot_required" {
	bash -n "$BYOBU_LIB/reboot_required"
}

@test "syntax: release" {
	bash -n "$BYOBU_LIB/release"
}

@test "syntax: services" {
	bash -n "$BYOBU_LIB/services"
}

@test "syntax: session" {
	bash -n "$BYOBU_LIB/session"
}

@test "syntax: swap" {
	bash -n "$BYOBU_LIB/swap"
}

@test "syntax: time" {
	bash -n "$BYOBU_LIB/time"
}

@test "syntax: time_binary" {
	bash -n "$BYOBU_LIB/time_binary"
}

@test "syntax: time_utc" {
	bash -n "$BYOBU_LIB/time_utc"
}

@test "syntax: trash" {
	bash -n "$BYOBU_LIB/trash"
}

@test "syntax: updates_available" {
	bash -n "$BYOBU_LIB/updates_available"
}

@test "syntax: uptime" {
	bash -n "$BYOBU_LIB/uptime"
}

@test "syntax: users" {
	bash -n "$BYOBU_LIB/users"
}

@test "syntax: whoami" {
	bash -n "$BYOBU_LIB/whoami"
}

@test "syntax: wifi_quality" {
	bash -n "$BYOBU_LIB/wifi_quality"
}

# --- Includes ---

@test "syntax: include/constants" {
	bash -n "$BYOBU_INCLUDE/constants"
}

@test "syntax: include/shutil" {
	bash -n "$BYOBU_INCLUDE/shutil"
}

@test "syntax: include/cycle-status" {
	bash -n "$BYOBU_INCLUDE/cycle-status"
}

@test "syntax: include/toggle-utf8.in" {
	bash -n "$BYOBU_INCLUDE/toggle-utf8.in"
}

@test "syntax: include/colors" {
	bash -n "$BYOBU_INCLUDE/colors"
}

@test "syntax: include/icons" {
	bash -n "$BYOBU_INCLUDE/icons"
}

# vi: syntax=sh ts=4 noexpandtab
