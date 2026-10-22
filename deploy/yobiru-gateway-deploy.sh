#!/bin/sh
# Deploy a released tag to the official gateway (bots.yobiru.com). Installed once on the server, owned by root:
#   install -o root -g root -m 0755 deploy/yobiru-gateway-deploy.sh /usr/local/sbin/yobiru-gateway-deploy
# and run by the GitHub Actions runner through one sudoers rule (deploy/yobiru-gateway-deploy.sudoers). It takes
# nothing but a stable tag name, fetches that tag from GitHub into the existing checkout, and:
#   1. stops the gateway and its mail relay, copies the database and gateway.json to the backups folder,
#   2. checks out the tag, runs `migrate` (a no-op when the schema is current), starts both services,
#   3. waits for /v1/health on loopback; on any failure it restores the copy, checks out the previous commit and
#      starts the old version again, then exits non-zero.
# Paths and names can be changed in /etc/default/yobiru-gateway-deploy (root-owned, same variable names).
set -eu
umask 077

TAG="${1:-}"
case "$TAG" in
  v[0-9]*.[0-9]*.[0-9]*) ;;
  *) echo "usage: yobiru-gateway-deploy vX.Y.Z" >&2; exit 2 ;;
esac
if ! printf '%s\n' "$TAG" | grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'; then
  echo "refusing $TAG: only stable release tags are deployed" >&2; exit 2
fi

ROOT=/srv/yobiru-gateway
SRC="$ROOT/src"
GW="$ROOT/gw"
BACKUPS="$ROOT/backups"
RUN_AS=yobiru-gw
PYTHON=/usr/bin/python3
SERVICES="yobiru-gateway.service yobiru-gateway-mail.service"
HEALTH_URL=http://127.0.0.1:7780/v1/health
[ -r /etc/default/yobiru-gateway-deploy ] && . /etc/default/yobiru-gateway-deploy

as_gw() { runuser -u "$RUN_AS" -- "$@"; }
log() { echo "==> $*"; }

exec 9>/run/yobiru-gateway-deploy.lock
flock -n 9 || { echo "another deploy is running" >&2; exit 1; }

log "fetching $TAG"
as_gw git -C "$SRC" fetch --quiet --force origin "refs/tags/$TAG:refs/tags/$TAG"
NEW="$(as_gw git -C "$SRC" rev-parse --verify "refs/tags/$TAG^{commit}")"
OLD="$(as_gw git -C "$SRC" rev-parse --verify HEAD)"
log "current $(as_gw git -C "$SRC" describe --tags --always HEAD) ($OLD), target $TAG ($NEW)"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUPS/pre-$TAG-$STAMP"

healthy() {
  i=0
  while [ "$i" -lt 15 ]; do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then return 0; fi
    i=$((i + 1)); sleep 2
  done
  return 1
}

BACKED_UP=0
rollback() {
  set +e
  echo "!! deploy of $TAG failed, rolling back to $OLD" >&2
  systemctl stop $SERVICES
  if [ "$BACKED_UP" = 1 ]; then
    install -o "$RUN_AS" -g "$RUN_AS" -m 0600 "$BACKUP.sqlite3" "$GW/gateway.sqlite3"
    install -o "$RUN_AS" -g "$RUN_AS" -m 0600 "$BACKUP.gateway.json" "$GW/gateway.json"
  fi
  as_gw git -C "$SRC" checkout --quiet --detach "$OLD"
  systemctl start $SERVICES 9>&-
  if healthy; then echo "!! rolled back; the previous version is serving again" >&2
  else echo "!! rollback did not come up healthy either: check the server now" >&2; fi
  exit 1
}

log "stopping $SERVICES"
systemctl stop $SERVICES
trap rollback EXIT

log "backing up to $BACKUP.*"
install -d -o "$RUN_AS" -g "$RUN_AS" -m 0700 "$BACKUPS"
as_gw "$PYTHON" - "$GW/gateway.sqlite3" "$BACKUP.sqlite3" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
as_gw cp -p "$GW/gateway.json" "$BACKUP.gateway.json"
[ -s "$BACKUP.sqlite3" ] || { echo "backup is empty" >&2; exit 1; }
BACKED_UP=1

log "checking out $TAG"
as_gw git -C "$SRC" checkout --quiet --detach "$NEW"

log "migrating"
as_gw "$PYTHON" "$SRC/public_bots/bot_gateway.py" --gateway-home "$GW" migrate

log "starting $SERVICES"
systemctl start $SERVICES 9>&-
healthy || { echo "gateway is not healthy on $HEALTH_URL" >&2; exit 1; }
systemctl is-active $SERVICES

trap - EXIT
log "deployed $TAG ($NEW); backup $BACKUP.sqlite3"
# Keep the 20 newest deploy backups (the daily backup timer keeps its own files).
ls -1t "$BACKUPS"/pre-v*.sqlite3 2>/dev/null | tail -n +21 | while read -r f; do
  rm -f -- "$f" "${f%.sqlite3}.gateway.json"
done
