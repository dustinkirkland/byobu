# Byobu Debian Packaging — Context & Status

## Goal

Dustin Kirkland (upstream byobu maintainer) is working toward Debian Maintainer
(DM) privileges for byobu on Salsa.  Andreas Tille is mentoring the process.
Andreas uploaded byobu 7.13-1 to Debian unstable on 2026-06-12.

## Key People

- **Dustin Kirkland** — upstream maintainer, seeking DM privileges
- **Andreas Tille** — Debian Developer, sponsor/mentor, uploaded 7.13-1
- Andreas's email triggered this whole cleanup (sent ~2026-06-12)

## Current model (as of 2026-07-06)

**`debian/` does not exist anywhere in this repo.** `salsa/debian/latest` is
its one and only source of truth. Andreas's words: *"The layout of the
debian/latest branch should be exactly the upstream branch + a debian/ dir."*
Nothing hidden, nothing stashed, nothing duplicated.

Earlier attempts got this wrong twice:

1. `debian/` at the repo root on GitHub master — a second source of truth
   alongside Salsa.
2. `debian/` moved to `.maintainer/debian/` and hidden from upstream tarballs
   via `.gitattributes` `export-ignore` — except `export-ignore` only affects
   `git archive`, not `git push`, so `push_salsa()` pushing `HEAD` straight to
   `salsa/debian/latest` put `.maintainer/debian/` (and `.maintainer/`,
   `.claude/`, `.github/`) onto that branch instead of a clean root-level
   `debian/`. Andreas caught this after the 7.14 release.

The fix: `.maintainer/debian/` was deleted outright (diffed clean against
`salsa/debian/latest` first — nothing valuable was only in our stash) and
`release.py` was reworked so this repo never writes to `debian/` again:

- **`prepare_debian()`** fetches `debian/` fresh from `salsa/debian/latest`
  (`git fetch salsa debian/latest` + `git archive salsa/debian/latest debian`
  extracted to `BYOBU_SRC/debian`, gitignored) before any local/PPA/Ubuntu
  build, and `cleanup_debian()` removes it afterwards. This fetch happens
  once, on the dev machine (which has Salsa access); the resulting `debian/`
  is then baked into whatever gets built/uploaded, so Launchpad, the Ubuntu
  build farm, and Docker containers never need their own path to Salsa.
- **`determine_versions()`** and **`open_dev()`** get the upstream version
  from `configure.ac`'s `AC_INIT` only (`read_configure_ac_version()`) —
  never from a changelog, since there is no local changelog anymore.
- **`push_salsa()`** (Phase 9) no longer pushes anything resembling `HEAD` to
  `debian/latest`. It generates the release tarball, then in Phase 9b runs
  `gbp import-orig --upstream-branch=upstream/latest --debian-branch=debian/latest`
  (its **default merge**, not `--no-merge`) in a temp Salsa clone — gbp's
  standard workflow imports the tarball into `upstream/latest` and merges it
  into `debian/latest`, bringing new upstream source onto that branch without
  touching `debian/` (upstream commits never contain it). Both branches are
  pushed with a plain non-force `git push`, which fails loudly instead of
  clobbering Salsa if it moved concurrently.
- **`check_salsa_sync()`** (Phase 1 preflight) dropped the old "salsa/debian/latest
  must be an ancestor of local HEAD" check — that assumed we pushed HEAD
  directly, which is no longer true. It now just confirms Salsa is reachable
  and that `debian/latest` / `upstream/latest` both exist.
- If we ever need to change packaging content itself (control, rules,
  changelog, etc.), that happens directly against `salsa/debian/latest` —
  never through this repo's automation.
- **`pristine-tar = True`** in `gbp.conf` (was `False` and unused since
  7.10 — the branch existed but every import since then skipped it).
  `push_salsa()` now fetches/pushes `pristine-tar` alongside the other two
  branches, and pushes the `upstream/<version>` tag `gbp import-orig`
  creates locally (previously only branches were pushed, so these tags
  were silently discarded with the temp clone since 7.10 — backfilled
  `upstream/7.14` when this was fixed). `run_salsa_ci()`'s local Docker
  simulation fetches the real `pristine-tar` branch from Salsa as a
  bundle so it actually exercises this instead of silently missing it.
- **Considered, rejected: `gbp import-orig --uscan`.** Otto's guide uses
  it, but `debian/watch`'s `Github` template can't distinguish byobu's
  own RC pre-release tags/GitHub prereleases from real releases (checked
  the `Devscripts::Uscan::Templates::Github` source — `releaseonly`
  doesn't filter on GitHub's `prerelease` flag in devscripts 2.26.x). It
  picked `trustmux-v7.14rc6` as "newer" than `7.14`. Kept the deterministic
  `git archive`-on-release-tag approach instead.

