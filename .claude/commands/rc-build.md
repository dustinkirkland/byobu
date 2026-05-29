---
description: Full trustmux release pipeline — publishes to PyPI (via git tag + GH Actions), updates Homebrew tap formula, builds PPA source packages for all supported Ubuntu series, and generates the sign-and-upload script
---

Full release pipeline for trustmux: PyPI → Homebrew → PPA.
The PyPI upload is handled by GitHub Actions (trusted publishing) triggered by a git tag.
Signing and uploading the PPA requires the user's interactive GPG passphrase; that step is scripted but not run by Claude.

All versions are derived from the source tree — nothing is hardcoded.

## Version scheme

| Channel   | Format                          | Example         | Source of truth                         |
|-----------|---------------------------------|-----------------|-----------------------------------------|
| PyPI      | `{BASE}rc{N}` or `{BASE}a{N}`  | `7.1rc2`        | PyPI API → highest existing pre-release |
| Git tag   | `trustmux-v{PYPI_VER}`         | `trustmux-v7.1rc2` | derived from PyPI version            |
| PPA       | `{BASE}~{PRE}~{series}1`       | `7.1~rc2~noble1`| mirrors PyPI pre-release tag           |
| Homebrew  | PyPI sdist tarball              | `7.1rc2.tar.gz` | derived from PyPI version              |
| Debian    | `{BASE}` (final release only)   | `7.1`           | `debian/changelog`                     |

`BASE` always comes from `debian/changelog`. The `rc` series is preferred over `a` (alpha) for
the PPA and Homebrew because rc packages are installable as-is. Upgrade path is strict:
`7.1~rc1~*  <  7.1~rc2~*  <  7.1` (Debian tilde ordering).

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
which dput debsign git python3 docker 2>&1
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

## Phase 2: Determine versions (all derived from source — no hardcoding)

```bash
# ── Canonical base version from debian/changelog ──────────────────────────
PKG=$(head -1 /home/kirkland/src/byobu/debian/changelog | grep -oP '^\S+')
BASE_VER=$(head -1 /home/kirkland/src/byobu/debian/changelog | grep -oP '\(\K[^)~]+' | tr -d '[:space:]')
echo "Package:      $PKG"
echo "Base version: $BASE_VER"

# ── Next PyPI pre-release: prefer rcN, fall back to aN ────────────────────
# Queries PyPI for existing versions matching this base, increments the
# highest rc (or a) number. Stays within the same pre-release series.
NEXT_PYPI=$(python3 -c "
import urllib.request, json, re, sys
base = '${BASE_VER}'
pat_rc = re.compile(r'^' + re.escape(base) + r'rc(\d+)$')
pat_a  = re.compile(r'^' + re.escape(base) + r'a(\d+)$')
try:
    d = json.loads(urllib.request.urlopen('https://pypi.org/pypi/trustmux/json').read())
    versions = list(d['releases'].keys())
    rcs = [int(m.group(1)) for v in versions for m in [pat_rc.match(v)] if m]
    if rcs:
        print(f'{base}rc{max(rcs) + 1}')
        sys.exit(0)
    alphas = [int(m.group(1)) for v in versions for m in [pat_a.match(v)] if m]
    print(f'{base}a{max(alphas) + 1 if alphas else 1}')
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null)
echo "Next PyPI:    $NEXT_PYPI"
[ -z "$NEXT_PYPI" ] || [[ "$NEXT_PYPI" == ERROR* ]] && { echo "Version detection failed — stop."; exit 1; }

# ── PPA pre-release tag: strip BASE_VER prefix from NEXT_PYPI ─────────────
# PyPI "7.1rc2"  →  PPA_PRE "rc2"  →  PPA version "7.1~rc2~noble1"
# PyPI "7.1a3"   →  PPA_PRE "a3"   →  PPA version "7.1~a3~noble1"
PPA_PRE="${NEXT_PYPI#${BASE_VER}}"
echo "PPA pre-release tag: $PPA_PRE"

# ── Guard: verify this PPA slot is not already occupied ───────────────────
EXISTING_PPA=$(python3 -c "
import urllib.request, json, re
base_ver = '${BASE_VER}'
ppa_pre  = '${PPA_PRE}'
pattern  = re.compile(r'^' + re.escape(base_ver) + r'~' + re.escape(ppa_pre) + r'~')
url_base = 'https://api.launchpad.net/1.0/~byobu/+archive/ubuntu/ppa?ws.op=getPublishedSources&source_name=byobu&status='
found = []
for status in ('Published', 'Pending'):
    try:
        d = json.loads(urllib.request.urlopen(url_base + status).read())
        found += [e['source_package_version'] for e in d.get('entries', []) if pattern.match(e['source_package_version'])]
    except: pass
print(' '.join(found) if found else 'none')
" 2>/dev/null)
if [ "$EXISTING_PPA" != "none" ]; then
  echo "ERROR: PPA slot ${BASE_VER}~${PPA_PRE}~* already exists: $EXISTING_PPA"
  echo "       PyPI version detection may be stale. Stop and verify."
  exit 1
fi
echo "PPA slot ${BASE_VER}~${PPA_PRE}~* is free — good to build."

# ── Supported Ubuntu series ────────────────────────────────────────────────
SERIES=$(ubuntu-distro-info --supported 2>/dev/null || python3 -c "
import urllib.request, json
d = json.loads(urllib.request.urlopen('https://api.launchpad.net/1.0/ubuntu/series').read())
active = {'Active Development','Current Stable Release','Supported'}
series = [e['name'] for e in d['entries']
          if e['status'] in active and float(e.get('version','0')) >= 22.04]
print(' '.join(series))
")
echo "Series: $SERIES"
```

