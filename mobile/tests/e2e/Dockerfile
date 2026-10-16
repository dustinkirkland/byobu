# Browser end-to-end tests for the trustmux PWA: a real Chromium (phone-sized,
# touch) pairs with a real trustmux daemon and drives a real tmux pane.
# Built and run by run.sh; trustmux itself is installed from the mounted
# source tree at run time, so this image only changes with this file.
FROM debian:trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium ca-certificates fonts-dejavu-core openssl procps \
      python3 python3-pip python3-venv tmux \
 && rm -rf /var/lib/apt/lists/*

# Debian's chromium, driven by a pinned Playwright (its own browser download
# doesn't support trixie).
RUN python3 -m venv /opt/pw && /opt/pw/bin/pip install --no-cache-dir playwright==1.48.0

RUN useradd -m -s /bin/bash tester
