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
| `pristine-tar` | Salsa | correct already |

## gbp.conf (on salsa/debian/latest)

```ini
[DEFAULT]
upstream-branch = upstream/latest
debian-branch = debian/latest
pristine-tar = False
```

`upstream-tree = HEAD` from the old `.maintainer/debian/gbp.conf` is gone —
it doesn't apply now that `gbp.conf` lives on Salsa's branch and imports come
from a real tarball, not a filtered local `HEAD`.

## release.py Phase 9 — Current Behavior

1. Resolves/pushes the plain version tag (e.g. `7.14`) to Salsa, for reference.
2. Generates the orig tarball from that tag via `git archive`.
3. Clones Salsa into a temp dir, runs `gbp import-orig` (default merge) to
   land the tarball on `upstream/latest` and merge it into `debian/latest`.
4. Pushes both `upstream/latest` and `debian/latest` — plain, non-force.

No push of `HEAD`. No `.maintainer/debian`. No force push. No `salsa/master`.
