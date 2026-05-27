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

2. Read `Build-Depends` from `debian/control` to confirm what to install.

3. Run the build — mount the source tree read-only, write output to a temp dir:
```bash
# From the byobu source root
docker run --rm \
  -v "$(pwd)":/src:ro \
  -v /tmp/byobu-debs:/out \
  ubuntu:noble \
  bash -c "
    set -e
    apt-get update -qq
    apt-get install -y --no-install-recommends \
      dpkg-dev debhelper dh-python \
      gettext-base automake autoconf \
      python3 python3-all python3-tornado \
      checkbashisms \
      ca-certificates
    cp -a /src /build
    cd /build
    dpkg-buildpackage -us -uc -b
    cp /build/../*.deb /out/
    echo '=== Built packages ==='
    ls -lh /out/*.deb
  "
```

4. Report the resulting `.deb` files from `/tmp/byobu-debs/`.

5. If the build fails, show the relevant error section (not the full log).

## Notes

- `ubuntu:noble` = Ubuntu 24.04 LTS. Use `ubuntu:latest` if targeting the current dev release.
- `-us -uc` skips GPG signing (not needed for local test builds).
- `-b` builds binary packages only (no source package).
- The test suite runs automatically as part of the build via `override_dh_auto_test` in `debian/rules` — 113 shell tests + 134 Python tests (247 total). A test failure fails the build.
- If the user wants to skip tests (e.g. to iterate on packaging only): add `DEB_BUILD_OPTIONS=nocheck` to the environment.
