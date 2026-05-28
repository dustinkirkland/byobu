---
description: Triage byobu bugs and PRs across GitHub, Launchpad, and Debian BTS
argument-hint: Optional source to focus on (github-issues, github-prs, github-discussions, launchpad, debian)
---

# Byobu Issue Triage

Systematically work through all open bugs, issues, PRs, and discussions for byobu across all upstream sources. For each item, analyze it, classify it, and take or recommend action.

**Sources to process in priority order** (unless $ARGUMENTS specifies one):
1. GitHub Issues — https://github.com/dustinkirkland/byobu/issues
2. GitHub PRs — https://github.com/dustinkirkland/byobu/pulls
3. GitHub Discussions — https://github.com/dustinkirkland/byobu/discussions
4. Launchpad Ubuntu — https://bugs.launchpad.net/ubuntu/+source/byobu
5. Launchpad Byobu — https://bugs.launchpad.net/byobu
6. Debian BTS — https://bugs.debian.org/cgi-bin/pkgreport.cgi?repeatmerged=no&src=byobu

---

## Phase 1: Inventory

Fetch open items from each source. Run these in parallel where possible.

**GitHub** (use `gh` CLI):
```bash
gh issue list --repo dustinkirkland/byobu --state open --limit 100 --json number,title,createdAt,updatedAt,labels,author,body
gh pr list --repo dustinkirkland/byobu --state open --limit 100 --json number,title,createdAt,updatedAt,author,isDraft,mergeable
gh api repos/dustinkirkland/byobu/discussions --jq '[.[] | {number:.number, title:.title, createdAt:.created_at, updatedAt:.updated_at, author:.user.login}]' 2>/dev/null || true
```

**Launchpad and Debian** (use WebFetch):
- Fetch https://bugs.launchpad.net/ubuntu/+source/byobu — extract open bug titles, numbers, dates, status
- Fetch https://bugs.launchpad.net/byobu — extract open bug titles, numbers, dates, status
- Fetch https://bugs.debian.org/cgi-bin/pkgreport.cgi?repeatmerged=no&src=byobu — extract open bug titles, numbers, severity

Present a summary table of counts by source before proceeding. Ask the user if they want to start from the top or jump to a specific source.

---

## Phase 2: Triage — GitHub Issues

Work through each open GitHub issue one at a time. For each issue:

1. Fetch full details: `gh api repos/dustinkirkland/byobu/issues/NUMBER`
2. Fetch all comments: `gh api repos/dustinkirkland/byobu/issues/NUMBER/comments`
3. Check whether the current codebase already addresses it by searching relevant files

**Classify into one of:**

- **ALREADY FIXED** — The issue describes a bug that no longer exists in current master. Close it politely, noting the approximate commit/version that fixed it.
- **STALE / NO INFO** — Reporter never followed up, no reproduction steps, very old with no activity (>2 years). Close politely, invite reopening with more detail.
- **DUPLICATE** — Same root cause as another issue. Close with a reference to the canonical issue.
- **REAL + FIXABLE** — Confirmed, reproducible, worth addressing now. Attempt a fix, then close with the fix commit.
- **REAL + DEFERRED** — Confirmed but complex, platform-specific, or requires hardware we can't test. Leave open, add a comment summarizing the status.
- **NEEDS MORE INFO** — Plausible but unverifiable. Post a polite comment asking for specific details (OS, version, reproduction steps), don't close yet.
- **WONTFIX** — Out of scope, working as intended, or the "fix" would harm other users. Close politely with an explanation.

**Tone guidance for all comments:**
- Thank the reporter by name if it's a closure
- Be specific about why it's being closed
- Invite reopening if new information emerges (for stale/needs-info closures)
- Never be dismissive; every report took effort

After classifying, **confirm the action with the user before posting or closing**, unless the classification is unambiguous (e.g., clearly already fixed in a commit you can cite).

---

## Phase 3: Triage — GitHub PRs

Work through each open PR. For each:

1. Fetch PR details and diff: `gh api repos/dustinkirkland/byobu/pulls/NUMBER` and `gh api repos/dustinkirkland/byobu/pulls/NUMBER/files`
2. Fetch all comments: `gh api repos/dustinkirkland/byobu/issues/NUMBER/comments`
3. Check merge conflict status from `mergeable` field

**Evaluate on five axes:**

