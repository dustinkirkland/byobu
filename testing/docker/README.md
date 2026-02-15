# Byobu Docker Testing

This directory contains Docker configurations for testing byobu across different platforms.

## Files

- `Dockerfile.ubuntu` - Ubuntu 24.04 test container
- `Dockerfile.wolfi` - Wolfi/Chainguard test container
- `test-containers.sh` - Helper script to build and run containers
- `DOCKER-TESTING.md` - Detailed testing documentation

## Quick Start

### Build Both Images

From the repository root:

```bash
# Ubuntu 24.04
docker build -t byobu-ubuntu:6.14 -f testing/docker/Dockerfile.ubuntu .

# Wolfi/Chainguard
docker build -t byobu-wolfi:6.14 -f testing/docker/Dockerfile.wolfi .
```

Or use the helper script:

```bash
cd testing/docker
./test-containers.sh
```

### Run Containers

**Ubuntu 24.04:**
```bash
docker run -it --rm byobu-ubuntu:6.14
```

**Wolfi/Chainguard:**
```bash
docker run -it --rm byobu-wolfi:6.14
```

## What's Tested

These containers build and install byobu 6.14 from source, including:

- All merged PRs (10 total)
- All fixed issues (6 total)
- Full build process verification
- Runtime functionality testing

## Testing Checklist

Inside each container:

1. **Version check**: `byobu -v`
2. **Launch**: `byobu`
3. **Key bindings**: F2, F3, F4, F6, F9
4. **Help screen**: F1 (should not disappear)
5. **Prompt runtime**: Should show command execution time
6. **No errors**: Exit byobu and verify no permission errors

See `DOCKER-TESTING.md` for complete testing instructions.

## Container Specifications

### Ubuntu 24.04
- Base image: `ubuntu:24.04`
- User: `testuser` (non-root)
- Shell: `/bin/bash`
- Byobu: Auto-enabled on login

### Wolfi/Chainguard
- Base image: `cgr.dev/chainguard/wolfi-base:latest`
- User: `nonroot`
- Shell: `/bin/bash`
- Byobu: Installed, manual launch

## Clean Up

Remove test images when done:

```bash
docker rmi byobu-ubuntu:6.14 byobu-wolfi:6.14
```
