---
description: Open the next development version after cutting a release — bumps configure.ac version, adds UNRELEASED debian/changelog stanza, and commits
---

Run this immediately after `/release final` to open the next development cycle.

---

## Step 1: Build identity from ~/.bashrc

```bash
DEBEMAIL=$(grep -oP 'DEBEMAIL=\K\S+' ~/.bashrc | tail -1 | tr -d '"'"'")
DEBFULLNAME=$(grep -oP 'DEBFULLNAME=\K.*' ~/.bashrc | tail -1 | tr -d '"'"'")
echo "DEBFULLNAME=$DEBFULLNAME"
echo "DEBEMAIL=$DEBEMAIL"
```

If either is empty, stop and ask the user to add them to `~/.bashrc`.

---

## Step 2: Determine the next version

Read the current version from `configure.ac`:

```bash
CURRENT_VER=$(grep -oP "AC_INIT\(\[byobu\], \[\K[^\]]+" /home/kirkland/src/byobu/configure.ac)
echo "Current version: $CURRENT_VER"
```

Compute the next minor version (bump the last numeric component by 1):

```bash
MAJOR=$(echo "$CURRENT_VER" | cut -d. -f1)
MINOR=$(echo "$CURRENT_VER" | cut -d. -f2)
NEXT_MINOR=$((MINOR + 1))
NEXT_VER="${MAJOR}.${NEXT_MINOR}"
echo "Next version: $NEXT_VER"
```

---

## Step 3: Update configure.ac

```bash
sed -i "s/AC_INIT(\[byobu\], \[${CURRENT_VER}\]/AC_INIT([byobu], [${NEXT_VER}]/" \
  /home/kirkland/src/byobu/configure.ac
grep "AC_INIT" /home/kirkland/src/byobu/configure.ac
```

Verify the substitution looks right before continuing.

---

## Step 4: Add UNRELEASED debian/changelog stanza

```bash
cd /home/kirkland/src/byobu
DEBEMAIL="$DEBEMAIL" DEBFULLNAME="$DEBFULLNAME" \
  dch --newversion "$NEXT_VER" --distribution UNRELEASED --urgency medium \
  "Open ${NEXT_VER} for development"
```

Verify the top of `debian/changelog`:

```bash
head -6 /home/kirkland/src/byobu/debian/changelog
```

Expected:
```
byobu (X.Y) UNRELEASED; urgency=medium

  * Open X.Y for development

 -- Dustin Kirkland <...>  <datestamp>
```

---

## Step 5: Commit

```bash
cd /home/kirkland/src/byobu
git add configure.ac debian/changelog
git commit -m "bump version to ${NEXT_VER} and open for development"
```

---

## Notes

- Always run this **immediately after** `/release final` so `master` never sits at a released version.
- The version scheme is `MAJOR.MINOR` (e.g. 7.0 → 7.1 → 7.2). If a major bump is needed, override `NEXT_VER` manually before Step 3.
- The `UNRELEASED` distribution ensures the new stanza is clearly marked as in-progress and won't accidentally be treated as a release target.
