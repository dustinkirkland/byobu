---
description: Full trustmux release pipeline — publishes to PyPI (via git tag + GH Actions), updates Homebrew tap formula, builds PPA source packages for all supported Ubuntu series, and generates the sign-and-upload script
---

Full release pipeline for trustmux: PyPI → Homebrew → PPA.
The PyPI upload is handled by GitHub Actions (trusted publishing) triggered by a git tag.
Signing and uploading the PPA requires the user's interactive GPG passphrase; that step is scripted but not run by Claude.

## Phase 1: Pre-flight checks (run all in parallel)

### 1a. Extract build identity from ~/.bashrc
```bash
DEBEMAIL=$(grep -oP 'DEBEMAIL=\K\S+' ~/.bashrc | tail -1 | tr -d '"'"'")
DEBFULLNAME=$(grep -oP 'DEBFULLNAME=\K.*' ~/.bashrc | tail -1 | tr -d '"'"'")
GPGKEY=$(grep -oP 'GPGKEY=\K\S+' ~/.bashrc | tail -1 | tr -d '"'"'")
echo "DEBFULLNAME=$DEBFULLNAME"
echo "DEBEMAIL=$DEBEMAIL"
echo "GPGKEY=$GPGKEY"
```
If any are empty, stop and ask the user to add them to `~/.bashrc`:
```bash
export DEBFULLNAME="Dustin Kirkland"
export DEBEMAIL="kirkland@ubuntu.com"
export GPGKEY="E2D9E1C5F9F5D59291F4607D95E64373F1529469"
```

### 1b. Check required tools
```bash
which dput debsign git python3 2>&1
```
If `dput` or `debsign` are missing: `sudo apt install devscripts dput`

### 1c. Find or clone the Homebrew tap repo
```bash
TAP_DIR=""
for d in /tmp/homebrew-trustmux ~/src/homebrew-trustmux ~/homebrew-trustmux; do
  if [ -d "$d/.git" ]; then TAP_DIR="$d"; break; fi
done
if [ -z "$TAP_DIR" ]; then
  git clone git@github.com:dustinkirkland/homebrew-trustmux.git /tmp/homebrew-trustmux
  TAP_DIR=/tmp/homebrew-trustmux
fi
echo "Homebrew tap: $TAP_DIR"
ls "$TAP_DIR/Formula/"
```

## Phase 2: Determine versions

### 2a. Next PyPI/trustmux version
Query PyPI for the latest published version and auto-increment the alpha number:
```bash
NEXT_PYPI=$(python3 -c "
import urllib.request, json, re
try:
    d = json.loads(urllib.request.urlopen('https://pypi.org/pypi/trustmux/json').read())
    versions = list(d['releases'].keys())
    alphas = [int(m.group(1)) for v in versions for m in [re.match(r'7\.0a(\d+)', v)] if m]
    last = max(alphas) if alphas else 0
    print(f'7.0a{last + 1}')
except Exception as e:
    print(f'ERROR: {e}')
" 2>/dev/null)
echo "Next PyPI version: $NEXT_PYPI"
```
If the query fails or the result looks wrong, stop and confirm with the user.

### 2b. Byobu base version and next PPA iteration
```bash
head -1 /home/kirkland/src/byobu/debian/changelog
# extract PKG and BASE_VER, e.g. byobu / 7.0
```
Then query Launchpad for the highest published PPA iteration:
```bash
EXISTING_ITER=$(python3 -c "
import urllib.request, json, re
base = 'https://api.launchpad.net/1.0/~byobu/+archive/ubuntu/ppa?ws.op=getPublishedSources&source_name=byobu&status='
iters = []
for status in ('Published', 'Pending', 'Superseded'):
    try:
        d = json.loads(urllib.request.urlopen(base + status).read())
        for e in d.get('entries', []):
            v = e['source_package_version']
            m = re.match(r'7\.0~ppa(\d+)', v)
            if m: iters.append(int(m.group(1)))
    except: pass
print(max(iters) if iters else 0)
" 2>/dev/null)
ITER=$((EXISTING_ITER + 1))
echo "API detected last PPA iter: ppa${EXISTING_ITER} → will build ppa${ITER}"
echo "Verify at: https://launchpad.net/~byobu/+archive/ubuntu/ppa/+packages"
```

### 2c. Supported Ubuntu series
```bash
SERIES=$(ubuntu-distro-info --supported 2>/dev/null || python3 -c "
import urllib.request, json
url = 'https://api.launchpad.net/1.0/ubuntu/series'
d = json.loads(urllib.request.urlopen(url).read())
active = {'Active Development','Current Stable Release','Supported'}
series = [e['name'] for e in d['entries']
          if e['status'] in active and float(e.get('version','0')) >= 22.04]
print(' '.join(series))
")
echo "Series: $SERIES"
```

## Phase 3: PyPI — create git tag and push

This triggers the GitHub Actions `pypi-publish.yml` workflow which stamps the version, builds the sdist+wheel, and uploads via OIDC trusted publishing.

