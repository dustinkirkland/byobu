#!/bin/sh
#
#    run.sh: browser end-to-end tests for the trustmux PWA, in Docker
#
#    Usage: mobile/tests/e2e/run.sh [SCENARIO...]
#
#    Runs everything in a throwaway container (never on the host): a real
#    Chromium pairs with a real trustmux daemon and taps the UI, and each
#    scenario checks the exact bytes a tmux pane received.  With no
#    arguments every scenario runs; otherwise only those whose names contain
#    one of the arguments.  Exits non-zero if any scenario fails.
#
#    REPEAT=N runs each scenario N times (flaky behavior), and E2E_CPUS=N
#    gives the container only N CPUs (a busy host).  Any change to these
#    tests must pass both of these with no failures (slow and fast hosts
#    break different timing assumptions):
#        E2E_CPUS=0.5 REPEAT=10 mobile/tests/e2e/run.sh
#        REPEAT=10 mobile/tests/e2e/run.sh
#    and TMUX_STALL=1 stops tmux for a second or two now and then, as a
#    release build's load does (see tests/stall.py):
#        TMUX_STALL=1 REPEAT=10 mobile/tests/e2e/run.sh

set -e
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)

# The image only depends on the Dockerfile, so tag it by its hash and
# rebuild only when it changes.
tag="trustmux-e2e:$(sha256sum "$here/Dockerfile" | cut -c1-12)"
if ! docker image inspect "$tag" >/dev/null 2>&1; then
	echo "Building $tag (first run only)..."
	docker build -q -t "$tag" "$here" >/dev/null
fi

exec docker run --rm ${E2E_CPUS:+--cpus "$E2E_CPUS"} -e REPEAT="${REPEAT:-1}" -e TMUX_STALL -e TMUX_STALL_SEED -v "$repo:/src:ro" "$tag" sh /src/mobile/tests/e2e/in_container.sh "$@"
