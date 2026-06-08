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
    4  Smoke test          ┐
    5  PPA source builds   ├─ run in parallel
    5b Debian exp source   ┘
    6  GitHub pre-release
    7  Write sign-and-upload.sh

Final adds:
    6b Debian unstable source build (Docker) — RC uses experimental, final promotes to unstable
    6c Ubuntu dev-series source build (Docker)  ┐ run in parallel with 4 and 5b
    6d Homebrew formula update
    GitHub release (non-prerelease, both byobu and trustmux tags)
    8  Regenerate PWA screenshots → commit + push trustmux-web
"""

import argparse
import concurrent.futures
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

BYOBU_SRC = Path(__file__).resolve().parent.parent


# ── thread-local stdout (parallel output capture) ──────────────────────────
#
# Parallel phase threads set _tls.buf to a StringIO.  All print() calls and
# subprocess runs in that thread then write into the buffer instead of the
# real terminal.  The main thread replays each buffer in phase order after
# all parallel phases have joined, giving clean, interleave-free output.

_tls = threading.local()


class _TLSStdout:
    """sys.stdout shim: routes writes to a per-thread StringIO when set."""

    def __init__(self, real):
        self._real = real

    def _buf(self):
        return getattr(_tls, "buf", None)

    def write(self, s):
        b = self._buf()
        (b if b is not None else self._real).write(s)

    def flush(self):
        if self._buf() is None:
            self._real.flush()

    def isatty(self):
        return False if self._buf() is not None else self._real.isatty()

    def fileno(self):
        return self._real.fileno()


sys.stdout = _TLSStdout(sys.stdout)


# ── helpers ────────────────────────────────────────────────────────────────

def run(cmd, check=True, capture=False, **kwargs):
    kw = dict(check=check, text=True, **kwargs)
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return subprocess.run(cmd, shell=isinstance(cmd, str), **kw)
    # In a parallel phase thread, pipe subprocess output into the thread buffer
    # so it is replayed in-order with the rest of that phase's output.
    buf = getattr(_tls, "buf", None)
    if buf is not None:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        result = subprocess.run(cmd, shell=isinstance(cmd, str), **kw)
        buf.write(result.stdout or "")
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout)
        return result
    return subprocess.run(cmd, shell=isinstance(cmd, str), **kw)


def run_phases_parallel(labeled_fns):
    """Run phase functions concurrently; replay each section's captured output
    in-order after all phases complete.

    labeled_fns: list of (label, callable).  Callables must be thread-safe.
    A single-element list is run directly with no threading overhead.
    """
    if not labeled_fns:
        return
    if len(labeled_fns) == 1:
        labeled_fns[0][1]()
        return

    labels = [l for l, _ in labeled_fns]
    print(f"\n  ⟳ Launching in parallel: {', '.join(labels)}")

    outcomes = {}  # label → (output_str, exc_or_None)

    def _run_one(label, fn):
        buf = io.StringIO()
        _tls.buf = buf
        try:
            fn()
            outcomes[label] = (buf.getvalue(), None)
        except BaseException as exc:
            outcomes[label] = (buf.getvalue(), exc)
        finally:
            _tls.buf = None

    threads = [
        threading.Thread(target=_run_one, args=(l, f), name=l)
        for l, f in labeled_fns
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    any_failed = False
    for label in labels:
        out, exc = outcomes.get(label, ("", RuntimeError("phase did not run")))
        ok = exc is None
        marker = "✓" if ok else "✗"
        print(f"\n── {marker} {label} " + "─" * max(0, 55 - len(label)))
        if out.strip():
            for line in out.rstrip().splitlines():
                print(f"  {line}")
        if not ok:
            if isinstance(exc, SystemExit):
                print(f"  ✗ {label}: aborted (see stderr above)")
            else:
                print(f"  ✗ {label}: {exc}")
            any_failed = True

    if any_failed:
        die("One or more parallel phases failed (see output above).")


def die(msg):
    print(f"\n✗  {msg}", file=sys.stderr)
    sys.exit(1)


def confirm(prompt, skippable=False):
    """Prompt the user to proceed, skip, or abort.

    Returns True  — user chose to proceed (y/yes).
    Returns False — user chose to skip this step (s/skip); only when skippable=True.
    Calls die()   — user chose to abort (anything else).
    """
    opts = "[y/s/N]" if skippable else "[y/N]"
    ans = input(f"\n{prompt} {opts} ").strip().lower()
    if ans in ("y", "yes"):
        return True
    if skippable and ans in ("s", "skip"):
        print("  (skipped)")
        return False
    die("Aborted.")


def banner(msg):
    w = 62
    print(f"\n{'━' * w}\n  {msg}\n{'━' * w}")


def section(msg):
    print(f"\n── {msg} " + "─" * max(0, 58 - len(msg)))


# ── phase resumption ──────────────────────────────────────────────────────

# Canonical phase order (6b is an alias for 5b used in final-mode docs)
_PHASE_ORDER = ["2b", "3", "4", "5", "5b", "6c", "6d", "6", "7", "8"]

def _phase_idx(phase):
    if phase == "6b":
        phase = "5b"
    return _PHASE_ORDER.index(phase)

def should_run(phase, start_from):
    if start_from is None:
        return True
    return _phase_idx(phase) >= _phase_idx(start_from)


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

def determine_versions(mode, resume=False):
    section("Phase 2: Determine versions")

    # Guard: refuse to release if HEAD is an open-dev bump commit.
    # open-dev bumps the version and commits "bump version to X.Y and open for
    # development".  If the release pipeline runs while HEAD is that commit,
    # the release tag lands on the version-bump commit rather than on real
    # development work — which is what happened with trustmux-v7.8.
    head_msg = run(
        ["git", "-C", str(BYOBU_SRC), "log", "--format=%s", "-1"],
        capture=True,
    ).stdout.strip()
    if re.match(r"^bump version to .* and open for development$", head_msg):
        die(
            f"HEAD is an open-dev bump commit: '{head_msg}'\n"
            f"  Release tags must not land on a version-bump commit.\n"
            f"  Either:\n"
            f"    • Add development commits before running the release pipeline, or\n"
            f"    • Run the release pipeline BEFORE running 'open-dev'."
        )
    print(f"  HEAD: {head_msg[:60]}")

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

    # Final releases do not upload to the PPA; skip the slot check entirely.
    # Also skip on resume — slots are already occupied.
    if mode == "rc" and not resume:
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

            def dpkg_ge(v1, v2):
                return subprocess.run(
                    ["dpkg", "--compare-versions", v1, "ge", v2],
                    check=False, capture_output=True,
                ).returncode == 0

            # For each existing PPA version under base_ver, compute what our
            # per-series upload target would be and check if existing >= target.
            # This handles exact collisions and cross-scheme RC conflicts in one
            # authoritative pass.
            conflicting = []
            for e in all_entries:
                ev = e["source_package_version"]
                if not ev.startswith(f"{base_ver}~"):
                    continue
                m = re.search(r"([a-z]+)\d+$", ev)
                if not m:
                    continue
                target = f"{ppa_base}~{m.group(1)}1"
                if dpkg_ge(ev, target):
                    conflicting.append(ev)

            if conflicting:
                die(
                    f"PPA already has versions >= {ppa_base}~{{series}}1:\n"
                    f"  {' '.join(conflicting)}\n"
                    f"  Delete at https://launchpad.net/~byobu/+archive/ubuntu/ppa\n"
                    f"  then re-run."
                )

            print(f"  PPA slot {ppa_base}~{{series}}1 is free.")
        except urllib.error.URLError as e:
            print(f"  (Launchpad check skipped — network error: {e})")

    # Output directory
    outdir = Path(f"/tmp/byobu-release-{ppa_base}")
    if resume:
        if not outdir.exists():
            die(
                f"--start-from: output directory {outdir} not found.\n"
                f"  Run without --start-from first to create it."
            )
        # Ensure all subdirs exist (harmless if already there)
        (outdir / "debs").mkdir(exist_ok=True)
        (outdir / "debian").mkdir(exist_ok=True)
        if mode == "rc":
            (outdir / "ppa").mkdir(exist_ok=True)
        if mode == "final":
            (outdir / "ubuntu").mkdir(exist_ok=True)
        print(f"  Resuming in:  {outdir}")
    else:
        if outdir.exists():
            shutil.rmtree(outdir)
        (outdir / "debs").mkdir(parents=True)
        (outdir / "debian").mkdir()
        if mode == "rc":
            (outdir / "ppa").mkdir()
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
    if not confirm(f"Push git tag {tag} to origin?", skippable=True):
        print(f"  (tag push skipped — downstream phases will use existing tag state)")
        return
    # Idempotent: skip local creation if tag already exists (e.g. re-run after partial failure)
    local = run(["git", "-C", str(BYOBU_SRC), "tag", "--list", tag], capture=True)
    if tag not in local.stdout.split():
        run(["git", "-C", str(BYOBU_SRC), "tag", tag])
    else:
        print(f"  (local tag {tag} already exists — skipping creation)")
    # Skip push if tag already on remote
    remote = run(["git", "-C", str(BYOBU_SRC), "ls-remote", "--tags", "origin", tag], capture=True)
    if remote.stdout.strip():
        print(f"  (tag {tag} already on remote — skipping push)")
    else:
        run(["git", "-C", str(BYOBU_SRC), "push", "origin", tag])
    print(f"  ✓ Tag {tag} ready.")
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


# ── phase 2b: local binary build ─────────────────────────────────────────

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


# ── phase 5: PPA source builds (parallel per-series) ─────────────────────
#
# Each Ubuntu series gets its own Docker container so all series build
# concurrently.  Output files have distinct names (version includes the
# codename), so there is no contention on the shared /out volume.

_PPA_SERIES_SCRIPT = r"""
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

