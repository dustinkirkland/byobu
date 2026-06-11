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

## Step 4: Prepend a fresh UNRELEASED stanza

`dch --newversion` merges into the existing UNRELEASED stanza when the current
top entry is also UNRELEASED — it simply bumps the version number in-place,
losing the per-version boundary. Instead, stamp the existing stanza's trailer
with the current datestamp, then prepend a brand-new stanza on top.

```bash
cd /home/kirkland/src/byobu

# 4a. Re-stamp the trailer of the current top stanza with right-now, so its
#     datestamp reflects when it was "closed" as the previous version.
DATESTAMP=$(date -R)
sed -i "1,/^ -- /{s/^ -- \(.*\)  .*/ -- \1  ${DATESTAMP}/}" debian/changelog

# 4b. Prepend a brand-new stanza for NEXT_VER above the existing content.
python3 - <<PYEOF
import datetime, subprocess, os

debemail   = os.environ['DEBEMAIL']
debfullname = os.environ['DEBFULLNAME']
next_ver   = os.environ['NEXT_VER']
datestamp  = subprocess.check_output(['date', '-R']).decode().strip()

new_stanza = (
    f"byobu ({next_ver}) UNRELEASED; urgency=medium\n"
    f"\n"
    f"  * Open {next_ver} for development\n"
    f"\n"
    f" -- {debfullname} <{debemail}>  {datestamp}\n"
    f"\n"
)

cl = open('debian/changelog').read()
open('debian/changelog', 'w').write(new_stanza + cl)
print("New top of debian/changelog:")
print(new_stanza)
PYEOF
```

Verify two separate stanzas now appear at the top:

```bash
head -14 /home/kirkland/src/byobu/debian/changelog
```

Expected:
```
byobu (X.Y) UNRELEASED; urgency=medium

  * Open X.Y for development

 -- Dustin Kirkland <...>  <datestamp>

byobu (X.Y-1) UNRELEASED; urgency=medium   ← or whatever the prev version was

  ...previous changes...
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
