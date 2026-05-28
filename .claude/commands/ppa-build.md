---
description: Build a signed source package and upload it to ppa:byobu/ppa for candidate testing
---

Build an unsigned source package in Docker, then tell the user the exact commands to sign and upload it to `ppa:byobu/ppa`. Signing requires the user's interactive GPG passphrase; Claude cannot do that step.

## Pre-flight checks (do these first, in parallel)

1. Confirm `DEBEMAIL` and `GPGKEY` are set by sourcing `~/.bashrc`:
   ```bash
   source ~/.bashrc && echo "DEBEMAIL=$DEBEMAIL" && echo "GPGKEY=$GPGKEY"
   ```
   If either is empty, tell the user and stop — the build would be signed by the wrong key.

2. Check that `dput` and `devscripts` (provides `debsign`) are installed:
   ```bash
   which dput debsign 2>&1
   ```
   If missing, tell the user to run: `sudo apt install devscripts dput`

3. Read the current version and package name from `debian/changelog`:
   ```bash
   head -1 /home/kirkland/src/byobu/debian/changelog
   ```
   The line looks like: `byobu (7.0) UNRELEASED; urgency=medium`
   Extract: PKG=byobu, BASE_VER=7.0

4. Determine the next PPA iteration by scanning the changelog for existing `~ppaN` entries:
   ```bash
   grep -oP '\(.*~ppa\K[0-9]+(?=~|\))' /home/kirkland/src/byobu/debian/changelog | sort -n | tail -1
   ```
   If nothing found, ITER=1. Otherwise ITER=last+1.

## Build the source package (unsigned, in Docker)

Compute: `PPA_VER="${BASE_VER}~ppa${ITER}~noble1"`

- The `~noble1` suffix allows rebuilding for multiple Ubuntu series without version collision.
- The double `~` means `7.0~ppa1~noble1 < 7.0~ppa1 < 7.0` in dpkg ordering, so official releases always supersede.

Create an output directory, then run Docker:

```bash
OUTDIR=/tmp/byobu-ppa
mkdir -p $OUTDIR

docker run --rm \
  -v /home/kirkland/src/byobu:/src:ro \
  -v $OUTDIR:/out \
  -e PKG=byobu \
  -e BASE_VER=7.0 \
  -e PPA_VER=7.0~ppa1~noble1 \
  -e DEBEMAIL=kirkland@ubuntu.com \
  ubuntu:noble \
  bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends \
      build-essential dpkg-dev debhelper dh-python \
      gettext-base automake autoconf \
      python3 python3-all python3-tornado \
      devscripts bc ca-certificates 2>&1 | tail -5

    # Copy source into a working tree named PKG-PPA_VER (required by dpkg-source)
    WORKDIR=$(mktemp -d)
    cp -a /src "$WORKDIR/${PKG}-${PPA_VER}"
    cd "$WORKDIR/${PKG}-${PPA_VER}"

    # Stamp the changelog: set version, series (noble), and maintainer
    DATESTAMP=$(date -R)
    {
      echo "${PKG} (${PPA_VER}) noble; urgency=medium"
      echo ""
      echo "  * PPA candidate build ${PPA_VER}"
      echo ""
      echo " -- ${DEBEMAIL}  ${DATESTAMP}"
      echo ""
      cat debian/changelog
    } > debian/changelog.new
    mv debian/changelog.new debian/changelog

    # Switch source format to native (version has no - separator)
    echo "3.0 (native)" > debian/source/format

    # Build source package — unsigned (-us -uc), source only (-S)
    # -d skips build-dep check (we installed them above but dpkg may not see them all)
    dpkg-buildpackage -S -us -uc -d 2>&1

    # Collect artifacts
    cp -v "$WORKDIR"/*.changes "$WORKDIR"/*.dsc "$WORKDIR"/*.tar.* /out/ 2>/dev/null || true
    ls -lh /out/
    echo "=== unsigned source package built ==="
  '
```

## Tell the user to sign and upload interactively

After the Docker build completes, print these instructions clearly:

```
Source package built in /tmp/byobu-ppa/. Now sign and upload:

  1. Sign (you will be prompted for your GPG passphrase):
     ! debsign -k $GPGKEY /tmp/byobu-ppa/*.changes

  2. Upload to ppa:byobu/ppa:
     ! dput ppa:byobu/ppa /tmp/byobu-ppa/*.changes

  3. Monitor the build at:
     https://launchpad.net/~byobu/+archive/ubuntu/ppa
```

Tell the user they can prefix the commands with `!` to run them directly in the Claude Code session, which will let GPG prompt for their passphrase.

## Notes

- Do NOT attempt to run `debsign` or `dput` yourself — signing requires interactive GPG passphrase input.
- If Docker build fails, show the last 30 lines of output to help diagnose.
- If `DEBEMAIL` or `GPGKEY` are empty after sourcing `~/.bashrc`, stop and ask the user to set them.
- The `~noble1` suffix allows building the same `~ppaN` for other series (jammy, etc.) without version conflict; omit or adjust if the user targets a different series.
- PPA packages are source-only uploads — Launchpad builds the binary .debs from your source.