PPA_VER="${PPA_BASE}~${CODENAME}1"
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
chown -R $(stat -c '%u:%g' /out) /out/

echo "=== $CODENAME done ==="
"""


def build_ppa_packages(v, identity):
    section("Phase 5: PPA source builds (parallel, all series)")
    # Exclude the devel series — PPA uploads target stable/supported series only.
    ppa_series = [s for s in v["series"] if s != v["devel_series"]]
    print(f"  Series ({len(ppa_series)}): {' '.join(ppa_series)}")
    print(f"  Launching {len(ppa_series)} containers in parallel…")

    series_outputs = {}
    series_errors = {}

    def _build_one(codename):
        # Each worker thread gets its own capture buffer; the parent thread's
        # _tls.buf (if set, i.e. we're inside run_phases_parallel) is separate.
        buf = io.StringIO()
        prev = getattr(_tls, "buf", None)
        _tls.buf = buf
        try:
            run([
                "docker", "run", "--rm",
                "-v", f"{BYOBU_SRC}:/src:ro",
                "-v", f"{v['outdir']}/ppa:/out",
                "-e", f"DEBEMAIL={identity['DEBEMAIL']}",
                "-e", f"DEBFULLNAME={identity['DEBFULLNAME']}",
                "-e", f"PKG={v['pkg']}",
                "-e", f"BASE_VER={v['base_ver']}",
                "-e", f"PPA_BASE={v['ppa_base']}",
                "-e", f"CODENAME={codename}",
                "ubuntu:noble", "bash", "-c", _PPA_SERIES_SCRIPT,
            ])
            series_outputs[codename] = buf.getvalue()
        except Exception as exc:
            series_errors[codename] = (buf.getvalue(), exc)
        finally:
            _tls.buf = prev

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ppa_series)) as ex:
        futures = {ex.submit(_build_one, c): c for c in ppa_series}
        concurrent.futures.wait(futures)

    # Replay per-series output in deterministic order (writes to parent buf
    # if we are ourselves inside a parallel phase, or to real stdout otherwise).
    for codename in ppa_series:
        if codename in series_errors:
            out, exc = series_errors[codename]
            print(f"  ── {codename}: FAILED ({exc})")
            for line in out.rstrip().splitlines():
                print(f"     {line}")
        else:
            print(f"  ── {codename}: OK")
            out = series_outputs.get(codename, "")
            if out.strip():
                for line in out.rstrip().splitlines():
                    print(f"     {line}")

    if series_errors:
        die(f"PPA build failed for: {', '.join(series_errors)}")

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
git config --global --add safe.directory "$STAGING/src"
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

dpkg-buildpackage -S -us -uc -d -sa 2>&1 | tail -3

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
    if not confirm(
        f"Confirm GH Actions PyPI publish completed:\n"
        f"    https://github.com/dustinkirkland/byobu/actions\n"
        f"  trustmux {v['pypi_version']} should be live at:\n"
        f"    https://pypi.org/project/trustmux/{v['pypi_version']}/\n"
        f"  Continue to update Homebrew formula?",
        skippable=True,
    ):
        return
    print("  Polling PyPI for tarball (up to ~5 min)…")

    tarball_url = tarball_sha256 = None
    for attempt in range(20):
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
        print(f"  Attempt {attempt + 1}/20 — not ready, waiting 15s…")
        time.sleep(15)

    if not tarball_url:
        die(
            "Timed out waiting for PyPI tarball (20 attempts × 15s).\n"
            "  Check GH Actions: https://github.com/dustinkirkland/byobu/actions\n"
            "  Once the workflow completes, resume with:\n"
            "    python .maintainer/release.py final --start-from 6d"
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
            "gh", "release", "create", tag,
            "--repo", "dustinkirkland/byobu",
            "--title", f"Trustmux {v['pypi_version']}",
            "--notes", f"byobu {v['base_ver']} / trustmux {v['pypi_version']}",
        ])
        run([
            "gh", "release", "create", v["base_ver"],
            "--repo", "dustinkirkland/byobu",
            "--title", f"byobu {v['base_ver']}",
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
read -rp "  Upload all series to $PPA? [y=upload / N=skip] " ans
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

echo "── Step 3: Debian experimental (mentors.debian.net) ────────────────"
read -rp "  Upload to mentors.debian.net for sponsor review? [y=upload / N=skip] " ans < /dev/tty
if [[ "$ans" =~ ^[Yy]$ ]]; then
  dput mentors "$BASE/debian/byobu_{v['deb_exp_version']}_source.changes" \\
    || {{ echo "  ✗ dput failed — package not uploaded."; exit 1; }}
  echo ""
  echo "  Uploaded. Email Antoine <anarcat@debian.org> with:"
  echo "    Subject: byobu {v['deb_exp_version']} sponsorship request (experimental)"
  echo "    Body: https://mentors.debian.net/package/byobu"
else
  echo "  Skipped."
fi
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
         "$BASE"/ubuntu/*_source.changes; do
  [ -f "$f" ] || continue
  echo "  Signing: $f"
  debsign -k "$GPGKEY" "$f"
done
echo "All signed."
echo ""

echo "── Step 2: Ubuntu {v['devel_series']} (dev series) ──────────────────────────────"
read -rp "  Upload to Ubuntu {v['devel_series']}? [y=upload / N=skip] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  dput ubuntu "$BASE/ubuntu/byobu_{v['ubuntu_ver']}_source.changes"
else
  echo "  Skipped."
fi
echo ""

echo "── Step 3: Debian unstable (mentors.debian.net) ────────────────────"
read -rp "  Upload to mentors.debian.net for sponsor review? [y=upload / N=skip] " ans < /dev/tty
if [[ "$ans" =~ ^[Yy]$ ]]; then
  dput mentors "$BASE/debian/byobu_{v['deb_exp_version']}_source.changes" \\
    || {{ echo "  ✗ dput failed — package not uploaded."; exit 1; }}
  echo ""
  echo "  Uploaded. Email Antoine <anarcat@debian.org> with:"
  echo "    Subject: byobu {v['base_ver']} sponsorship request (unstable)"
  echo "    Body: https://mentors.debian.net/package/byobu"
else
  echo "  Skipped."
fi

echo ""
echo "=========================================="
echo " All uploads complete!"
echo " Next step: open the next development cycle"
echo ""
echo "   cd {BYOBU_SRC}"
echo "   ./.maintainer/release.py open-dev"
echo "=========================================="
"""

    script_path = outdir / "sign-and-upload.sh"
    script_path.write_text(body)
    script_path.chmod(0o755)
    print(f"  ✓ Written: {script_path}")