### RC numbering is unaffected

`rcN` was never derived from `debian/changelog` — it's computed fresh each
run by scanning existing `trustmux-v{base_ver}rcN` git tags in this repo and
taking `max + 1` (`determine_versions()`, `.maintainer/release.py`). The
actual version strings used for Debian/Ubuntu builds (`ppa_base`,
`deb_exp_version`) are synthesized into a brand-new changelog stanza inside
the Docker build scripts at build time (via `printf`/`dch`) — never read from
or written to a persisted file. `configure.ac` only ever holds the clean
upstream version (e.g. `7.15`), never an `rcN` suffix.

## History

### DEP-14 (https://dep-team.pages.debian.net/deps/dep14/)

Branch naming for Salsa packaging repo:
- `debian/latest`    — packaging branch (was `master`)
- `upstream/latest`  — imported upstream tarballs (was `upstream`)
- `pristine-tar`     — already correct
- Release tags: `debian/7.13-1` style (Andreas already does this ✓)

DEP-14 native upstream exception: Dustin's GitHub `master` needs no
vendor-prefixed branches — that's already DEP-14 compliant as-is.

### Otto's Guide (https://optimizedbyotto.com/post/debian-packaging-from-git/)

The upstream repo should have **no** top-level `debian/` directory — not
even hidden away. Salsa's `debian/latest` branch is where `debian/` lives,
on top of `upstream/latest`. New upstream versions are imported via
`gbp import-orig`, which is also how new upstream source reaches
`debian/latest` (its default merge behavior).

### What Was Wrong (Original Approach, pre-2026-06)

1. **Force-pushing to salsa/master** — silently wiped Andreas's changelog
   fixes, lintian-override cleanup, salsa-ci.yml edits, etc.
2. **Stacking UNRELEASED changelog stanzas** across dev cycles.
3. **Wrong branch names in gbp.conf** (`master` instead of DEP-14 names).
4. **`debian/` at the top level of the GitHub upstream repo.**
5. **Watch file overlay dance** — fragile, root cause of several bugs.

### Andreas's Guidance

"Keep upstream development on GitHub. Follow what Otto explained." He
explicitly endorsed the Otto model, then later caught the `.maintainer/debian`
hiding trick as still violating it — see "Current model" above.

## Key File Locations

```
/home/kirkland/src/byobu/
  .maintainer/
    release.py              — release pipeline; prepare_debian()/cleanup_debian()
                               fetch/remove a temp debian/ from Salsa per-build
  .gitignore                — /debian/ at root is gitignored (temp build copy only)
```

`debian/` never appears in this repo's git history going forward.

## Branch Summary

| Branch | Location | Status |
|--------|----------|--------|
| `master` | GitHub | upstream + packaging tooling, no `debian/` anywhere |
| `debian/latest` | Salsa | sole source of truth for `debian/`; maintained directly there |
| `upstream/latest` | Salsa | seeded per-release via `gbp import-orig` |
| `pristine-tar` | Salsa | `pristine-tar = True`; delta backfilled through 7.14 |

## gbp.conf (on salsa/debian/latest)

```ini
[DEFAULT]
upstream-branch = upstream/latest
debian-branch = debian/latest
pristine-tar = True

[buildpackage]
sign-tags = False
```

`upstream-tree = HEAD` from the old `.maintainer/debian/gbp.conf` is gone —
it doesn't apply now that `gbp.conf` lives on Salsa's branch and imports come
from a real tarball, not a filtered local `HEAD`. `upstream-signatures`
stays at its default ("auto") since byobu's GitHub releases aren't
GPG-signed yet — revisit once they are.

## release.py Phase 9 — Current Behavior

1. Resolves/pushes the plain version tag (e.g. `7.14`) to Salsa, for reference.
2. Generates the orig tarball from that tag via `git archive`.
3. Clones Salsa into a temp dir, runs `gbp import-orig` (default merge) to
   land the tarball on `upstream/latest` and merge it into `debian/latest`,
   extending the `pristine-tar` delta history.
4. Pushes `upstream/latest`, `debian/latest`, `pristine-tar`, and the new
   `upstream/<version>` tag — all plain, non-force. A rejected push means
   Salsa moved concurrently; re-run rather than force.

No push of `HEAD`. No `.maintainer/debian`. No force push. No `salsa/master`.

## 2026-07-14: debian/changelog is packaging-only; upstream ChangeLog moved to GitHub

