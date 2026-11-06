#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
APP_VERSION="$(sed -nE "s/^[[:space:]]*version: '([^']+)'.*/\\1/p" \
  meson.build | head -n 1)"
export APP_VERSION
mkdir -p .appimage-build
appimage-builder --recipe AppImageBuilder.yml