1. **Safety / Security** — No command injection, no unquoted variables in shell, no world-writable temp files, no privilege escalation. Shell scripts must be POSIX sh unless the file declares bash explicitly.
2. **Correctness** — Does it actually do what it claims? Check for off-by-one errors, wrong field indices, broken awk/sed patterns (see lessons from #91).
3. **Performance** — No new subprocesses in hot paths without justification. No polling loops. Forks should be cached where possible (see BYOBU_OSTYPE pattern).
4. **Regression risk** — Are non-targeted platforms (Linux, macOS, other BSDs) fully insulated? Every platform-specific path should be gated behind an OS or file-existence check.
5. **Genuine usefulness** — Does it solve a real problem? Is a new separate script the right shape, or should it be a configurable option on an existing indicator (see LOAD_AVERAGES=3 pattern)?

**Classify into one of:**

- **MERGE AS-IS** — Passes all five axes. Merge, push, thank contributor, note any tiny follow-up commits you make.
- **MERGE WITH FIXES** — Good idea, fixable bugs. Fix them yourself, merge, explain in the thank-you comment exactly what changed and why.
- **NEEDS CONTRIBUTOR WORK** — Structural issues the contributor should address. Post a detailed, constructive review comment listing specific problems and how to fix them.
- **SUPERSEDED** — Already landed via another PR or commit. Close with a reference.
- **CONFLICT** — Has merge conflicts. Attempt rebase/merge manually; if non-trivial, post a comment describing the conflict and asking contributor to rebase.
- **CLOSE / WONTFIX** — Out of scope, harmful, or duplicates existing functionality without improvement. Close with a clear, respectful explanation.

For MERGE actions, always confirm with the user before running `gh pr merge`.

---

## Phase 4: Triage — GitHub Discussions

Fetch discussions via the API or WebFetch. For each:

- If it's a feature request that's already implemented, note the implementation.
- If it's a bug in disguise, suggest converting to an issue.
- If it's a Q&A that's been answered, mark it answered if it isn't already.
- Otherwise summarize and leave open.

Discussions rarely need closing; focus on making sure answered ones are marked.

---

## Phase 5: Triage — Launchpad (Ubuntu + Byobu)

Use WebFetch to fetch each Launchpad bug list page. For individual bugs, fetch the bug page directly (e.g., `https://bugs.launchpad.net/byobu/+bug/NUMBER`).

**Cross-reference with GitHub**: Many Launchpad bugs are duplicates of GitHub issues or already fixed in upstream. Check current master before confirming a bug is open.

**Status options on Launchpad** (you cannot change these directly — flag them for the user to act on, or note them in your summary):
- Mark as **Fix Released** if fixed in current upstream
- Mark as **Invalid** if not a byobu bug
- Mark as **Confirmed** if you can verify the reproduction

Since you can't authenticate to Launchpad, produce a list of recommended actions (bug number, current status, recommended new status, reason) for the user to apply.

**Closing Launchpad bugs via debian/changelog**: When a fix is committed (or already in master), the proper way to officially close a Launchpad Ubuntu bug is to add `- LP: #NNNNNN` as a sub-bullet under the relevant change entry in `debian/changelog`. The Ubuntu archive infrastructure automatically marks the bug Fix Released when a package containing that changelog entry is uploaded. The format used in this repo is:

```
  * path/to/file:
    - description of the fix
    - LP: #NNNNNN
```

If the fix is already in master but the changelog entry lacks the LP reference, add it. If a new fix is being committed, add the `LP: #NNNNNN` line as part of that commit. Always add LP references to the topmost (UNRELEASED) changelog stanza. Multiple LP references for a single changelog bullet are fine on separate lines.

---

## Phase 6: Triage — Debian BTS

Use WebFetch to fetch https://bugs.debian.org/cgi-bin/pkgreport.cgi?repeatmerged=no&src=byobu.

For each open Debian bug:
- Check if fixed in current upstream master
- Check if it's a Debian-packaging issue vs. an upstream code issue
- If it's an upstream fix needed: check whether it's already in the GitHub issues list

**Closing Debian bugs via debian/changelog**: Debian bugs are officially closed by including `Closes: #NNNNNN` in a `debian/changelog` entry that reaches the Debian archive. The format used in this repo is:

```
  * path/to/file:
    - description of the fix
    - Closes: #NNNNNN
```

When a Debian bug is confirmed fixed in current master, add the `Closes: #NNNNNN` line under the relevant entry in the topmost (UNRELEASED) changelog stanza. If no single change entry is a natural home for it (e.g., the fix was incidental), add a dedicated bullet:

```
  * debian/changelog: close Debian bug fixed in prior commit
    - Closes: #NNNNNN
```

Commit the changelog update, push it, and note it in your triage summary. The bug will be closed automatically when the next Debian package upload containing that entry is processed by the archive.

Note: `LP:` and `Closes:` references can coexist in the same changelog entry when a fix addresses both a Launchpad and a Debian bug simultaneously.

---

## Phase 7: Summary

At the end of each source, and at the very end of the full triage, present a brief summary table:

```
Source            | Reviewed | Closed | Fixed | Deferred | No-action
------------------|----------|--------|-------|----------|----------
GitHub Issues     |        X |      X |     X |        X |         X
GitHub PRs        |        X |      X |     X |        X |         X
GitHub Discussions|        X |      X |     X |        X |         X
Launchpad Ubuntu  |        X |    n/a |   n/a |      n/a |         X
Launchpad Byobu   |        X |    n/a |   n/a |      n/a |         X
Debian BTS        |        X |    n/a |   n/a |      n/a |         X
```

---

## General Rules

- **Always read the current source file before confirming a bug is fixed or unfixed.** Don't rely on memory.
- **Never close an issue without a comment.** Every closure gets a polite explanation.
- **Confirm before acting** on anything destructive (close, merge, push). A quick "shall I go ahead?" costs nothing.
- **One item at a time** — don't batch-close without user awareness of what's being closed.
- **Cite your work** — when closing as "already fixed", name the commit or version. When closing a PR with fixes applied, link the follow-up commit.
- **Close Launchpad bugs via changelog** — add `- LP: #NNNNNN` under the relevant entry in the UNRELEASED `debian/changelog` stanza and commit it. The archive does the rest on next upload.
- **Close Debian bugs via changelog** — add `- Closes: #NNNNNN` under the relevant entry in the UNRELEASED `debian/changelog` stanza and commit it. Both references can coexist in the same entry.
- **Match the existing comment tone** — warm, direct, grateful, never terse or dismissive.
- Use `gh` CLI for all GitHub operations. Use WebFetch for Launchpad and Debian BTS pages.
