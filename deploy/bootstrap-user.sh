#!/bin/sh
# No sudo, global installs, or changes to existing sites.
# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
set -eu
BASE=${TRACKER_HOME:-"$HOME/apps/openruyi-tracker"}
NODE_VERSION=v24.21.0
case "$(uname -m)" in x86_64) ARCH=x64 ;; aarch64) ARCH=arm64 ;; *) echo "Unsupported Node architecture" >&2; exit 1;; esac
NODE_DIR="$BASE/runtime/node-$NODE_VERSION-linux-$ARCH"
mkdir -p "$BASE/runtime" "$BASE/state" "$BASE/releases"
if [ ! -x "$NODE_DIR/bin/node" ]; then
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT HUP INT TERM
  FILE="node-$NODE_VERSION-linux-$ARCH.tar.xz"
  curl --fail --silent --show-error --location --max-time 180 "https://nodejs.org/dist/$NODE_VERSION/$FILE" -o "$TMP/$FILE"
  curl --fail --silent --show-error --location --max-time 30 "https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt" -o "$TMP/SHASUMS256.txt"
  (cd "$TMP"; grep "  $FILE\$" SHASUMS256.txt | sha256sum -c -)
  tar -xJf "$TMP/$FILE" -C "$BASE/runtime"
fi
ln -sfn "$NODE_DIR" "$BASE/runtime/node"
python3 -c 'import rpm; print("Native RPM", rpm.__version__)'
if [ ! -x "$BASE/runtime/venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$BASE/runtime/venv"
fi
"$NODE_DIR/bin/node" --version
"$BASE/runtime/venv/bin/python" --version
