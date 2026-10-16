#!/bin/sh
# Runs inside the trustmux-e2e container (see run.sh).  /src is the byobu
# checkout, read-only.
set -e

# Install trustmux from a clean copy of the source: a stale mobile/build/
# from an earlier local build would otherwise be installed instead.
mkdir /tmp/tm
(cd /src/mobile && tar --exclude=./build --exclude=./dist --exclude='*.egg-info' \
	--exclude=__pycache__ -cf - .) | tar -xf - -C /tmp/tm
pip install -q --break-system-packages /tmp/tm >/dev/null 2>&1 || {
	echo "pip install of trustmux failed:" >&2
	pip install --break-system-packages /tmp/tm >&2
	exit 1
}

# Everything else runs as an ordinary user, like on a real machine.
cd /home/tester
exec env HOME=/home/tester USER=tester LOGNAME=tester \
	runuser -u tester -- /opt/pw/bin/python /src/mobile/tests/e2e/e2e.py "$@"
