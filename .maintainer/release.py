#!/usr/bin/env python3
"""
release.py — byobu/trustmux release pipeline

Usage:
    ./release.py [rc]     # build and tag an RC (default)
    ./release.py final    # cut a full release

RC phases:
    1  Pre-flight checks
    2  Determine versions
    2b Local binary .deb build (test before tagging)
    3  Push PyPI git tag (triggers GH Actions)
    4  Smoke test (Docker)
    5  PPA source builds (Docker, all series)
    5b Debian experimental source build (Docker)
    6  GitHub pre-release
    7  Write sign-and-upload.sh

Final adds:
    6b Debian unstable source build (Docker) — RC uses experimental, final promotes to unstable
    6c Ubuntu dev-series source build (Docker)
    6d Homebrew formula update
    GitHub release (non-prerelease, both byobu and trustmux tags)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BYOBU_SRC = Path(__file__).resolve().parent.parent


# ── helpers ────────────────────────────────────────────────────────────────

def run(cmd, check=True, capture=False, **kwargs):
    kw = dict(check=check, text=True, **kwargs)
    if capture:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.PIPE
    return subprocess.run(cmd, shell=isinstance(cmd, str), **kw)


def die(msg):
    print(f"\n✗  {msg}", file=sys.stderr)
    sys.exit(1)


def confirm(prompt):
    ans = input(f"\n{prompt} [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        die("Aborted.")


def banner(msg):
    w = 62
    print(f"\n{'━' * w}\n  {msg}\n{'━' * w}")


def section(msg):
    print(f"\n── {msg} " + "─" * max(0, 58 - len(msg)))


# ── phase 1: pre-flight ────────────────────────────────────────────────────

def load_identity():
    section("Phase 1: Pre-flight checks")
    bashrc = Path("~/.bashrc").expanduser().read_text()

    def extract(key):
        m = re.search(rf"export {key}=['\"]?([^'\"#\n]+)['\"]?", bashrc)
        return m.group(1).strip() if m else ""

    identity = {
        "DEBEMAIL":    extract("DEBEMAIL"),
        "DEBFULLNAME": extract("DEBFULLNAME"),
        "GPGKEY":      extract("GPGKEY"),
    }
    for k, v in identity.items():
        print(f"  {k}={v}")

    if not all(identity.values()):
        die(
            "Missing identity in ~/.bashrc. Add:\n"
            "  export DEBFULLNAME='Your Name'\n"
            "  export DEBEMAIL='you@example.com'\n"
            "  export GPGKEY='<your GPG key fingerprint>'\n\n"
            "  Note: DEBEMAIL must match your Debian Developer keyring entry.\n"
            "  Debian ftp-master silently drops uploads with a mismatched address."
        )

    return identity


def check_tools():
    required = ["dput", "debsign", "git", "docker", "python3", "gh"]
    missing = [t for t in required if not shutil.which(t)]
    if missing:
        die(f"Missing tools: {' '.join(missing)}\n  sudo apt install devscripts dput gh")
    print(f"  Tools OK: {' '.join(required)}")


def find_homebrew_tap(mode):
    if mode == "rc":
        return None
    for d in [
        Path("/tmp/homebrew-trustmux"),
        Path.home() / "src/homebrew-trustmux",
        Path.home() / "homebrew-trustmux",
    ]:
        if (d / ".git").is_dir():
            print(f"  Homebrew tap: {d}")
            return d
    tap = Path("/tmp/homebrew-trustmux")
    run(["git", "clone", "git@github.com:dustinkirkland/homebrew-trustmux.git", str(tap)])
    return tap


# ── phase 2: versions ─────────────────────────────────────────────────────

def determine_versions(mode):
    section("Phase 2: Determine versions")

    # Canonical base version from debian/changelog
    changelog_line = (BYOBU_SRC / "debian/changelog").read_text().splitlines()[0]
    m = re.search(r"\(([^)~]+)", changelog_line)
    if not m:
        die(f"Cannot parse base version from: {changelog_line}")
    base_ver = m.group(1).strip()
    pkg = changelog_line.split()[0]
    print(f"  Package:      {pkg}")
    print(f"  Base version: {base_ver}")

    if mode == "rc":
        # RC number: next after the highest existing trustmux-v{base_ver}rcN git tag
        r = run(
            ["git", "-C", str(BYOBU_SRC), "tag", "--list", f"trustmux-v{base_ver}rc*"],
            capture=True,
        )
        existing = [
            int(m.group(1))
            for tag in r.stdout.splitlines()
            for m in [re.search(r"rc(\d+)$", tag)]
            if m
        ]
        rc_num = (max(existing) if existing else 0) + 1
        pypi_version = f"{base_ver}rc{rc_num}"
        # 0-prefix: 7.1~0rc1~noble1 < 7.1~noble1 in dpkg ordering
        ppa_base = f"{base_ver}~0rc{rc_num}"
        deb_exp_version = ppa_base
        ubuntu_ver = None
        print(f"  RC:           {rc_num}  →  trustmux-v{pypi_version}")
        print(f"  PPA base:     {ppa_base}~{{series}}1")
        print(f"  Debian exp:   {deb_exp_version}")
    else:
        pypi_version = base_ver
        ppa_base = base_ver
        deb_exp_version = base_ver
        ubuntu_ver = f"{base_ver}-0ubuntu1"
        print(f"  PyPI version: {pypi_version}")
        print(f"  PPA base:     {ppa_base}~{{series}}1")
        print(f"  Debian unstable: {deb_exp_version}")
        print(f"  Ubuntu:       {ubuntu_ver}")

    # Supported Ubuntu series
    try:
        r = run(["ubuntu-distro-info", "--supported"], capture=True, check=False)
        series = r.stdout.split() if r.returncode == 0 else []
        if not series:
            raise RuntimeError
    except (FileNotFoundError, RuntimeError):
        print("  (ubuntu-distro-info unavailable — querying Launchpad API)")
        d = json.loads(
            urllib.request.urlopen(
                "https://api.launchpad.net/1.0/ubuntu/series"
            ).read()
        )
        active = {"Active Development", "Current Stable Release", "Supported"}
        series = [
            e["name"]
            for e in d["entries"]
            if e["status"] in active and float(e.get("version") or "0") >= 22.04
        ]

    try:
        r = run(["ubuntu-distro-info", "--devel"], capture=True, check=False)
        devel_series = r.stdout.strip() if r.returncode == 0 else "stonking"
    except FileNotFoundError:
        devel_series = "stonking"

    print(f"  Series:       {' '.join(series)}")
    print(f"  Devel series: {devel_series}")

    # Check for PPA slot collision — also catch cross-scheme RC conflicts.
    # dpkg ordering: empty-string < letters, so "0rc13" < "rc3" even though
    # 13 > 3.  Any existing *rc* upload under this base_ver can block us.
    print("  Checking Launchpad for existing PPA slot…")
    try:
        base_url = (
            "https://api.launchpad.net/1.0/~byobu/+archive/ubuntu/ppa"
            "?ws.op=getPublishedSources&source_name=byobu&status="
        )
        all_entries = []
        for status in ("Published", "Pending"):
            d = json.loads(urllib.request.urlopen(base_url + status).read())
            all_entries += d.get("entries", [])

        # Exact slot collision (same ppa_base prefix)
        exact = [
            e["source_package_version"]
            for e in all_entries
            if e["source_package_version"].startswith(f"{ppa_base}~")
        ]
        if exact:
            die(
                f"PPA slot {ppa_base}~* already occupied: {' '.join(exact)}\n"
                "  Version detection may be stale — verify and bump manually."
            )

        # Cross-scheme RC collision: any *rc* version under this base_ver that
        # dpkg would consider >= our ppa_base (e.g. old "7.1~rc3" vs new "7.1~0rc13")
        if mode == "rc":
            rc_pattern = re.compile(
                rf"^{re.escape(base_ver)}~(?:0*rc\d+|[a-z]+rc\d+)~"
            )
            stale = [
                e["source_package_version"]
                for e in all_entries
                if rc_pattern.match(e["source_package_version"])
                and not e["source_package_version"].startswith(f"{ppa_base}~")
            ]
            if stale:
                die(
                    f"PPA contains RC packages from a different naming scheme:\n"
                    f"  {' '.join(stale)}\n"
                    f"  These may sort >= {ppa_base} in dpkg and block the upload.\n"
                    f"  Delete them at https://launchpad.net/~byobu/+archive/ubuntu/ppa\n"
                    f"  then re-run."
                )

        print(f"  PPA slot {ppa_base}~* is free.")
    except urllib.error.URLError as e:
        print(f"  (Launchpad check skipped — network error: {e})")

    # Output directory
    outdir = Path(f"/tmp/byobu-release-{ppa_base}")
    if outdir.exists():
        shutil.rmtree(outdir)
    (outdir / "ppa").mkdir(parents=True)
    (outdir / "debs").mkdir()
    (outdir / "debian").mkdir()
    if mode == "final":
        (outdir / "ubuntu").mkdir()
    print(f"  Output dir:   {outdir}")

    return dict(
        pkg=pkg,
        base_ver=base_ver,
        pypi_version=pypi_version,
        ppa_base=ppa_base,
        deb_exp_version=deb_exp_version,
        ubuntu_ver=ubuntu_ver,
        series=series,
        devel_series=devel_series,
        outdir=outdir,
    )


# ── phase 3: PyPI tag ─────────────────────────────────────────────────────

def push_pypi_tag(v):
    section("Phase 3: Push PyPI tag (triggers GH Actions → PyPI upload)")
    tag = f"trustmux-v{v['pypi_version']}"
    confirm(f"Push git tag {tag} to origin?")
    run(["git", "-C", str(BYOBU_SRC), "tag", tag])
    run(["git", "-C", str(BYOBU_SRC), "push", "origin", tag])
    print(f"  ✓ Tag {tag} pushed.")
    print("    Monitor: https://github.com/dustinkirkland/byobu/actions")


# ── phase 4: smoke test ───────────────────────────────────────────────────

_SMOKE_SCRIPT = r"""
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