```bash
cd /home/kirkland/src/byobu
git tag trustmux-v${NEXT_PYPI}
git push origin trustmux-v${NEXT_PYPI}
```

Tell the user:
```
✓ Tag trustmux-v{NEXT_PYPI} pushed.
  GitHub Actions will build and upload to PyPI automatically.
  Monitor at: https://github.com/dustinkirkland/byobu/actions
```

## Phase 4: PPA smoke test (run while PyPI builds)

While GitHub Actions publishes to PyPI, run the local Docker smoke test to catch test failures before burning Launchpad build time.

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
    echo "--- Running install step (catches duplicate-install bugs) ---"
    dh install --with python3

    echo ""
    echo "=== Smoke test PASSED ==="
  '
```

If the smoke test fails, stop — do not proceed to PPA source builds.

## Phase 5: Homebrew formula update

After the smoke test completes, check whether PyPI has the new version. Poll up to ~10 times with 15-second waits:

```bash
python3 -c "
import urllib.request, json, time, sys
version = '${NEXT_PYPI}'
for attempt in range(10):
    try:
        url = f'https://pypi.org/pypi/trustmux/{version}/json'
        d = json.loads(urllib.request.urlopen(url).read())
        for u in d['urls']:
            if u['filename'].endswith('.tar.gz'):
                print(u['url'])
                print(u['digests']['sha256'])
                sys.exit(0)
    except Exception:
        pass
    print(f'Attempt {attempt+1}/10 — PyPI not ready yet, waiting 15s...', file=sys.stderr)
    time.sleep(15)
print('ERROR: timed out waiting for PyPI', file=sys.stderr)
sys.exit(1)
"
```

Once `TARBALL_URL` and `TARBALL_SHA256` are known, update the Homebrew formula:

```bash
cd "$TAP_DIR"
git pull --ff-only   # sync with remote first

# Update url and sha256 in Formula/trustmux.rb
python3 - <<'PYEOF'
import re, sys

formula = open('Formula/trustmux.rb').read()

# Replace url line
formula = re.sub(
    r'  url "https://files\.pythonhosted\.org/[^"]*"',
    f'  url "{TARBALL_URL}"',
    formula
)
# Replace the first sha256 line (the package sha256, not the tornado resource sha256)
formula = re.sub(
    r'(  sha256 ")[a-f0-9]+"',
    f'\\g<1>{TARBALL_SHA256}"',
    formula,
    count=1
)

open('Formula/trustmux.rb', 'w').write(formula)
print("Formula updated.")
PYEOF

echo "=== Updated formula ==="
grep -E 'url|sha256' Formula/trustmux.rb | head -6

git add Formula/trustmux.rb
git commit -m "trustmux: update to ${NEXT_PYPI}"
git push origin main 2>/dev/null || git push origin master
```

Tell the user:
```
✓ Homebrew tap updated and pushed.
  brew upgrade trustmux  (or brew install dustinkirkland/trustmux/trustmux)
```

## Phase 6: PPA source builds (in Docker)

```bash
PKG=byobu
BASE_VER=$(head -1 /home/kirkland/src/byobu/debian/changelog | grep -oP '\(\K[^)]+' | sed 's/ .*//')
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

      echo "3.0 (native)" > debian/source/format

      DATESTAMP=$(date -R)
      {
        printf "%s (%s) %s; urgency=medium\n\n" "$PKG" "$PPA_VER" "$CODENAME"
        printf "  * PPA candidate build %s\n\n" "$PPA_VER"
        printf " -- %s <%s>  %s\n\n" "$DEBFULLNAME" "$DEBEMAIL" "$DATESTAMP"
        cat debian/changelog
      } > debian/changelog.new
      mv debian/changelog.new debian/changelog

      dpkg-buildpackage -S -us -uc -d 2>&1 | tail -3

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

## Phase 7: Write the sign-and-upload script

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
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  RC build complete for trustmux-v{NEXT_PYPI}

  PyPI:     https://pypi.org/project/trustmux/{NEXT_PYPI}/
  Homebrew: brew install dustinkirkland/trustmux/trustmux
  PPA:      https://launchpad.net/~byobu/+archive/ubuntu/ppa

  To sign and upload PPA packages:
    /tmp/byobu-ppa/sign-and-upload.sh

  GPG will prompt for your passphrase once per series.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Notes

- Do NOT run `debsign` or `dput` — both require interactive GPG.
- Do NOT run `git push --force` or amend published tags.
- If PyPI polling times out (>2.5 min), the GH Actions job may still be running — check https://github.com/dustinkirkland/byobu/actions before retrying.
- If a series is still in "Active Development" and Launchpad lacks its build toolchain, that series build may fail on LP — that's expected; skip it.
- To release a non-alpha (e.g. `7.0` stable), push tag `trustmux-v7.0` instead of the auto-incremented alpha.
- The PPA version scheme `{BASE_VER}~ppa{ITER}~{SERIES}1` ensures strict ordering: any official Ubuntu or higher PPA upload automatically supersedes the candidate.
