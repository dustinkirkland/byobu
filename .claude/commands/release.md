---
description: Cut a byobu/trustmux RC or final release — PPA, PyPI, Debian experimental, Ubuntu dev, Homebrew, GitHub releases, and a ready-to-run sign-and-upload script
---

Full byobu/trustmux release pipeline. Handles two modes:

- **RC** (`/release rc`): PPA-only candidate build + trustmux PyPI RC tag. Run this many times while iterating.
- **Final** (`/release final`): Full release — Debian experimental + Ubuntu dev + PPA + PyPI + Homebrew tap update + GitHub release tags.

Use `/release rc` for iteration; `/release final` only when the RC has been validated.

---

## Phase 1: Pre-flight checks (run all in parallel)

### 1a. Build identity from ~/.bashrc
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

### 1b. Required tools
```bash
which dput debsign git docker python3 gh 2>&1
```
If `dput` or `debsign` are missing: `sudo apt install devscripts dput`
If `gh` is missing: `sudo apt install gh` or `brew install gh`

### 1c. Homebrew tap repo (final releases only — skip for RC)
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
```

---

## Phase 2: Determine versions

### 2a. Base version from debian/changelog
```bash
BASE_VER=$(head -1 /home/kirkland/src/byobu/debian/changelog | grep -oP '\(\K[^)]+' | sed 's/ .*//')
PKG="byobu"
echo "Base version: $BASE_VER"
```

### 2b. RC number (RC mode only)

Auto-detect from existing git tags:
```bash
EXISTING_RC=$(git -C /home/kirkland/src/byobu tag --list "trustmux-v${BASE_VER}rc*" \
  | grep -oP 'rc\K[0-9]+' | sort -n | tail -1)