echo "--- Build step ---"
dh build --with python3

echo "--- Test step ---"
bash usr/share/byobu/tests/test_byobu.sh
python3 -m unittest discover -s mobile/tests -v

echo "--- Install step ---"
dh install --with python3

echo "=== Smoke test PASSED ==="
"""


def run_smoke_test():
    section("Phase 4: Smoke test (Docker ubuntu:noble)")
    print("  This takes a few minutes…")
    run([
        "docker", "run", "--rm",
        "-v", f"{BYOBU_SRC}:/src:ro",
        "ubuntu:noble", "bash", "-c", _SMOKE_SCRIPT,
    ])
    print("  ✓ Smoke test PASSED")


# ── phase 4b: local binary build ─────────────────────────────────────────

_LOCAL_BUILD_SCRIPT = r"""
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
  build-essential dpkg-dev debhelper dh-python \
  gettext-base automake autoconf \
  python3 python3-all python3-tornado \
  devscripts bc ca-certificates 2>&1 | tail -5

cp -a /src /build
cd /build
DEB_BUILD_OPTIONS=parallel=1 dpkg-buildpackage -us -uc -b
cp /build/../*.deb /out/
echo ""
echo "=== Built packages ==="
ls -lh /out/*.deb
chown -R $(stat -c '%u:%g' /out) /out/
"""


def build_local_debs(v):
    section("Phase 2b: Local binary build (installable .deb files)")
    run([
        "docker", "run", "--rm",
        "-v", f"{BYOBU_SRC}:/src:ro",
        "-v", f"{v['outdir']}/debs:/out",
        "ubuntu:noble", "bash", "-c", _LOCAL_BUILD_SCRIPT,
    ])
    debs = sorted((v["outdir"] / "debs").glob("*.deb"))
    print(f"  ✓ {len(debs)} package(s) built:")
    for d in debs:
        print(f"    {d}")


# ── phase 5: PPA source builds ────────────────────────────────────────────

_PPA_SCRIPT = r"""
set -eo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
  build-essential dpkg-dev debhelper dh-python \
  gettext-base automake autoconf \
  python3 python3-all python3-tornado \
  devscripts bc ca-certificates distro-info git 2>&1 | tail -5

SERIES=$(ubuntu-distro-info --supported | tr "\n" " ")
echo "Building for: $DEBFULLNAME <$DEBEMAIL>"
echo "Series: $SERIES"

SRCDIR=$(mktemp -d)
git config --global --add safe.directory /src
git -C /src archive --format=tar HEAD | tar -x -C "$SRCDIR" -f -

for CODENAME in $SERIES; do
  PPA_VER="${PPA_BASE}~${CODENAME}1"
  echo ""
  echo "=== Building $PPA_VER ==="

  BUILDDIR=$(mktemp -d)
  cp -a "$SRCDIR" "$BUILDDIR/${PKG}-${PPA_VER}"
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

  cp -v "$BUILDDIR"/*.changes "$BUILDDIR"/*.dsc \
        "$BUILDDIR"/*.tar.* "$BUILDDIR"/*.buildinfo /out/ 2>/dev/null || true

  cd /
  rm -rf "$BUILDDIR"
done

rm -rf "$SRCDIR"
echo ""
echo "=== All series built ==="
ls -lh /out/
chown -R $(stat -c '%u:%g' /out) /out/
"""


def build_ppa_packages(v, identity):
    section("Phase 5: PPA source builds (Docker, all series)")
    run([
        "docker", "run", "--rm",
        "-v", f"{BYOBU_SRC}:/src:ro",
        "-v", f"{v['outdir']}/ppa:/out",
        "-e", f"DEBEMAIL={identity['DEBEMAIL']}",
        "-e", f"DEBFULLNAME={identity['DEBFULLNAME']}",
        "-e", f"PKG={v['pkg']}",
        "-e", f"BASE_VER={v['base_ver']}",
        "-e", f"PPA_BASE={v['ppa_base']}",
        "ubuntu:noble", "bash", "-c", _PPA_SCRIPT,
    ])
    changes = sorted((v["outdir"] / "ppa").glob("*.changes"))
    if not changes:
        die(f"PPA build produced no .changes files in {v['outdir']}/ppa/\n"
            "  Check Docker output above for dpkg-buildpackage errors.")
    print(f"  ✓ PPA source packages built ({len(changes)} series)")
    for f in changes:
        print(f"    {f.name}")


# ── phase 5b/6b: Debian source build (experimental for RC, unstable for final) ──

_DEB_SOURCE_SCRIPT = r"""
set -eo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
  build-essential dpkg-dev debhelper dh-python \
  gettext-base automake autoconf \
  python3 python3-all python3-tornado \
  devscripts bc ca-certificates git 2>&1 | tail -5

SRCDIR=$(mktemp -d)
git config --global --add safe.directory /src
git -C /src archive --format=tar HEAD | tar -x -C "$SRCDIR" -f -

BUILDDIR=$(mktemp -d)
cp -a "$SRCDIR" "$BUILDDIR/${PKG}-${DEB_EXP_VERSION}"
cd "$BUILDDIR/${PKG}-${DEB_EXP_VERSION}"

echo "3.0 (native)" > debian/source/format

# Set versioned entry; change UNRELEASED → target distribution
sed -i "1s/^${PKG} ([^)]*)/${PKG} (${DEB_EXP_VERSION})/" debian/changelog
sed -i "1s/) UNRELEASED;/) ${DEB_DIST};/" debian/changelog

dpkg-buildpackage -S -us -uc -d 2>&1 | tail -3

cp -v "$BUILDDIR"/*.changes "$BUILDDIR"/*.dsc \
      "$BUILDDIR"/*.tar.* "$BUILDDIR"/*.buildinfo /out/ 2>/dev/null || true
chown -R $(stat -c '%u:%g' /out) /out/

echo "=== Debian ${DEB_DIST} source package built ==="
ls -lh /out/
"""


def build_debian_source(v, identity, dist):
    section(f"Phase 5b: Debian {dist} source build (Docker)")
    run([
        "docker", "run", "--rm",
        "-v", f"{BYOBU_SRC}:/src:ro",
        "-v", f"{v['outdir']}/debian:/out",
        "-e", f"DEBEMAIL={identity['DEBEMAIL']}",
        "-e", f"DEBFULLNAME={identity['DEBFULLNAME']}",
        "-e", f"PKG={v['pkg']}",
        "-e", f"BASE_VER={v['base_ver']}",
        "-e", f"DEB_EXP_VERSION={v['deb_exp_version']}",
        "-e", f"DEB_DIST={dist}",
        "ubuntu:noble", "bash", "-c", _DEB_SOURCE_SCRIPT,
    ])
    changes = sorted((v["outdir"] / "debian").glob("*.changes"))
    if not changes:
        die(f"Debian {dist} build produced no .changes files in {v['outdir']}/debian/\n"
            "  Check Docker output above for dpkg-buildpackage errors.")
    print(f"  ✓ Debian {dist} source package built")
    for f in changes:
        print(f"    {f.name}")


# ── phase 6c: Ubuntu dev series (final only) ─────────────────────────────

_UBUNTU_SCRIPT = r"""
set -eo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
  build-essential dpkg-dev debhelper dh-python \
  gettext-base automake autoconf \
  python3 python3-all python3-tornado \
  devscripts bc ca-certificates git 2>&1 | tail -5

STAGING=$(mktemp -d)
cp -a /src "$STAGING/src"

BUILDDIR=$(mktemp -d)

# Use git archive to avoid .venv / build/ / dist/ contamination
cd "$STAGING/src"
git archive --format=tar.gz --prefix="${PKG}-${BASE_VER}/" HEAD \
  -o "$BUILDDIR/${PKG}_${BASE_VER}.orig.tar.gz"

mkdir "$BUILDDIR/${PKG}-${BASE_VER}"
tar -xzf "$BUILDDIR/${PKG}_${BASE_VER}.orig.tar.gz" \
    -C "$BUILDDIR/${PKG}-${BASE_VER}" --strip-components=1

cd "$BUILDDIR/${PKG}-${BASE_VER}"
[ ! -d debian ] && cp -a "$STAGING/src/debian" .
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

cp -v "$BUILDDIR"/*.changes "$BUILDDIR"/*.dsc \
      "$BUILDDIR"/*.tar.* "$BUILDDIR"/*.buildinfo /out/ 2>/dev/null || true
chown -R $(stat -c '%u:%g' /out) /out/

echo "=== Ubuntu ${DEVEL_SERIES} source package built ==="
ls -lh /out/
"""


def build_ubuntu_dev(v, identity):
    section(f"Phase 6c: Ubuntu {v['devel_series']} source build (Docker)")
    run([
        "docker", "run", "--rm",
        "-v", f"{BYOBU_SRC}:/src:ro",
        "-v", f"{v['outdir']}/ubuntu:/out",
        "-e", f"DEBEMAIL={identity['DEBEMAIL']}",
        "-e", f"DEBFULLNAME={identity['DEBFULLNAME']}",
        "-e", f"PKG={v['pkg']}",
        "-e", f"BASE_VER={v['base_ver']}",
        "-e", f"UBUNTU_VER={v['ubuntu_ver']}",
        "-e", f"DEVEL_SERIES={v['devel_series']}",
        "ubuntu:noble", "bash", "-c", _UBUNTU_SCRIPT,
    ])
    changes = sorted((v["outdir"] / "ubuntu").glob("*.changes"))
    if not changes:
        die(f"Ubuntu {v['devel_series']} build produced no .changes files in {v['outdir']}/ubuntu/\n"
            "  Check Docker output above for dpkg-buildpackage errors.")
    print(f"  ✓ Ubuntu {v['devel_series']} source package built: {v['ubuntu_ver']}")
    for f in changes:
        print(f"    {f.name}")
    print(f"    Output dir: {v['outdir']}/ubuntu/")


# ── phase 6d: Homebrew (final only) ──────────────────────────────────────

def update_homebrew(v, tap_dir):
    section("Phase 6d: Homebrew formula update")
    print("  Polling PyPI for tarball (up to ~150s)…")

    tarball_url = tarball_sha256 = None
    for attempt in range(10):
        try:
            url = f"https://pypi.org/pypi/trustmux/{v['pypi_version']}/json"
            d = json.loads(urllib.request.urlopen(url).read())
            for u in d["urls"]:
                if u["filename"].endswith(".tar.gz"):
                    tarball_url = u["url"]
                    tarball_sha256 = u["digests"]["sha256"]
                    break
            if tarball_url:
                break
        except Exception:
            pass
        print(f"  Attempt {attempt + 1}/10 — not ready, waiting 15s…")
        time.sleep(15)

    if not tarball_url:
        die(
            "Timed out waiting for PyPI.\n"
            "  Check https://github.com/dustinkirkland/byobu/actions before retrying."
        )

    print(f"  URL:    {tarball_url}")
    print(f"  SHA256: {tarball_sha256}")

    formula_path = tap_dir / "Formula/trustmux.rb"
    formula = formula_path.read_text()
    formula = re.sub(
        r'^  url "https://files\.pythonhosted\.org/[^"]*"',
        f'  url "{tarball_url}"',
        formula, count=1, flags=re.MULTILINE,
    )
    formula = re.sub(
        r'^  sha256 "[a-f0-9]+"',
        f'  sha256 "{tarball_sha256}"',
        formula, count=1, flags=re.MULTILINE,
    )
    formula = re.sub(
        r'^  version "[^"]*"',
        f'  version "{v["pypi_version"]}"',
        formula, count=1, flags=re.MULTILINE,
    )
    formula_path.write_text(formula)

    run(["git", "-C", str(tap_dir), "pull", "--ff-only"])
    run(["git", "-C", str(tap_dir), "add", "Formula/trustmux.rb"])
    run(["git", "-C", str(tap_dir), "commit", "-m", f"trustmux: update to {v['pypi_version']}"])
    try:
        run(["git", "-C", str(tap_dir), "push", "origin", "main"])
    except subprocess.CalledProcessError:
        run(["git", "-C", str(tap_dir), "push", "origin", "master"])

    print("  ✓ Homebrew formula updated and pushed")
    print("    brew upgrade dustinkirkland/trustmux/trustmux")


# ── github release ────────────────────────────────────────────────────────

def create_github_release(v, mode):
    section("Phase 6: GitHub release" if mode == "rc" else "Phase 7: GitHub release")
    tag = f"trustmux-v{v['pypi_version']}"

    if mode == "rc":
        run([
            "gh", "release", "create", tag,
            "--repo", "dustinkirkland/byobu",
            "--title", f"Trustmux {v['pypi_version']} (RC)",
            "--prerelease",
            "--notes",
            f"Release candidate.\n\n"
            f"pip install trustmux=={v['pypi_version']}\n"
            f"PPA: ppa:byobu/ppa  ({v['ppa_base']}~{{series}}1)",
        ])
    else:
        run([
            "gh", "release", "create", v["base_ver"],
            "--repo", "dustinkirkland/byobu",
            "--title", f"byobu {v['base_ver']}",
            "--notes", f"byobu {v['base_ver']} / trustmux {v['pypi_version']}",
        ])
        run([
            "gh", "release", "create", tag,
            "--repo", "dustinkirkland/byobu",
            "--title", f"Trustmux {v['pypi_version']}",
            "--notes", f"byobu {v['base_ver']} / trustmux {v['pypi_version']}",
        ])

    print("  ✓ GitHub release created")


# ── sign-and-upload script ────────────────────────────────────────────────

def write_sign_and_upload(v, identity, mode):
    section("Phase 7: Write sign-and-upload.sh" if mode == "rc" else "Phase 8: Write sign-and-upload.sh")
    outdir = v["outdir"]
    gpgkey = identity["GPGKEY"]

    if mode == "rc":
        body = f"""\
#!/bin/bash
set -e
GPGKEY="${{GPGKEY:-{gpgkey}}}"
PPA="ppa:byobu/ppa"
BASE="$(dirname "$0")"

echo "=========================================="
echo " byobu {v['ppa_base']} sign-and-upload (RC)"
echo " GPG key: $GPGKEY"
echo "=========================================="
echo ""

echo "── Step 1: GPG signing ─────────────────────────────────────────────"
for f in "$BASE"/ppa/*_source.changes \\
         "$BASE"/debian/*_source.changes; do
  [ -f "$f" ] || continue
  echo "  Signing: $f"
  debsign -k "$GPGKEY" "$f"
done
echo "All signed."
echo ""

echo "── Step 2: PPA ppa:byobu/ppa ────────────────────────────────────────"
read -rp "  Upload all series to $PPA? [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  for f in "$BASE"/ppa/*_source.changes; do
    echo "  dput $PPA $f"
    dput "$PPA" "$f"
  done
  echo "Done. Monitor: https://launchpad.net/~byobu/+archive/ubuntu/ppa"
else
  echo "  Skipped."
fi
echo ""

echo "── Step 3: Debian experimental ─────────────────────────────────────"
read -rp "  Upload to Debian experimental (ftp-master)? [y/N] " ans
[[ "$ans" =~ ^[Yy]$ ]] && \\
  dput ftp-master "$BASE/debian/byobu_{v['deb_exp_version']}_source.changes" || \\
  echo "  Skipped."
echo "  Monitor: https://ftp-master.debian.org/new.html"
"""
    else:
        body = f"""\
#!/bin/bash
set -e
GPGKEY="${{GPGKEY:-{gpgkey}}}"
BASE="{outdir}"

echo "=========================================="
echo " byobu {v['base_ver']} sign-and-upload"
echo " GPG key: $GPGKEY"
echo "=========================================="
echo ""

echo "── Step 1: GPG signing ─────────────────────────────────────────────"
for f in "$BASE"/debian/*_source.changes \\
         "$BASE"/ubuntu/*_source.changes \\
         "$BASE"/ppa/*_source.changes; do
  [ -f "$f" ] || continue
  echo "  Signing: $f"
  debsign -k "$GPGKEY" "$f"
done
echo "All signed."
echo ""

echo "── Step 2: Ubuntu {v['devel_series']} (dev series) ──────────────────────────────"
read -rp "  Upload to Ubuntu {v['devel_series']}? [y/N] " ans
[[ "$ans" =~ ^[Yy]$ ]] && \\
  dput ubuntu "$BASE/ubuntu/byobu_{v['ubuntu_ver']}_source.changes" || \\
  echo "  Skipped."
echo ""

echo "── Step 3: PPA ppa:byobu/ppa ────────────────────────────────────────"
read -rp "  Upload all series to ppa:byobu/ppa? [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  for f in "$BASE"/ppa/*_source.changes; do
    echo "  dput ppa:byobu/ppa $f"
    dput ppa:byobu/ppa "$f"
  done
  echo "Done. Monitor: https://launchpad.net/~byobu/+archive/ubuntu/ppa"
else
  echo "  Skipped."
fi
echo ""

echo "── Step 4: Debian unstable ──────────────────────────────────────────"
read -rp "  Upload to Debian unstable (ftp-master)? [y/N] " ans
[[ "$ans" =~ ^[Yy]$ ]] && \\
  dput ftp-master "$BASE/debian/byobu_{v['deb_exp_version']}_source.changes" || \\
  echo "  Skipped."
echo "  Monitor: https://ftp-master.debian.org/new.html"
"""

    script_path = outdir / "sign-and-upload.sh"
    script_path.write_text(body)
    script_path.chmod(0o755)
    print(f"  ✓ Written: {script_path}")


# ── summary ───────────────────────────────────────────────────────────────

def print_summary(v, mode):
    outdir = v["outdir"]
    mode_label = "RC" if mode == "rc" else "Release"
    banner(f"{mode_label} complete: {v['pkg']} {v['ppa_base']}")
    deb_target = "experimental" if mode == "rc" else "unstable"
    print(
        f"\n  PyPI:  trustmux-v{v['pypi_version']} → GH Actions"
        f"\n         https://github.com/dustinkirkland/byobu/actions"
        f"\n  PPA:   ppa:byobu/ppa — {v['ppa_base']}~{{series}}1"
        f"\n         https://launchpad.net/~byobu/+archive/ubuntu/ppa"
        f"\n  Debian: byobu {v['deb_exp_version']} → {deb_target}"
    )
    if mode == "final":
        print(
            f"  Ubuntu: byobu {v['ubuntu_ver']} → {v['devel_series']}"
            f"\n          (files in {outdir}/ubuntu/)"
            f"\n  Homebrew: brew upgrade dustinkirkland/trustmux/trustmux"
        )
    debs = sorted((outdir / "debs").glob("*.deb"))
    if debs:
        print(f"\n  Local install:")
        print(f"    sudo dpkg -i " + " ".join(str(d) for d in debs))

    print(
        f"\n  Sign and upload:\n    {outdir}/sign-and-upload.sh"
        f"\n  (GPG prompts once per series)\n"
    )
    if mode == "final":
        print("  Next: run ./release.py open-dev to bump version and open development.\n")


# ── open-dev (post-final) ─────────────────────────────────────────────────

def open_dev(identity):
    """Bump configure.ac + debian/changelog to next minor version after a final release."""
    banner("open-dev: bump to next development version")

    configure_ac = BYOBU_SRC / "configure.ac"
    cur = re.search(r"AC_INIT\(\[byobu\], \[([^\]]+)\]", configure_ac.read_text())
    if not cur:
        die("Cannot find AC_INIT version in configure.ac")
    current_ver = cur.group(1).strip()
    major, minor = current_ver.split(".")[:2]
    next_ver = f"{major}.{int(minor) + 1}"
    print(f"  {current_ver}  →  {next_ver}")

    text = configure_ac.read_text()
    text = text.replace(
        f"AC_INIT([byobu], [{current_ver}]",
        f"AC_INIT([byobu], [{next_ver}]",
    )
    configure_ac.write_text(text)

    env = {**os.environ,
           "DEBEMAIL": identity["DEBEMAIL"],
           "DEBFULLNAME": identity["DEBFULLNAME"]}
    run(
        ["dch", "--newversion", next_ver,
         "--distribution", "UNRELEASED", "--urgency", "medium",
         f"Open {next_ver} for development"],
        cwd=str(BYOBU_SRC), env=env,
    )

    print("\n  debian/changelog top:")
    for line in (BYOBU_SRC / "debian/changelog").read_text().splitlines()[:6]:
        print(f"    {line}")

    run(["git", "-C", str(BYOBU_SRC), "add", "configure.ac", "debian/changelog"])
    run(["git", "-C", str(BYOBU_SRC), "commit",
         "-m", f"bump version to {next_ver} and open for development"])
    print(f"  ✓ Committed: bump version to {next_ver}")


# ── main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="byobu/trustmux release pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Modes: rc (default), final, open-dev",
    )
    parser.add_argument(
        "mode", nargs="?",
        choices=["rc", "final", "open-dev"],
        default="rc",
    )
    args = parser.parse_args()
    mode = args.mode

    if mode == "open-dev":
        identity = load_identity()
        open_dev(identity)
        return

    banner(f"byobu/trustmux release pipeline — {mode.upper()}")

    identity = load_identity()
    check_tools()
    tap_dir = find_homebrew_tap(mode)
    v = determine_versions(mode)
    build_local_debs(v)
    debs = sorted((v["outdir"] / "debs").glob("*.deb"))
    if debs:
        install_cmd = "sudo dpkg -i " + " ".join(str(d) for d in debs)
        print(f"\n  Install locally:\n    {install_cmd}\n")
    confirm(f"Local .deb built and ready to test. Continue to tag trustmux-v{v['pypi_version']} on PyPI?")
    push_pypi_tag(v)
    run_smoke_test()
    build_ppa_packages(v, identity)
    build_debian_source(v, identity, "experimental" if mode == "rc" else "unstable")

    if mode == "final":
        build_ubuntu_dev(v, identity)
        update_homebrew(v, tap_dir)

    create_github_release(v, mode)
    write_sign_and_upload(v, identity, mode)
    print_summary(v, mode)


if __name__ == "__main__":
    main()
