#!/bin/zsh
# Build and install the snapshot helper. Rebuilding changes its signature, so macOS may ask
# for Full Disk Access again (System Settings > Privacy & Security > Full Disk Access).
set -e
cd "${0:A:h}"
mkdir -p ~/.local/bin
swiftc -O -o ~/.local/bin/people-snapshot people-snapshot.swift
codesign -s - -f ~/.local/bin/people-snapshot
echo "installed ~/.local/bin/people-snapshot"
