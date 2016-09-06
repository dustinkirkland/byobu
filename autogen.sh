#!/bin/sh
set -e

autoreconf -fiv

cat << EOF
The byobu build system is now prepared.

To build here, run:
  ./configure
  make
EOF
