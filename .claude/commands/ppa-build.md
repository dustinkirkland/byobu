---
description: Build candidate source packages for all supported Ubuntu series and upload to ppa:byobu/ppa
---

Build an unsigned source package for every currently-supported Ubuntu series in Docker, then tell the user the exact commands to sign and upload each one. Signing requires the user's interactive GPG passphrase; Claude cannot do that step.

## Pre-flight checks (do these first, in parallel)

1. Extract `DEBEMAIL`, `DEBFULLNAME`, and `GPGKEY` directly from `~/.bashrc` (do NOT
   `source ~/.bashrc` — it has a `[ -z "$PS1" ] && return` guard that exits early in
   non-interactive shells):
   ```bash
   DEBEMAIL=$(grep -oP 'DEBEMAIL=\K\S+' ~/.bashrc | tail -1 | tr -d '"'"'")
   DEBFULLNAME=$(grep -oP 'DEBFULLNAME=\K.*' ~/.bashrc | tail -1 | tr -d '"'"'")
   GPGKEY=$(grep -oP 'GPGKEY=\K\S+' ~/.bashrc | tail -1 | tr -d '"'"'")
   echo "DEBFULLNAME=$DEBFULLNAME"
   echo "DEBEMAIL=$DEBEMAIL"
   echo "GPGKEY=$GPGKEY"
   ```
   If any are empty, stop — ask the user to add them to `~/.bashrc`:
   ```bash
   export DEBFULLNAME="Dustin Kirkland"
   export DEBEMAIL="kirkland@ubuntu.com"
   export GPGKEY="E2D9E1C5F9F5D59291F4607D95E64373F1529469"
   ```
   All three are required: `DEBFULLNAME` + `DEBEMAIL` populate the mandatory `Changed-By`
   field in the `.changes` file; Launchpad rejects uploads missing it.

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

5. Determine the next PPA iteration by querying the Launchpad API for the highest already-published `~ppaN` version:
   ```bash
   EXISTING_ITER=$(python3 -c "
   import urllib.request, json, re
   base = 'https://api.launchpad.net/1.0/~byobu/+archive/ubuntu/ppa?ws.op=getPublishedSources&source_name=byobu&status='
   iters = []
   for status in ('Published', 'Pending'):
       try:
           d = json.loads(urllib.request.urlopen(base + status).read())
           for e in d.get('entries', []):
               v = e['source_package_version']
               # Only match current-generation versions (BASE_VER prefix)
               m = re.match(r'7\.0~ppa(\d+)', v)
               if m: iters.append(int(m.group(1)))
       except: pass
   print(max(iters) if iters else 0)
   " 2>/dev/null)
   ITER=$((EXISTING_ITER + 1))
   echo "ITER=$ITER (last seen was ppa${EXISTING_ITER})"
   ```
   This queries Launchpad directly so repeated `/ppa-build` runs auto-increment even though
   the Docker build never writes back to the host changelog.

## Version scheme

For each series, the version is: `${BASE_VER}~ppa${ITER}~${SERIES}1`

Examples for BASE_VER=7.0, ITER=1:
- `7.0~ppa1~jammy1`
- `7.0~ppa1~noble1`
- `7.0~ppa1~questing1`
- `7.0~ppa1~resolute1`
- `7.0~ppa1~stonking1`

The double `~` gives strict ordering: `7.0~ppa1~noble1 < 7.0~ppa1 < 7.0`, so any official Ubuntu or higher PPA upload automatically supersedes the candidate. The series suffix (`~noble1`, `~jammy1`, …) prevents version collisions between series and allows independent rebuilds per series.

## Smoke test (run this first, before the source builds)

Run a full binary build in Docker against `ubuntu:noble` to catch test
failures and missing `Build-Depends` **locally** before uploading anything
to Launchpad. This mirrors exactly what Launchpad does: installs
`Build-Depends`, runs `debian/rules build`, then `debian/rules test`
(i.e. `override_dh_auto_test`). If this step fails, stop — do not proceed
to the source builds.

```bash
echo "=== Smoke test: local binary build ==="
docker run --rm \
  -v /home/kirkland/src/byobu:/src:ro \
  ubuntu:noble \
  bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive

    apt-get update -qq
    apt-get install -y --no-install-recommends \
      build-essential dpkg-dev debhelper dh-python \
      gettext-base automake autoconf \
      python3 python3-all python3-tornado \
      devscripts bc ca-certificates distro-info 2>&1 | tail -5

    WORKDIR=$(mktemp -d)
    cp -a /src "$WORKDIR/byobu"
    cd "$WORKDIR/byobu"

    echo ""
    echo "--- Running build step ---"
    dh build --with python3

    echo ""
    echo "--- Running test step (mirrors override_dh_auto_test) ---"
    bash usr/share/byobu/tests/test_byobu.sh
    python3 -m unittest discover -s mobile/tests -v

    echo ""
    echo "=== Smoke test PASSED ==="
  '
```

