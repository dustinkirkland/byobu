#!/bin/sh
# Runs inside the trustmux-android container (see run.sh).  /src is the
# byobu checkout, read-only.
set -e

# Install trustmux from a clean copy of the source (as in tests/e2e).
mkdir /tmp/tm
(cd /src/mobile && tar --exclude=./build --exclude=./dist --exclude='*.egg-info' \
	--exclude=__pycache__ -cf - .) | tar -xf - -C /tmp/tm
pip install -q --break-system-packages /tmp/tm >/dev/null 2>&1 || {
	echo "pip install of trustmux failed:" >&2
	pip install --break-system-packages /tmp/tm >&2
	exit 1
}

# Boot the emulator (cold, no snapshot: every run starts from the same
# state) and wait for Android to finish booting.
emulator -avd phone -no-window -no-audio -no-boot-anim -no-snapshot \
	-gpu swiftshader_indirect -accel on >/tmp/emulator.log 2>&1 &
adb wait-for-device
t=0
until [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; do
	sleep 2; t=$((t + 2))
	if [ $t -gt 300 ]; then
		echo "emulator didn't boot in 300s:" >&2; tail -20 /tmp/emulator.log >&2; exit 1
	fi
done
echo "Android booted in ${t}s; Chrome $(adb shell dumpsys package com.android.chrome \
	| sed -n 's/.*versionName=//p' | head -1 | tr -d '\r')"

# Chrome: no first-run screens, accept the daemon's self-signed cert, no
# notification prompt. The command-line file is read because Chrome is the
# debug app (the google_apis image allows adb root, unlike Play images).
adb root >/dev/null; sleep 2; adb wait-for-device
echo "_ --disable-fre --no-default-browser-check --no-first-run --ignore-certificate-errors" \
	>/tmp/chrome-command-line
adb push /tmp/chrome-command-line /data/local/tmp/chrome-command-line >/dev/null
adb shell chmod 644 /data/local/tmp/chrome-command-line
adb shell am set-debug-app --persistent com.android.chrome
adb shell pm grant com.android.chrome android.permission.POST_NOTIFICATIONS

# Everything else runs as an ordinary user, like on a real machine; adb
# reaches the server root started over its TCP port.
useradd -m -s /bin/bash tester
mkdir -p /shots && chmod 777 /shots
cd /home/tester
exec env HOME=/home/tester USER=tester LOGNAME=tester SHOTS=/shots REPEAT="${REPEAT:-1}" \
	runuser -u tester -- python3 /src/mobile/tests/android/android.py "$@"
