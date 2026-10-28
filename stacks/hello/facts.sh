#!/usr/bin/env bash
# facts.sh: live facts in front of every turn. Runs as the agent user in the app dir, 20 s at most.
cd "${APP_DIR:-/srv/app}" 2>/dev/null || exit 0
echo "app dir: $PWD ($(ls | tr '\n' ' '))"
app status 2>/dev/null | head -2
