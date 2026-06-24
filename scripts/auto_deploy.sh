#!/usr/bin/env bash
# Auto-deploy telegram-lark: pull latest main from GitHub and redeploy.
# Triggered every minute by auto-deploy.timer. Git is the single source of truth.
set -euo pipefail

REPO_DIR=/opt/telegram-lark
BRANCH=main
# Long-running (Type=simple) services that must be restarted on code change.
# The oneshot+timer jobs pick up new code automatically on their next run.
DAEMON_SERVICES=(telegram-private-autoreply.service)

cd "$REPO_DIR"

# Only operate on a real git checkout.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

OLD_REV=$(git rev-parse HEAD)

git fetch --quiet origin "$BRANCH"
NEW_REV=$(git rev-parse "origin/$BRANCH")

# Up to date: nothing to do, stay quiet.
[ "$OLD_REV" = "$NEW_REV" ] && exit 0

echo "[auto-deploy] $(date -Is) updating ${OLD_REV:0:7} -> ${NEW_REV:0:7}"

CHANGED=$(git diff --name-only "$OLD_REV" "$NEW_REV")

# Git is source of truth: force working tree to match remote main.
git reset --hard "origin/$BRANCH"

# Reinstall Python deps only when requirements.txt changed.
if echo "$CHANGED" | grep -qx 'requirements.txt'; then
  echo "[auto-deploy] requirements.txt changed -> installing deps"
  "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"
fi

# Sync systemd unit templates and reload only when they changed.
if echo "$CHANGED" | grep -q '^systemd/'; then
  echo "[auto-deploy] systemd units changed -> syncing to /etc/systemd/system"
  cp "$REPO_DIR"/systemd/*.service "$REPO_DIR"/systemd/*.timer /etc/systemd/system/ 2>/dev/null || true
  systemctl daemon-reload
fi

# Restart only the long-running daemons so they load the new code.
for svc in "${DAEMON_SERVICES[@]}"; do
  if systemctl is-active --quiet "$svc" || systemctl is-enabled --quiet "$svc" 2>/dev/null; then
    echo "[auto-deploy] restarting $svc"
    systemctl restart "$svc"
  fi
done

echo "[auto-deploy] done $(date -Is)"
