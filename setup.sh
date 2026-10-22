#!/bin/sh
# Run from any directory. Re-running this never resets an existing repository.
set -eu

app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
parser_dir="$app_dir/ticket_parser"

# Checking Git membership alone would find the parent repository.
if [ -e "$parser_dir/.git" ] || [ -L "$parser_dir/.git" ]; then
    git -C "$parser_dir" rev-parse --verify HEAD >/dev/null
    printf '%s\n' 'Keeping the existing ticket_parser repository unchanged.'
else
    git init -b main "$parser_dir"
    git -C "$parser_dir" config user.name 'Whalehub'
    git -C "$parser_dir" config user.email 'whalehub@example.invalid'
    git -C "$parser_dir" add .
    git -C "$parser_dir" commit -m 'Initial ticket parser and workflow scaffolds'
fi

mkdir -p "$app_dir/.whalehub/runs" \
    "$app_dir/.whalehub/worktrees" "$app_dir/.whalehub/state"

# An empty placeholder lets the foundation start without real credentials.
# Runner integration will require an attendance ID before calling Claude.
if [ ! -e "$app_dir/attendance_id" ]; then
    : > "$app_dir/attendance_id"
fi

printf '%s\n' 'Setup complete. Start Whalehub with docker compose up --build.'