# ── phase 8: website screenshots (final only) ─────────────────────────────

def update_website_screenshots(v):
    """Regenerate PWA demo PNGs and push them to the trustmux-web repo."""
    import tempfile
    section("Phase 8: Regenerate website screenshots → trustmux-web")

    mobile_dir  = BYOBU_SRC / "mobile"
    venv_python = mobile_dir / ".venv" / "bin" / "python"
    gen_script  = mobile_dir / "generate_screenshots.py"

    if not venv_python.exists():
        die(
            f"Mobile venv not found at {venv_python}\n"
            "  Run: cd mobile && uv venv && uv pip install -e '.[dev]' cairosvg pillow"
        )
    if not gen_script.exists():
        die(f"generate_screenshots.py not found at {gen_script}")

    # Locate trustmux-web repo
    web_repo = None
    for candidate in [
        Path.home() / "src/trustmux-web",
        Path.home() / "trustmux-web",
        BYOBU_SRC.parent / "trustmux-web",
    ]:
        if (candidate / ".git").is_dir():
            web_repo = candidate
            break
    if not web_repo:
        die("trustmux-web repo not found. Clone it to ~/src/trustmux-web first.")

    with tempfile.TemporaryDirectory() as tmpdir:
        run([str(venv_python), str(gen_script), "--out-dir", tmpdir])
        shots_dir = web_repo / "screenshots"
        shots_dir.mkdir(exist_ok=True)
        for png in sorted(Path(tmpdir).glob("*.png")):
            shutil.copy2(png, shots_dir / png.name)
            print(f"  copied {png.name} → {shots_dir}/")

    run(["git", "-C", str(web_repo), "add", "screenshots/"])
    dirty = run(
        ["git", "-C", str(web_repo), "diff", "--cached", "--quiet"], check=False
    )
    if dirty.returncode != 0:
        run([
            "git", "-C", str(web_repo), "commit", "-m",
            f"Regenerate PWA screenshots for trustmux {v['pypi_version']}",
        ])
        run(["git", "-C", str(web_repo), "push"])
        print(f"  ✓ Screenshots updated and pushed to trustmux-web ({v['pypi_version']})")
    else:
        print("  (screenshots unchanged — nothing to push)")


