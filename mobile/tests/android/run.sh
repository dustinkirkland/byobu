#!/bin/sh
#
#    run.sh: real-Android tests for the trustmux PWA, in Docker
#
#    Usage: mobile/tests/android/run.sh [SCENARIO...]
#
#    An x86_64 Android emulator (needs /dev/kvm) runs real Chrome against a
#    real trustmux daemon and tmux, all inside a throwaway container (never
#    on the host). Touches go in through adb, so Chrome's own text selection
#    handles, long press and gestures are exercised -- what headless
#    Chromium (tests/e2e) can't do. With no arguments every scenario runs;
#    otherwise only those whose names contain one of the arguments. Set
#    ANDROID_SHOTS=DIR to keep a screenshot per scenario, REPEAT=N to run
#    each N times (flaky behavior). Exits non-zero if any scenario fails.

set -e
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)

if [ ! -w /dev/kvm ] && [ ! -r /dev/kvm ]; then
	echo "/dev/kvm isn't available; the Android emulator needs it." >&2
	exit 1
fi

# The image (SDK, emulator, system image: a few GB) only depends on the
# Dockerfile, so tag it by its hash and rebuild only when it changes.
tag="trustmux-android:$(sha256sum "$here/Dockerfile" | cut -c1-12)"
if ! docker image inspect "$tag" >/dev/null 2>&1; then
	echo "Building $tag (first run only, downloads the Android SDK)..."
	docker build -q -t "$tag" "$here" >/dev/null
fi

shots=
if [ -n "$ANDROID_SHOTS" ]; then
	mkdir -p "$ANDROID_SHOTS"
	shots="-v $(cd "$ANDROID_SHOTS" && pwd):/shots"
fi
# shellcheck disable=SC2086
exec docker run --rm --device /dev/kvm -v "$repo:/src:ro" -e REPEAT="${REPEAT:-1}" $shots "$tag" \
	sh /src/mobile/tests/android/in_container.sh "$@"