Andreas Tille flagged that `debian/changelog` was being (mis)used as an upstream
changelog — bulleted trustmux/pwa feature and bugfix entries had been going straight
into it for the 7.15-1 draft (and every version before it, "almost 20 years" of
history per Dustin). His point: `debian/changelog` documents changes to the *Debian
packaging* only (control, rules, dependencies, patches). Upstream release notes
belong in a file that ships in the upstream tarball itself, so every distribution
repackaging byobu (Ubuntu, Fedora, etc.) can reuse the same information instead of
it being siloed inside Debian-specific metadata. He unilaterally rewrote the 7.15-1
stanza down to `* New upstream version` and deleted the never-uploaded 7.14-1 stanza
entirely before uploading to the `delayed/1` queue — Dustin agreed with the
principle after review.

**What changed:**

- **`/home/kirkland/src/byobu/ChangeLog`** (new, top-level, part of the upstream
  tarball) is now byobu's real changelog — curated, prose-bullet entries per
  version, same style as the old debian/changelog bullets but without any
  Debian-specific decoration (no `; urgency=`, no `UNRELEASED`/`unstable`
  distribution field — those are meaningless upstream). Full ~18-year history
  (back to `screen-profiles` in 2008) migrated in from `debian/changelog` verbatim;
  the two most recent entries (7.14, 7.15) were restored from their original
  curated content before Andreas's edit, not left squashed.
- **`dh_installchangelogs` auto-detects this file** — confirmed by reading its
  actual source (`find_changelog()` in `/usr/bin/dh_installchangelogs`, Perl,
  inside `debhelper`): it lowercases each candidate filename and compares against
  `changelog`/`changes`/`history` (optionally with `.txt`/`.md`/etc.), so
  `ChangeLog` (any case) matches `changelog` with no `debian/rules` override
  needed. Installed automatically as `/usr/share/doc/byobu/changelog.gz`.
- **`debian/changelog` gets a minimal, mechanical stanza only** — `push_salsa()`
  (Phase 9c, `release.py final`) now runs, in the temp Salsa clone, right after
  `gbp import-orig`'s merge and before pushing:
  ```
  dch --newversion {base_ver}-1 --distribution UNRELEASED --release-heuristic log \
      --package byobu "New upstream release. See /usr/share/doc/byobu/changelog for upstream changes."
  ```
  `--release-heuristic log` is required — plain `dch --newversion` merges into an
  existing `UNRELEASED` top stanza in-place instead of creating a real boundary
  (confirmed by actually running it and diffing the result, twice, independently).
  `log` instead checks for a dupload/dput log file recording a real prior upload;
  byobu's never been dput'd from an automated context, so none exists, and `dch`
  correctly creates a fresh stanza regardless of the existing top entry's
  distribution field. Verified with a real `gbp buildpackage` + `lintian` run in a
  `debian:sid` Docker container, checked out at the actual `upstream/<version>`
  tag (not current dev HEAD — same bug class as the `run_salsa_ci()` fix below;
  hit it again independently while validating this).
- **`determine_versions()` (final mode) now dies if `ChangeLog`'s top entry
  doesn't match `base_ver`** — a forcing function so a release can't ship without
  a curated entry. Nothing in the pipeline auto-generates the *content* of a
  ChangeLog entry (that requires editorial judgment: filtering internal-only
  commits like release-tooling/skill/doc changes out of what ships, writing
  clean prose from raw commit messages) — a human (or Claude, asked explicitly)
  must add the entry before running `release.py final`.

**Process reference** (from actually doing this by hand for 7.15 before automating
Phase 9c): drafted in a `git worktree` of `salsa/debian/latest` → `dch
--newversion X.Y-Z --distribution UNRELEASED --release-heuristic log` → hand-edit
the placeholder bullet to curated content → verify with `gbp buildpackage` +
`lintian` in Docker → commit → `git push salsa HEAD:debian/latest`. That was for
the one-off 7.15-1 stanza before this convention existed; going forward the
mechanical debian/changelog part is Phase 9c and only the ChangeLog file entry
needs to be written by hand.

## 2026-07-06 pristine-tar backfill (one-time, already done)

Backfilled the missing `7.14` pristine-tar delta and `upstream/7.14` tag
*before* flipping `pristine-tar = True`, specifically to avoid a window
where the config says "on" but no delta exists for the currently-committed
changelog version (which would break any build — Andreas's included —
against the then-current `debian/latest` HEAD). Verified the exact
existing `salsa/upstream/latest` 7.14 commit was already clean (no
`.maintainer`/`.claude`/`.github`), built the tarball from that commit
specifically (not a fresh re-derivation), confirmed byte-identical
pristine-tar round-trip, then confirmed a full real `gbp buildpackage` run
(162 tests passed) before pushing anything. 7.14-1 was not yet in the real
Debian archive at the time (still `7.13-1` there), so there was no
external immutable tarball to match — this reasoning would need to change
for any future backfill of an already-archive-uploaded version.