If any value looks wrong, stop and confirm with the user before proceeding.

## Phase 3: PyPI — create git tag and push

This triggers the GitHub Actions `pypi-publish.yml` workflow, which stamps `mobile/pyproject.toml`
with `$VERSION`, builds sdist + wheel, and uploads via OIDC trusted publishing.

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

## Phase 4: Smoke test (run while PyPI builds)

While GitHub Actions publishes to PyPI, run the local Docker smoke test to catch
test failures before burning Launchpad build time.

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

If the smoke test fails, stop — do not proceed to Homebrew or PPA.

## Phase 5: Homebrew formula update

Wait for PyPI to have the new version (GH Actions usually completes in ~30s).
Poll up to 10 times with 15-second waits:

```bash
read TARBALL_URL TARBALL_SHA256 < <(python3 -c "
import urllib.request, json, time, sys
version = '${NEXT_PYPI}'
for attempt in range(10):
    try:
        d = json.loads(urllib.request.urlopen(f'https://pypi.org/pypi/trustmux/{version}/json').read())
        for u in d['urls']:
            if u['filename'].endswith('.tar.gz'):
                print(u['url'], u['digests']['sha256'])
                sys.exit(0)
    except Exception:
        pass
    print(f'Attempt {attempt+1}/10 — PyPI not ready, waiting 15s...', file=sys.stderr)
    time.sleep(15)
print('ERROR: timed out', file=sys.stderr)
sys.exit(1)
" 2>/dev/null)
echo "Tarball: $TARBALL_URL"
echo "SHA256:  $TARBALL_SHA256"
```

Once both are set, update the Homebrew formula:

```bash
cd "$TAP_DIR"
git pull --ff-only

python3 - "$TARBALL_URL" "$TARBALL_SHA256" <<'PYEOF'
import re, sys
url, sha = sys.argv[1], sys.argv[2]
formula = open('Formula/trustmux.rb').read()
# Update only the top-level url (2-space indent, not inside a resource block)
formula = re.sub(r'^  url "https://files\.pythonhosted\.org/[^"]*"',
                 f'  url "{url}"', formula, count=1, flags=re.MULTILINE)
# Update only the top-level sha256 (2-space indent)
formula = re.sub(r'^  sha256 "[a-f0-9]+"',
                 f'  sha256 "{sha}"', formula, count=1, flags=re.MULTILINE)
open('Formula/trustmux.rb', 'w').write(formula)
print("Formula updated.")
PYEOF

echo "=== Updated formula ==="
grep -E '^\s+(url|sha256) ' Formula/trustmux.rb | head -4

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
OUTDIR=/tmp/byobu-ppa
rm -rf "$OUTDIR" && mkdir -p "$OUTDIR"

docker run --rm \
  -v /home/kirkland/src/byobu:/src:ro \
  -v "$OUTDIR":/out \
  -e DEBEMAIL="$DEBEMAIL" \
  -e DEBFULLNAME="$DEBFULLNAME" \
  -e PKG="$PKG" \
  -e BASE_VER="$BASE_VER" \
  -e PPA_PRE="$PPA_PRE" \
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
    echo "PPA version prefix: ${BASE_VER}~${PPA_PRE}"

    STAGING=$(mktemp -d)
    cp -a /src "$STAGING/src"

    for CODENAME in $SERIES; do
      PPA_VER="${BASE_VER}~${PPA_PRE}~${CODENAME}1"
      echo ""
      echo "=== Building $PPA_VER ==="

      BUILDDIR=$(mktemp -d)
      cp -a "$STAGING/src" "$BUILDDIR/${PKG}-${BASE_VER}"
      cd "$BUILDDIR/${PKG}-${BASE_VER}"

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
    chown -R $(stat -c '"'"'%u:%g'"'"' /out) /out/
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
- If PyPI polling times out (>2.5 min), check https://github.com/dustinkirkland/byobu/actions before retrying.
- If a series is still in "Active Development" and Launchpad lacks its build toolchain, that LP build may fail — that's expected; skip it.
- For a stable final release (e.g. `7.1`): push tag `trustmux-v7.1` instead of letting the script auto-increment. The PPA would use `7.1~{series}1` with no pre-release tag.
- Always include `--repo dustinkirkland/byobu` in any `gh release create` command so it works from any working directory.
