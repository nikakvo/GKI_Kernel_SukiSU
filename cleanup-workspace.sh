#!/bin/bash
# Run this AFTER you've copied out the zip/boot.img files you need.
# Deletes the versioned build directory (heavy, specific to that sub_level),
# keeps the shared repos/toolchain for faster future builds.

set -e
WORKSPACE="$HOME/gki-workspace"

echo "=== Before cleanup ==="
du -sh "$WORKSPACE"/* 2>/dev/null

echo ""
echo "About to delete versioned build directories (android*-*-*), keeping:"
echo "  AnyKernel3, SukiSU_patch, kernel_patches, susfs4ksu, toolchain, mkbootimg, git-repo"
read -p "Continue? (y/n) " confirm

if [ "$confirm" = "y" ]; then
    find "$WORKSPACE" -maxdepth 1 -type d -regextype posix-extended -regex '.*/android[0-9]+-[0-9.]+-[0-9X]+' -exec rm -rf {} \;
    echo "Done."
else
    echo "Cancelled, nothing was deleted."
fi

echo ""
echo "=== After cleanup ==="
du -sh "$WORKSPACE"/* 2>/dev/null
