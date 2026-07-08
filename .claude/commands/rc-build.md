---
description: Cut a byobu/trustmux RC via the codified release.py pipeline — PyPI, PPA, Debian experimental, GitHub pre-release, GPG sign + upload
---

All RC/final release logic is codified in `.maintainer/release.py`. This skill does not
reimplement any of it — earlier versions of this file duplicated the pipeline in bash and
drifted out of sync with `release.py` (notably: deriving the base version from
`debian/changelog`, which lives only on `salsa/debian/latest` and lags upstream between
releases — see `.maintainer/DEBIAN_PACKAGING_CONTEXT.md`). That produced a bad `7.15a1`
pre-release on 2026-07-08 that didn't match `release.py`'s `{base}rcN` scheme. Don't
reintroduce that duplication: if `release.py`'s behavior needs to change, change
`release.py`, not this file.

`release.py rc` (default mode) runs, in order:
```
1  Pre-flight checks
1b GPG pre-warm (sign a throwaway blob to unlock gpg-agent for phase 7)
2  Determine versions (base version from mobile/pyproject.toml / configure.ac)
3  Push PyPI git tag (triggers GH Actions → PyPI)
4  Smoke tests + local deb + Fedora RPM + Homebrew smoke tests + Salsa CI   ┐ parallel
5  PPA source builds                                                        │
5b Debian experimental source build                                        ┘
6  GitHub pre-release
7  GPG sign + upload (ppa:byobu/ppa, mentors.debian.net)
```
`final` mode additionally updates Homebrew, builds for the Ubuntu dev series, and pushes to
Debian Salsa. Full phase list and flags: `python3 .maintainer/release.py --help`.

## Do not run this via Bash

Phase 1b (`prewarm_gpg`) signs a throwaway file with the user's real GPG key to warm
`gpg-agent` before the long builds start — it blocks on a live pinentry prompt for the actual
passphrase, at the very beginning of the run, before anything else happens. There is no way to
supply that non-interactively, and running it through the Bash tool (no real TTY) will hang or
fail at the first phase. Later phases are similarly unsafe to automate: in non-interactive mode
(`release.py`'s default) `confirm()` auto-proceeds, so once GPG is unlocked phase 7 will
actually `dput` to the production PPA and mentors.debian.net without a human in the loop unless
a person is physically present to have typed the passphrase in the first place.

**Hand the command to the user and let them run it themselves** in their own terminal:

```bash
cd /home/kirkland/src/byobu && .maintainer/release.py rc
```

Add `--interactive` if they want a y/n prompt before each upload step instead of auto-proceed.

## What Claude can do beforehand

Read-only prep and sanity checks are fine — these don't touch GPG or upload anything:

- Confirm build identity is set: `grep -oP 'DEBEMAIL=\K\S+|DEBFULLNAME=\K.*|GPGKEY=\K\S+' ~/.bashrc`
- Confirm `configure.ac` and `mobile/pyproject.toml` agree on the current version
- Fetch and eyeball `salsa/debian/latest` sync: `git fetch salsa debian/latest`
- Check existing `trustmux-v*` tags (`git tag -l 'trustmux-v*' | sort -V | tail`) so the
  expected next RC number is known before the user kicks it off
- After the user's run completes, verify results (PyPI, GitHub release, PPA) and report back —
  but don't re-run any phase or touch GPG/upload yourself; if something needs redoing, tell the
  user which `--start-from PHASE` to use.

## After a validated RC

```bash
.maintainer/release.py final       # full release: Debian unstable, Ubuntu dev, Homebrew, Salsa push
.maintainer/release.py open-dev    # bump configure.ac/pyproject.toml, open next dev cycle
```

Both have the same GPG/interactivity constraints as `rc` — hand them to the user too.
