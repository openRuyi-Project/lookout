#!/bin/sh
# Initialise or reconcile the managed openRuyi SPEC clone in a controlled directory.
#
# This is the third external source's on-disk state (peer to OBS and nvchecker):
# a full bare clone of the openRuyi packaging repository. Full history feeds each
# package's changelog; every current SPEC blob is read from it with `git cat-file`.
# The script is idempotent — safe to run at first deploy and on every container start.
#
# Network access to github.com is an environment concern (e.g. a git global proxy);
# this script stays proxy-agnostic so the project never hard-codes a network path.
#
# openRuyi packaging repository: https://github.com/openRuyi-Project/openRuyi
#
# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
set -eu

# All inputs are overridable so the same script serves host and container deploys.
BASE=${TRACKER_HOME:-"$HOME/apps/openruyi-tracker"}
REPO_URL=${SPEC_REPO_URL:-"https://github.com/openRuyi-Project/openRuyi.git"}
REPO_BRANCH=${SPEC_REPO_BRANCH:-"main"}
REPO_DIR=${SPEC_REPO_DIR:-"$BASE/runtime/spec-full.git"}
REFSPEC="+refs/heads/$REPO_BRANCH:refs/heads/$REPO_BRANCH"

mkdir -p "$(dirname "$REPO_DIR")"
# Never prompt indefinitely in an unattended container.
export GIT_TERMINAL_PROMPT=0

if [ ! -d "$REPO_DIR" ]; then
  # First run: a full bare clone (no --filter). A partial/blobless clone cannot serve
  # offline history traversal or SPEC reads, so the full object set is required.
  echo "Cloning $REPO_URL -> $REPO_DIR (full bare clone)"
  TEMP_DIR="$REPO_DIR.init.$$"
  trap 'rm -rf "$TEMP_DIR"' 0
  trap 'exit 1' HUP INT TERM
  git clone --bare --single-branch --branch "$REPO_BRANCH" "$REPO_URL" "$TEMP_DIR"
  mv "$TEMP_DIR" "$REPO_DIR"
  trap - 0 HUP INT TERM
fi

# Reconcile configuration every run so an interrupted or hand-made clone converges:
#  - correct origin URL,
#  - a fetch refspec so a plain `git fetch` advances the local branch,
#  - HEAD pointing at the tracked branch.
git -C "$REPO_DIR" remote set-url origin "$REPO_URL" 2>/dev/null \
  || git -C "$REPO_DIR" remote add origin "$REPO_URL"
git -C "$REPO_DIR" config remote.origin.fetch "$REFSPEC"

# Advance to the latest published commit. On a fresh clone this is a no-op.
echo "Fetching $REPO_BRANCH"
if ! git -C "$REPO_DIR" fetch --quiet origin; then
  # The collector records fetch failure/staleness. A usable existing clone must
  # still permit serving its last observations when the network is unavailable.
  echo "WARNING: fetch failed; retaining the existing SPEC clone" >&2
fi
git -C "$REPO_DIR" rev-parse --verify "refs/heads/$REPO_BRANCH" >/dev/null
git -C "$REPO_DIR" symbolic-ref HEAD "refs/heads/$REPO_BRANCH"

# A partial clone here would be a silent regression; fail loudly if one is present.
if git -C "$REPO_DIR" config --get remote.origin.partialclonefilter >/dev/null 2>&1; then
  echo "ERROR: $REPO_DIR is a partial clone; a full clone is required" >&2
  exit 1
fi

echo "SPEC clone ready: $(git -C "$REPO_DIR" rev-parse --short HEAD) ($(git -C "$REPO_DIR" rev-list --count HEAD) commits)"