If the smoke test fails, fix the issue and re-run before continuing.

## Build the source packages (unsigned, in Docker)

Run a single Docker container that loops through all series and produces one `.changes` + `.dsc` pair per series. All series share the same source tarball.

```bash
OUTDIR=/tmp/byobu-ppa
rm -rf $OUTDIR && mkdir -p $OUTDIR

docker run --rm \
  -v /home/kirkland/src/byobu:/src:ro \
  -v $OUTDIR:/out \
  -e DEBEMAIL="$DEBEMAIL" \
  -e DEBFULLNAME="$DEBFULLNAME" \
  -e PKG="$PKG" \
  -e BASE_VER="$BASE_VER" \
  -e ITER="$ITER" \
  ubuntu:noble \
  bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive

    apt-get update -qq
    apt-get install -y --no-install-recommends \
      build-essential dpkg-dev debhelper dh-python \
      gettext-base automake autoconf \
      python3 python3-all python3-tornado \
      devscripts bc ca-certificates distro-info 2>&1 | tail -5

    # PKG, BASE_VER, ITER, DEBEMAIL, DEBFULLNAME all injected via -e flags above

    SERIES=$(ubuntu-distro-info --supported | tr "\n" " ")
    echo "Building for: $DEBFULLNAME <$DEBEMAIL>"
    echo "Series: $SERIES"

    STAGING=$(mktemp -d)
    cp -a /src "$STAGING/src"

    for CODENAME in $SERIES; do
      PPA_VER="${BASE_VER}~ppa${ITER}~${CODENAME}1"
      echo ""
      echo "=== Building $PPA_VER ==="

      BUILDDIR=$(mktemp -d)
      cp -a "$STAGING/src" "$BUILDDIR/${PKG}-${PPA_VER}"
      cd "$BUILDDIR/${PKG}-${PPA_VER}"

      # Switch to native source format (version has no - separator)
      echo "3.0 (native)" > debian/source/format

      # Stamp changelog: trailer must be "Name <email>  date" for Changed-By to populate
      DATESTAMP=$(date -R)
      {
        printf "%s (%s) %s; urgency=medium\n\n" "$PKG" "$PPA_VER" "$CODENAME"
        printf "  * PPA candidate build %s\n\n" "$PPA_VER"
        printf " -- %s <%s>  %s\n\n" "$DEBFULLNAME" "$DEBEMAIL" "$DATESTAMP"
        cat debian/changelog
      } > debian/changelog.new
      mv debian/changelog.new debian/changelog

      # Build source package — unsigned (-us -uc), source only (-S)
      dpkg-buildpackage -S -us -uc -d 2>&1 | tail -3

      # Collect all artifacts (.buildinfo required by debsign)
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

## Write the sign-and-upload script, then tell the user to run it

After the Docker build completes, write this script to `/tmp/byobu-ppa/sign-and-upload.sh`
and make it executable:

```bash
cat > /tmp/byobu-ppa/sign-and-upload.sh << 'EOF'
#!/bin/bash
set -e

GPGKEY=$(grep -oP 'GPGKEY=\K\S+' ~/.bashrc | tail -1 | tr -d '"'"'")
PPA="ppa:byobu/ppa"
DIR="$(dirname "$0")"

echo "Signing with key: $GPGKEY"
echo "Uploading to:     $PPA"
echo ""

for f in "$DIR"/*_source.changes; do
    echo "=== Signing $f ==="
    debsign -k "$GPGKEY" "$f"
done

echo ""
echo "All signed. Uploading..."
echo ""

for f in "$DIR"/*_source.changes; do
    echo "=== Uploading $f ==="
    dput "$PPA" "$f"
done

echo ""
echo "Done. Monitor builds at:"
echo "  https://launchpad.net/~byobu/+archive/ubuntu/ppa"
EOF
chmod +x /tmp/byobu-ppa/sign-and-upload.sh
```

Then tell the user:

```
All series built. To sign and upload:

  /tmp/byobu-ppa/sign-and-upload.sh

GPG will prompt for your passphrase once per series.
Monitor builds at: https://launchpad.net/~byobu/+archive/ubuntu/ppa
```

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
