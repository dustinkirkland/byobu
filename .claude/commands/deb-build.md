---
description: Build Debian packages for byobu/trustmux in a Docker clean room
---

When the user asks to build the deb, run the package build, or do a dpkg-buildpackage:

**Always build in a Docker clean room — never on the host.**

The clean-room approach catches missing Build-Depends early and produces reproducible packages regardless of what's installed on the dev machine.

## Steps

1. Check Docker is available:
```bash
docker --version
```

2. Read `Build-Depends` from `.maintainer/debian/control` to confirm what to install.
   (Note: `debian/` lives at `.maintainer/debian/` — it is copied into place inside Docker.)

3. Compute the RC version on the host and pass it into Docker:
```bash
BASE_VER=$(grep "AC_INIT" configure.ac | grep -oP '(?<=\[)\d+\.\d+(?=\])')
LAST_RC=$(git tag --list "trustmux-v${BASE_VER}rc*" | grep -oP '\d+$' | sort -n | tail -1)
NEXT_RC=$(( ${LAST_RC:-0} + 1 ))
LOCAL_VER="${BASE_VER}~rc${NEXT_RC}-1"
echo "Building: byobu_${LOCAL_VER}"
```

4. Run the build — mount the source tree read-only, write output to a temp dir:
```bash
mkdir -p /tmp/byobu-debs
rm -f /tmp/byobu-debs/*.deb
docker run --rm \
  -v /home/kirkland/src/byobu:/src:ro \
  -v /tmp/byobu-debs:/out \
  -e LOCAL_VER="$LOCAL_VER" \
  -e DEBEMAIL="dustin.kirkland@gmail.com" \
  -e DEBFULLNAME="Dustin Kirkland" \
  ubuntu:noble \
  bash -c "
    set -e
    apt-get update -qq
    apt-get install -y --no-install-recommends \
      build-essential dpkg-dev debhelper dh-python \
      gettext-base automake autoconf \
      python3 python3-all python3-tornado \
      devscripts bc \
      ca-certificates
    cp -a /src /build
    cp -a /build/.maintainer/debian /build/debian
    cd /build
    dch -v \"\$LOCAL_VER\" -b --distribution UNRELEASED --no-auto-nmu 'Local RC test build'
    DEB_BUILD_OPTIONS=parallel=1 dpkg-buildpackage -us -uc -b
    cp /build/../*.deb /out/
    echo '=== Built packages ==='
    ls -lh /out/*.deb
    chown -R \$(stat -c '%u:%g' /out) /out/
  "
```

**Why these packages:**
- `build-essential` — provides `make`, `gcc`, etc. (not implicit in Ubuntu minimal)
- `devscripts` — provides `dch` and `checkbashisms`
- `bc` — required by `byobu-ulevel` tests
- `DEB_BUILD_OPTIONS=parallel=1` — avoids a race condition in `make install` that fails if two jobs install the same file simultaneously

5. Report the resulting `.deb` files from `/tmp/byobu-debs/`.

6. If the build fails, show the relevant error section (not the full log).

## Notes

- `ubuntu:noble` = Ubuntu 24.04 LTS. Use `ubuntu:latest` if targeting the current dev release.
- `-us -uc` skips GPG signing (not needed for local test builds).
- `-b` builds binary packages only (no source package).
- The RC version (`7.14~rc5-1`) is computed from git tags on the host and injected via `$LOCAL_VER`. It matches what the release pipeline would assign as the next RC number, so test builds are clearly labeled and sort correctly below final releases in dpkg.
- `debian/` lives at `.maintainer/debian/` in the repo; it is copied into `/build/debian/` inside Docker before building.
- The test suite runs automatically as part of the build via `override_dh_auto_test` in `debian/rules` — 113 shell tests + 134 Python tests (247 total). A test failure fails the build.
- If the user wants to skip tests (e.g. to iterate on packaging only): add `DEB_BUILD_OPTIONS=nocheck` to the environment.
