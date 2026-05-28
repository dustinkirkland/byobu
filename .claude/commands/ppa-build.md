---
description: Build candidate source packages for all supported Ubuntu series and upload to ppa:byobu/ppa
---

Build an unsigned source package for every currently-supported Ubuntu series in Docker, then tell the user the exact commands to sign and upload each one. Signing requires the user's interactive GPG passphrase; Claude cannot do that step.

## Pre-flight checks (do these first, in parallel)

1. Extract `DEBEMAIL` and `GPGKEY` directly from `~/.bashrc` (do NOT `source ~/.bashrc` —
   it has a `[ -z "$PS1" ] && return` guard that exits early in non-interactive shells):
   ```bash
   DEBEMAIL=$(grep -oP 'DEBEMAIL=\K\S+' ~/.bashrc | tail -1 | tr -d '"'"'")
   GPGKEY=$(grep -oP 'GPGKEY=\K\S+' ~/.bashrc | tail -1 | tr -d '"'"'")
   echo "DEBEMAIL=$DEBEMAIL"
   echo "GPGKEY=$GPGKEY"
   ```
   If either is empty, stop — ask the user to add them to `~/.bashrc`:
   ```bash
   export DEBEMAIL="kirkland@ubuntu.com"
   export GPGKEY="E2D9E1C5F9F5D59291F4607D95E64373F1529469"
   ```

2. Check that `dput` and `devscripts` (provides `debsign`) are installed:
   ```bash
   which dput debsign 2>&1
   ```
   If missing: `sudo apt install devscripts dput`

3. Get the canonical list of currently-supported Ubuntu series from the installed distro-info tool:
   ```bash
   ubuntu-distro-info --supported 2>/dev/null || python3 -c "
   import urllib.request, json
   url = 'https://api.launchpad.net/1.0/ubuntu/series'
   d = json.loads(urllib.request.urlopen(url).read())
   active = {'Active Development','Current Stable Release','Supported'}
   # Only standard-support series (not ESM), i.e. version >= 22.04
   series = [e['name'] for e in d['entries']
             if e['status'] in active and float(e.get('version','0')) >= 22.04]
   print('\n'.join(series))
   "
   ```
   This gives the codenames: e.g. `jammy noble questing resolute stonking`.
   Store as a space-separated list: `SERIES="jammy noble questing resolute stonking"`

4. Read the current package version from `debian/changelog`:
   ```bash
   head -1 /home/kirkland/src/byobu/debian/changelog
   ```
   Extract `PKG` (e.g. `byobu`) and `BASE_VER` (e.g. `7.0`).

5. Determine the next PPA iteration by scanning `debian/changelog` for existing `~ppaN` entries:
   ```bash
   grep -oP '\(.*~ppa\K[0-9]+(?=~|\))' /home/kirkland/src/byobu/debian/changelog | sort -n | tail -1
   ```
   If nothing found, `ITER=1`. Otherwise `ITER=last+1`. This auto-increments so repeated
   `/ppa-build` runs produce `~ppa1`, `~ppa2`, etc. without manual bookkeeping.

## Version scheme

For each series, the version is: `${BASE_VER}~ppa${ITER}~${SERIES}1`

Examples for BASE_VER=7.0, ITER=1:
- `7.0~ppa1~jammy1`
- `7.0~ppa1~noble1`
- `7.0~ppa1~questing1`
- `7.0~ppa1~resolute1`
- `7.0~ppa1~stonking1`

The double `~` gives strict ordering: `7.0~ppa1~noble1 < 7.0~ppa1 < 7.0`, so any official Ubuntu or higher PPA upload automatically supersedes the candidate. The series suffix (`~noble1`, `~jammy1`, …) prevents version collisions between series and allows independent rebuilds per series.

## Build the source packages (unsigned, in Docker)

Run a single Docker container that loops through all series and produces one `.changes` + `.dsc` pair per series. All series share the same source tarball.

