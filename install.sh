#!/usr/bin/env bash
# Adds an `audiobookify` alias to ~/.zshrc that runs the script in whatever
# folder you're standing in. Re-running is safe (it replaces the old line).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RC="${ZDOTDIR:-$HOME}/.zshrc"
LINE="alias audiobookify='python3 \"$DIR/audiobookify.py\"'"

touch "$RC"
# drop any previous audiobookify alias, then append the current one
grep -v "alias audiobookify=" "$RC" > "$RC.tmp" && mv "$RC.tmp" "$RC"
printf '%s\n' "$LINE" >> "$RC"

echo "Added to $RC:"
echo "  $LINE"
echo "Run 'source $RC' (or open a new terminal) to use 'audiobookify'."
