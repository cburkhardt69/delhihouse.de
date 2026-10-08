#!/bin/bash
# Refresh the DHS & LDI finance dashboard at https://delhihouse.de/finance/
# 1) Download the four Tally Day Book XML exports into ~/Downloads.
# 2) Double-click this file (or run /update-finance-dashboard in Claude Code).

# ---- Settings ---------------------------------------------------------------
INBOX="$HOME/Downloads"
SUBDIR="finance"
KEYCHAIN_ITEM="dhs-finance-dashboard"
PUSH="${PUSH:-yes}"          # PUSH=no builds and commits locally without pushing
# -----------------------------------------------------------------------------

set -e
KIT="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(git -C "$KIT" rev-parse --show-toplevel)"
pause() { [ -t 0 ] && [ -z "$CLAUDECODE" ] && read -n 1 -s -r -p "$1"; echo; }

DASHBOARD_PASSWORD="$(security find-generic-password -s "$KEYCHAIN_ITEM" -w 2>/dev/null)" || {
  echo "No dashboard password in the Keychain yet. Store it once with:"
  echo "  security add-generic-password -s $KEYCHAIN_ITEM -a dashboard -w"
  pause "Press any key to close"; exit 1; }
export DASHBOARD_PASSWORD

echo "Pulling latest website changes…"
git -C "$REPO_DIR" pull --rebase --quiet

echo "Building dashboard from Tally exports in $INBOX…"
python3 "$KIT/refresh_dashboard.py" --inbox "$INBOX" --warehouse "$KIT" --site "$REPO_DIR/$SUBDIR"

git -C "$REPO_DIR" add "$SUBDIR"
if git -C "$REPO_DIR" diff --cached --quiet; then
  echo "No changes to publish."
else
  git -C "$REPO_DIR" commit --quiet -m "Update finance dashboard $(date +%Y-%m-%d)"
  if [ "$PUSH" = "yes" ]; then git -C "$REPO_DIR" push --quiet; echo "Published. GitHub Pages usually updates within a minute or two."
  else echo "Committed locally, not pushed (PUSH=no)."; fi
fi
pause "Done. Press any key to close"