# ── summary ───────────────────────────────────────────────────────────────

def print_summary(v, mode):
    outdir = v["outdir"]
    mode_label = "RC" if mode == "rc" else "Release"
    banner(f"{mode_label} complete: {v['pkg']} {v['ppa_base']}")
    deb_target = "experimental" if mode == "rc" else "unstable"
    lines = [
        f"\n  PyPI:  trustmux-v{v['pypi_version']} → GH Actions",
        f"         https://github.com/dustinkirkland/byobu/actions",
    ]
    if mode == "rc":
        lines += [
            f"  PPA:   ppa:byobu/ppa — {v['ppa_base']}~{{series}}1",
            f"         https://launchpad.net/~byobu/+archive/ubuntu/ppa",
        ]
    lines.append(f"  Debian: byobu {v['deb_exp_version']} → {deb_target}")
    print("\n".join(lines))
    if mode == "final":
        print(
            f"  Ubuntu: byobu {v['ubuntu_ver']} → {v['devel_series']}"
            f"\n          (files in {outdir}/ubuntu/)"
            f"\n  Homebrew: brew upgrade dustinkirkland/trustmux/trustmux"
            f"\n  Website: trustmux-web screenshots regenerated and pushed"
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
    parser.add_argument(
        "--start-from",
        metavar="PHASE",
        choices=_PHASE_ORDER + ["6b"],
        help=(
            "Resume from this phase, reusing the existing /tmp/byobu-release-* dir. "
            "Phases: 2b 3 4 5 5b(=6b) 6c 6d 6 7 8"
        ),
    )
    args = parser.parse_args()
    mode = args.mode
    start_from = args.start_from

    if mode == "open-dev":
        identity = load_identity()
        open_dev(identity)
        return

    banner(f"byobu/trustmux release pipeline — {mode.upper()}"
           + (f"  [resuming from phase {start_from}]" if start_from else ""))

    identity = load_identity()
    check_tools()
    tap_dir = find_homebrew_tap(mode) if should_run("6d", start_from) else None
    v = determine_versions(mode, resume=(start_from is not None))

    if should_run("2b", start_from):
        build_local_debs(v)
        debs = sorted((v["outdir"] / "debs").glob("*.deb"))
        if debs:
            install_cmd = "sudo dpkg -i " + " ".join(str(d) for d in debs)
            print(f"\n  Install locally:\n    {install_cmd}\n")
        confirm(f"Local .deb built and ready to test. Continue to tag trustmux-v{v['pypi_version']} on PyPI?")

    if should_run("3", start_from):
        push_pypi_tag(v)

    # ── phases 4 / 5 / 5b (RC) or 4 / 5b / 6c (final): run in parallel ────
    #
    # All three Docker builds are independent: each mounts /src read-only and
    # writes to a different output directory.  Running them concurrently saves
    # ~5 minutes on a typical RC run and ~6 minutes on a final release.
    parallel = []
    if should_run("4", start_from):
        parallel.append(("smoke test", run_smoke_test))
    if mode == "rc" and should_run("5", start_from):
        parallel.append(("PPA builds", lambda: build_ppa_packages(v, identity)))
    dist = "experimental" if mode == "rc" else "unstable"
    if should_run("5b", start_from):
        parallel.append(("Debian source", lambda: build_debian_source(v, identity, dist)))
    if mode == "final" and should_run("6c", start_from):
        parallel.append(("Ubuntu dev", lambda: build_ubuntu_dev(v, identity)))

    run_phases_parallel(parallel)

    if mode == "final":
        if should_run("6d", start_from):
            update_homebrew(v, tap_dir)

    if should_run("6", start_from):
        create_github_release(v, mode)

    if should_run("7", start_from):
        write_sign_and_upload(v, identity, mode)

    if mode == "final" and should_run("8", start_from):
        update_website_screenshots(v)

    print_summary(v, mode)


if __name__ == "__main__":
    main()
