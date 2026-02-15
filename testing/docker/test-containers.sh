#!/bin/bash
# Byobu 6.14 Container Test Script

echo "Byobu 6.14 Test Containers"
echo "============================"
echo ""

# Check if images exist
if ! docker image inspect byobu-ubuntu:6.14 >/dev/null 2>&1; then
    echo "⚠️  Ubuntu image not found. Building..."
    docker build -t byobu-ubuntu:6.14 -f Dockerfile.ubuntu .
fi

if ! docker image inspect byobu-wolfi:6.14 >/dev/null 2>&1; then
    echo "⚠️  Wolfi image not found. Building..."
    docker build -t byobu-wolfi:6.14 -f Dockerfile.wolfi .
fi

echo ""
echo "✅ Docker images ready!"
echo ""
echo "Launch Commands:"
echo "================"
echo ""
echo "Ubuntu 24.04 Container:"
echo "  docker run -it --rm byobu-ubuntu:6.14"
echo ""
echo "Wolfi/Chainguard Container:"
echo "  docker run -it --rm byobu-wolfi:6.14"
echo ""
echo "Test Byobu Inside Container:"
echo "  1. Run: byobu"
echo "  2. Check version: byobu -v"
echo "  3. Test features:"
echo "     - F2 = new window"
echo "     - F3/F4 = navigate windows"
echo "     - F6 = detach"
echo "     - F9 = configuration menu"
echo ""
