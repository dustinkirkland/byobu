# Byobu 6.14 Docker Test Containers

Both test containers are ready!

## Quick Launch Commands

### Ubuntu 24.04 Container
```bash
docker run -it --rm byobu-ubuntu:6.14
```

### Wolfi/Chainguard Container
```bash
docker run -it --rm byobu-wolfi:6.14
```

## What's Been Tested

This byobu 6.14 build includes:

### 10 PRs Merged:
- PR #68: stderr fix for prompt runtime
- PR #63: expr syntax error fix
- PR #64: help screen LESS variable fix
- PR #67: README typo fix
- PR #70: version management with autoconf
- PR #55: home directory ownership test
- PR #42: systemd support
- PR #36: OpenDNS with wget fallback
- PR #74: Oracle Linux logo
- PR #69: automake dist directives

### 6 Issues Fixed:
- Issue #83: byobu -v now works
- Issue #75: no permission denied outside byobu
- Issue #72: git:// → https:// in README
- Issue #73: Arch Linux updates detection
- Issue #80: prompt runtime display (closed)
- Issue #84: multiline editing (closed)

## Testing Inside Containers

Once inside a container, test these features:

### Basic Functionality
```bash
# Check version
byobu -v

# Launch byobu
byobu

# Inside byobu, test:
# - F2: Create new window
# - F3/F4: Navigate windows
# - F6: Detach from session
# - F9: Configuration menu
# - Ctrl+a d: Detach (alternative)
```

### Test Specific Fixes

**Issue #83 - Version command:**
```bash
byobu -v
# Should display: byobu version 6.14
```

**Issue #75 - No permission errors:**
```bash
# Exit byobu and run commands
exit
ls
# Should not see "Permission denied" errors
```

**Issue #68 - Prompt runtime:**
```bash
# Inside byobu, commands should show runtime like [0.002s]
# And NOT cause line wrapping issues
```

**Help screen:**
```bash
# Press F1 or Shift+F1
# Help should display and not disappear
```

## Container Details

### Ubuntu 24.04 Container
- Base: ubuntu:24.04
- User: testuser
- Shell: /bin/bash
- Byobu: Auto-enabled on login

### Wolfi/Chainguard Container
- Base: cgr.dev/chainguard/wolfi-base:latest
- User: nonroot
- Shell: /bin/bash
- Byobu: Installed, ready to launch

## Rebuilding

If you need to rebuild the containers:

```bash
# Ubuntu
docker build -t byobu-ubuntu:6.14 -f Dockerfile.ubuntu .

# Wolfi
docker build -t byobu-wolfi:6.14 -f Dockerfile.wolfi .
```

## Clean Up

Remove test containers when done:

```bash
docker rmi byobu-ubuntu:6.14 byobu-wolfi:6.14
```