```bash
OUTDIR=/tmp/byobu-ppa
rm -rf $OUTDIR && mkdir -p $OUTDIR

docker run --rm \
  -v /home/kirkland/src/byobu:/src:ro \
  -v $OUTDIR:/out \
  ubuntu:noble \
  bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive DEBSIGN_KEYID="" GPGKEY=""

    apt-get update -qq
    apt-get install -y --no-install-recommends \
      build-essential dpkg-dev debhelper dh-python \
      gettext-base automake autoconf \
      python3 python3-all python3-tornado \
      devscripts bc ca-certificates distro-info 2>&1 | tail -5

    PKG=byobu
    BASE_VER=7.0
    ITER=1
    DEBEMAIL=kirkland@ubuntu.com
    # SERIES is passed in from the host substitution below

    SERIES=$(ubuntu-distro-info --supported | tr "\n" " ")
    echo "Building for series: $SERIES"

    # Create one orig tarball shared across all series
    STAGING=$(mktemp -d)
    cp -a /src "$STAGING/src"

    for CODENAME in $SERIES; do
      PPA_VER="${BASE_VER}~ppa${ITER}~${CODENAME}1"
      echo ""
      echo "=== Building $PPA_VER for $CODENAME ==="

      BUILDDIR=$(mktemp -d)
      cp -a "$STAGING/src" "$BUILDDIR/${PKG}-${PPA_VER}"
      cd "$BUILDDIR/${PKG}-${PPA_VER}"

      # Switch to native source format (version has no - separator)
      echo "3.0 (native)" > debian/source/format

      # Stamp changelog: new entry at top for this PPA version + series
      DATESTAMP=$(date -R)
      {
        printf "%s (%s) %s; urgency=medium\n\n" "$PKG" "$PPA_VER" "$CODENAME"
        printf "  * PPA candidate build %s\n\n" "$PPA_VER"
        printf " -- %s  %s\n\n" "$DEBEMAIL" "$DATESTAMP"
        cat debian/changelog
      } > debian/changelog.new
      mv debian/changelog.new debian/changelog

      # Build source package — unsigned (-us -uc), source only (-S)
      dpkg-buildpackage -S -us -uc -d 2>&1

      # Collect this series output
      cp -v "$BUILDDIR"/*.changes "$BUILDDIR"/*.dsc "$BUILDDIR"/*.tar.* "$BUILDDIR"/*.buildinfo /out/ 2>/dev/null || true

      cd /
      rm -rf "$BUILDDIR"
    done

    rm -rf "$STAGING"
    echo ""
    echo "=== All series built ==="
    ls -lh /out/
  '
```

## Tell the user to sign and upload interactively

After Docker completes, print this block clearly so the user can copy-paste or use `!` to run in-session:

```
Unsigned source packages built in /tmp/byobu-ppa/

Step 1 — Sign all .changes files (you will be prompted for your GPG passphrase once per file):

  for f in /tmp/byobu-ppa/*.changes; do
    ! debsign -k $GPGKEY "$f"
  done

Step 2 — Upload each signed .changes to ppa:byobu/ppa:

  for f in /tmp/byobu-ppa/*.changes; do
    ! dput ppa:byobu/ppa "$f"
  done

Step 3 — Monitor the Launchpad build queue:
  https://launchpad.net/~byobu/+archive/ubuntu/ppa
```

Tell the user they can run individual lines with the `!` prefix to get interactive GPG prompts directly in the Claude Code session. Or run without `!` in a separate terminal.

## Notes

- Do NOT attempt to run `debsign` or `dput` — both need interactive GPG or terminal.
- Launchpad requires one `.changes` upload per series. Each upload triggers a separate LP build.
- If a series is still in "Active Development" (e.g. stonking), the PPA build may fail if Launchpad does not yet have the build toolchain for it — that's fine, skip it.
- The source tarball is technically rebuilt per series in this script (simpler). Launchpad deduplicates if checksums match.
- If a later `/ppa-build` run is needed, the `ITER` auto-increment means no manual editing.
- To target only LTS series (no interim releases), use:
  ```bash
  comm -12 \
    <(ubuntu-distro-info --supported | sort) \
    <(paste <(ubuntu-distro-info --all) <(ubuntu-distro-info --all --fullname) \
      | awk '/LTS/{print $1}' | sort)
  ```
  On today's (2026-05-27) data that gives: `jammy noble resolute`