RC_NUM=$((${EXISTING_RC:-0} + 1))
RC_VERSION="${BASE_VER}rc${RC_NUM}"
PYPI_VERSION="${BASE_VER}rc${RC_NUM}"
echo "RC number: $RC_NUM  →  trustmux-v${PYPI_VERSION}"
```

For **final** mode, set instead:
```bash
PYPI_VERSION="${BASE_VER}"
echo "Final version: ${PYPI_VERSION}"
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
DEVEL_SERIES=$(ubuntu-distro-info --devel 2>/dev/null || echo "stonking")
echo "Series: $SERIES"
echo "Devel:  $DEVEL_SERIES"
```

### 2d. PPA iteration (RC or final)

For **RC** mode — version scheme: `{BASE_VER}~rc{N}~{series}1`
```bash
PPA_BASE="${BASE_VER}~rc${RC_NUM}"
```

For **final** mode — version scheme: `{BASE_VER}~{series}1`
```bash
PPA_BASE="${BASE_VER}"
```

Query Launchpad for existing iterations matching the prefix to avoid collisions:
```bash
EXISTING_ITER=$(python3 -c "
import urllib.request, json, re
prefix = '${PPA_BASE}~'
base = 'https://api.launchpad.net/1.0/~byobu/+archive/ubuntu/ppa?ws.op=getPublishedSources&source_name=byobu&status='
iters = []
for status in ('Published', 'Pending', 'Superseded'):
    try:
        d = json.loads(urllib.request.urlopen(base + status).read())
        for e in d.get('entries', []):
            v = e['source_package_version']
            m = re.search(r'~(\d+)$', v)
            if m and v.startswith(prefix): iters.append(int(m.group(1)))
    except: pass
print(max(iters) if iters else 0)
" 2>/dev/null)
echo "Existing PPA suffix: ${EXISTING_ITER} → next build uses suffix 1 per series (no global iter needed for RC scheme)"
echo "Verify at: https://launchpad.net/~byobu/+archive/ubuntu/ppa/+packages"
```

Set the output directory:
```bash
OUTDIR="/tmp/byobu-release-${PPA_BASE}"
rm -rf "$OUTDIR"
mkdir -p "$OUTDIR/ppa"
[ "$MODE" = "final" ] && mkdir -p "$OUTDIR/debian" "$OUTDIR/ubuntu"
echo "Output: $OUTDIR"
```

---

## Phase 3: PyPI tag (triggers GitHub Actions OIDC publish)

```bash
cd /home/kirkland/src/byobu
git tag "trustmux-v${PYPI_VERSION}"
git push origin "trustmux-v${PYPI_VERSION}"
```

Tell the user:
```
✓ Tag trustmux-v{PYPI_VERSION} pushed.
  GitHub Actions will build and upload to PyPI automatically.
  Monitor at: https://github.com/dustinkirkland/byobu/actions
```

---

## Phase 4: Smoke test (run while PyPI builds)

Always run this before burning Launchpad build time.

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
    echo "--- Running test step ---"
    bash usr/share/byobu/tests/test_byobu.sh
    python3 -m unittest discover -s mobile/tests -v

    echo ""
    echo "--- Running install step ---"
    dh install --with python3

    echo ""
    echo "=== Smoke test PASSED ==="
  '
```

**If the smoke test fails, stop.** Fix the failure before proceeding.

---

## Phase 5: PPA source builds (Docker, all series)

```bash
docker run --rm \
  -v /home/kirkland/src/byobu:/src:ro \
  -v "$OUTDIR/ppa":/out \
  -e DEBEMAIL="$DEBEMAIL" \
  -e DEBFULLNAME="$DEBFULLNAME" \
  -e PKG="$PKG" \
  -e BASE_VER="$BASE_VER" \
  -e PPA_BASE="$PPA_BASE" \
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
      PPA_VER="${PPA_BASE}~${CODENAME}1"
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
    chown -R $(stat -c '"'"'%u:%g'"'"' /out) /out/
  '
```

---

## Phase 6: Debian experimental source build (final mode only)

Skip for RC releases.

```bash
docker run --rm \
  -v /home/kirkland/src/byobu:/src:ro \
  -v "$OUTDIR/debian":/out \
  -e DEBEMAIL="$DEBEMAIL" \
  -e DEBFULLNAME="$DEBFULLNAME" \
  -e PKG="$PKG" \
  -e BASE_VER="$BASE_VER" \
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

    STAGING=$(mktemp -d)
    cp -a /src "$STAGING/src"

    BUILDDIR=$(mktemp -d)
    cp -a "$STAGING/src" "$BUILDDIR/${PKG}-${BASE_VER}"
    cd "$BUILDDIR/${PKG}-${BASE_VER}"

    # Debian native: no orig tarball needed
    echo "3.0 (native)" > debian/source/format

    dpkg-buildpackage -S -us -uc -d 2>&1 | tail -3

    cp -v "$BUILDDIR"/*.changes "$BUILDDIR"/*.dsc "$BUILDDIR"/*.tar.* "$BUILDDIR"/*.buildinfo /out/ 2>/dev/null || true
    chown -R $(stat -c '"'"'%u:%g'"'"' /out) /out/

    echo "=== Debian experimental source package built ==="
    ls -lh /out/
  '
```

---

## Phase 7: Ubuntu dev-series source build (final mode only)

Skip for RC releases. Uses `{BASE_VER}-0ubuntu1` as the version, targeting the current development series.

```bash
UBUNTU_VER="${BASE_VER}-0ubuntu1"

docker run --rm \
  -v /home/kirkland/src/byobu:/src:ro \
  -v "$OUTDIR/ubuntu":/out \
  -e DEBEMAIL="$DEBEMAIL" \
  -e DEBFULLNAME="$DEBFULLNAME" \
  -e PKG="$PKG" \
  -e BASE_VER="$BASE_VER" \
  -e UBUNTU_VER="$UBUNTU_VER" \
  -e DEVEL_SERIES="$DEVEL_SERIES" \
  ubuntu:noble \
  bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive

    apt-get update -qq
    apt-get install -y --no-install-recommends \
      build-essential dpkg-dev debhelper dh-python \
      gettext-base automake autoconf \
      python3 python3-all python3-tornado \
      devscripts bc ca-certificates git 2>&1 | tail -5

    STAGING=$(mktemp -d)
    cp -a /src "$STAGING/src"

    # Ubuntu non-native: needs orig tarball + debian/ overlay (3.0 quilt)
    BUILDDIR=$(mktemp -d)

    # Generate clean orig tarball from git (avoids .venv, build/, dist/ contamination)
    cd "$STAGING/src"
    git archive --format=tar.gz --prefix="${PKG}-${BASE_VER}/" HEAD \
      -o "$BUILDDIR/${PKG}_${BASE_VER}.orig.tar.gz"

    cp -a "$STAGING/src/debian" "$BUILDDIR/"
    mkdir "$BUILDDIR/${PKG}-${BASE_VER}"
    tar -xzf "$BUILDDIR/${PKG}_${BASE_VER}.orig.tar.gz" -C "$BUILDDIR" --strip-components=1 \
      -C "$BUILDDIR/${PKG}-${BASE_VER}" --strip-components=1 2>/dev/null || \
      tar -xzf "$BUILDDIR/${PKG}_${BASE_VER}.orig.tar.gz" -C "$BUILDDIR"

    cd "$BUILDDIR/${PKG}-${BASE_VER}"
    [ ! -d debian ] && cp -a "$BUILDDIR/debian" .
    echo "3.0 (quilt)" > debian/source/format

    DATESTAMP=$(date -R)
    {
      printf "%s (%s) %s; urgency=medium\n\n" "$PKG" "$UBUNTU_VER" "$DEVEL_SERIES"
      printf "  * Ubuntu development series upload\n\n"
      printf " -- %s <%s>  %s\n\n" "$DEBFULLNAME" "$DEBEMAIL" "$DATESTAMP"
      cat debian/changelog
    } > debian/changelog.new
    mv debian/changelog.new debian/changelog

    dpkg-buildpackage -S -us -uc -d 2>&1 | tail -3

    cp -v "$BUILDDIR"/*.changes "$BUILDDIR"/*.dsc "$BUILDDIR"/*.tar.* "$BUILDDIR"/*.buildinfo /out/ 2>/dev/null || true
    chown -R $(stat -c '"'"'%u:%g'"'"' /out) /out/

    echo "=== Ubuntu ${DEVEL_SERIES} source package built ==="
    ls -lh /out/
  '
```

---

## Phase 8: Homebrew formula update (final mode only, after PyPI is live)

Skip for RC releases.

Poll PyPI until the new version is available (up to ~10 × 15s = 2.5 min):

```bash
read TARBALL_URL TARBALL_SHA256 < <(python3 -c "
import urllib.request, json, time, sys
version = '${PYPI_VERSION}'
for attempt in range(10):
    try:
        url = f'https://pypi.org/pypi/trustmux/{version}/json'
        d = json.loads(urllib.request.urlopen(url).read())
        for u in d['urls']:
            if u['filename'].endswith('.tar.gz'):
                print(u['url'], u['digests']['sha256'])
                sys.exit(0)
    except Exception:
        pass
    print(f'Attempt {attempt+1}/10 — not ready, waiting 15s...', file=sys.stderr)
    time.sleep(15)
print('ERROR: timed out waiting for PyPI', file=sys.stderr)
sys.exit(1)
" 2>/dev/null)
echo "URL:    $TARBALL_URL"
echo "SHA256: $TARBALL_SHA256"
```

Update the formula:

```bash
cd "$TAP_DIR"
git pull --ff-only

python3 - <<PYEOF
import re

formula = open('Formula/trustmux.rb').read()

formula = re.sub(
    r'^  url "https://files\.pythonhosted\.org/[^"]*"',
    f'  url "${TARBALL_URL}"',
    formula, count=1, flags=re.MULTILINE
)
formula = re.sub(
    r'^  sha256 "[a-f0-9]+"',
    f'  sha256 "${TARBALL_SHA256}"',
    formula, count=1, flags=re.MULTILINE
)
formula = re.sub(
    r'^  version "[^"]*"',
    f'  version "${PYPI_VERSION}"',
    formula, count=1, flags=re.MULTILINE
)

open('Formula/trustmux.rb', 'w').write(formula)
print("Formula updated.")
PYEOF

echo "=== Updated formula ==="
grep -E 'url|sha256|version' Formula/trustmux.rb | head -6

git add Formula/trustmux.rb
git commit -m "trustmux: update to ${PYPI_VERSION}"
git push origin main 2>/dev/null || git push origin master
```

---

## Phase 9: GitHub release tags

### RC mode
```bash
gh release create "trustmux-v${PYPI_VERSION}" \
  --repo dustinkirkland/byobu \
  --title "Trustmux ${PYPI_VERSION} (RC)" \
  --prerelease \
  --notes "Release candidate. Test via:
  pip install trustmux==${PYPI_VERSION}
  PPA: ppa:byobu/ppa  (${PPA_BASE}~{series}1)"
```

### Final mode
```bash
gh release create "${BASE_VER}" \
  --repo dustinkirkland/byobu \
  --title "byobu ${BASE_VER}" \
  --notes-file /tmp/byobu-release-notes.md

gh release create "trustmux-v${PYPI_VERSION}" \
  --repo dustinkirkland/byobu \
  --title "Trustmux ${PYPI_VERSION}" \
  --notes-file /tmp/byobu-release-notes.md
```

---

## Phase 10: Write sign-and-upload script

### RC mode (PPA only)

```bash
cat > "$OUTDIR/sign-and-upload.sh" << SCRIPT
#!/bin/bash
set -e
GPGKEY="\${GPGKEY:-${GPGKEY}}"
PPA="ppa:byobu/ppa"
DIR="\$(dirname "\$0")/ppa"

echo "Signing with key: \$GPGKEY"
echo "Uploading to:     \$PPA"
echo ""

for f in "\$DIR"/*_source.changes; do
  echo "=== Signing \$f ==="
  debsign -k "\$GPGKEY" "\$f"
done

echo ""
echo "All signed. Upload to PPA? [y/N] "
read -r ans
if [[ "\$ans" =~ ^[Yy]\$ ]]; then
  for f in "\$DIR"/*_source.changes; do
    echo "=== Uploading \$f ==="
    dput "\$PPA" "\$f"
  done
  echo "Done. Monitor: https://launchpad.net/~byobu/+archive/ubuntu/ppa"
else
  echo "Skipped upload."
fi
SCRIPT
chmod +x "$OUTDIR/sign-and-upload.sh"
```

### Final mode (Debian + Ubuntu + PPA)

```bash
cat > "$OUTDIR/sign-and-upload.sh" << SCRIPT
#!/bin/bash
set -e
GPGKEY="\${GPGKEY:-${GPGKEY}}"
BASE="${OUTDIR}"

echo "========================================"
echo " byobu ${BASE_VER} sign-and-upload"
echo " GPG key: \$GPGKEY"
echo "========================================"
echo ""

# Step 1: Sign everything
echo "── Step 1: GPG signing ─────────────────────────────────────────────────"
for f in "\$BASE"/debian/*_source.changes "\$BASE"/ubuntu/*_source.changes "\$BASE"/ppa/*_source.changes; do
  [ -f "\$f" ] || continue
  echo "  Signing: \$f"
  debsign -k "\$GPGKEY" "\$f"
done
echo "All signed."
echo ""

# Step 2: Debian experimental
echo "── Step 2: Debian experimental ────────────────────────────────────────"
echo "  dput ftp-master \$BASE/debian/byobu_${BASE_VER}_source.changes"
read -rp "  Upload to Debian experimental? [y/N] " ans
[[ "\$ans" =~ ^[Yy]\$ ]] && dput ftp-master "\$BASE/debian/byobu_${BASE_VER}_source.changes" || echo "  Skipped."
echo ""

# Step 3: Ubuntu dev series
echo "── Step 3: Ubuntu ${DEVEL_SERIES} (dev series) ──────────────────────────────────"
echo "  dput ubuntu \$BASE/ubuntu/byobu_${UBUNTU_VER}_source.changes"
read -rp "  Upload to Ubuntu ${DEVEL_SERIES}? [y/N] " ans
[[ "\$ans" =~ ^[Yy]\$ ]] && dput ubuntu "\$BASE/ubuntu/byobu_${UBUNTU_VER}_source.changes" || echo "  Skipped."
echo ""

# Step 4: PPA (all series)
echo "── Step 4: PPA ppa:byobu/ppa ───────────────────────────────────────────"
read -rp "  Upload all series to ppa:byobu/ppa? [y/N] " ans
if [[ "\$ans" =~ ^[Yy]\$ ]]; then
  for f in "\$BASE"/ppa/*_source.changes; do
    echo "  dput ppa:byobu/ppa \$f"
    dput ppa:byobu/ppa "\$f"
  done
  echo "Done. Monitor: https://launchpad.net/~byobu/+archive/ubuntu/ppa"
else
  echo "  Skipped."
fi
echo ""

echo "========================================"
echo " GitHub releases (run from anywhere):"
echo "   gh release create ${BASE_VER} --repo dustinkirkland/byobu --title 'byobu ${BASE_VER}' --notes-file /tmp/byobu-release-notes.md"
echo "   gh release create trustmux-v${PYPI_VERSION} --repo dustinkirkland/byobu --title 'Trustmux ${PYPI_VERSION}' --notes-file /tmp/byobu-release-notes.md"
echo "========================================"
SCRIPT
chmod +x "$OUTDIR/sign-and-upload.sh"
```

---

## Phase 11: Summary to user

### RC mode:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  RC build complete: byobu {PPA_BASE}

  PyPI (trustmux):  trustmux-v{PYPI_VERSION} tag pushed → GH Actions
                    https://github.com/dustinkirkland/byobu/actions
  PPA:              ppa:byobu/ppa — {PPA_BASE}~{series}1
                    https://launchpad.net/~byobu/+archive/ubuntu/ppa

  Sign and upload:
    {OUTDIR}/sign-and-upload.sh

  GPG will prompt for your passphrase once per series.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Final mode:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Release complete: byobu {BASE_VER} / trustmux {PYPI_VERSION}

  PyPI (trustmux):  trustmux-v{PYPI_VERSION} tag pushed → GH Actions
                    https://github.com/dustinkirkland/byobu/actions
  Homebrew:         brew upgrade dustinkirkland/trustmux/trustmux
  Debian:           byobu {BASE_VER} → experimental
  Ubuntu:           byobu {UBUNTU_VER} → {DEVEL_SERIES}
  PPA:              ppa:byobu/ppa — {BASE_VER}~{series}1
                    https://launchpad.net/~byobu/+archive/ubuntu/ppa

  Sign and upload:
    {OUTDIR}/sign-and-upload.sh

  GPG will prompt for your passphrase once per series.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Notes

- **Never run `debsign` or `dput` directly** — both require interactive GPG; that's what sign-and-upload.sh is for.
- **Never force-push or amend published tags** — tags trigger GH Actions; amending breaks OIDC publishing.
- **`chown` in Docker**: `chown -R $(stat -c '%u:%g' /out) /out/` reads the bind-mount owner so output files belong to the host user, not root.
- **`gh release create` always needs `--repo dustinkirkland/byobu`** so it works from any working directory.
- **PyPI polling timeout**: if GH Actions hasn't finished in ~2.5 min, check https://github.com/dustinkirkland/byobu/actions — don't retry the tag.
- **Quilt build from git archive**: always generate the Ubuntu orig tarball via `git archive` to avoid `.venv`, `build/`, `dist/` contamination that causes `dpkg-source` to fail.
- **`ubuntu-distro-info --devel`** gives the current development codename; confirm it matches what's in debian/changelog's Ubuntu stanza.
- **RC version scheme**: `{BASE_VER}~rc{N}~{series}1` — strictly less than `{BASE_VER}~{series}1`, so the final release supersedes all RCs automatically.
- **Final PPA version scheme**: `{BASE_VER}~{series}1` — strictly less than any official Ubuntu or Debian upload, so the archive always supersedes the PPA.
- **Closing bugs automatically**:
  - Launchpad: add `- LP: #NNNNNN` in the debian/changelog entry (triggers on PPA or Ubuntu upload)
  - Debian BTS: add `- Closes: #NNNNNN` in the debian/changelog entry (triggers on Debian upload)
