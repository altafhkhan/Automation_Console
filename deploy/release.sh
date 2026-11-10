#!/usr/bin/env bash
# Release the Console API to the droplet from a local checkout.
#
#   deploy/release.sh [commit] [host]
#
# Defaults: commit = HEAD of this checkout, host = root@165.22.240.27.
# Run from Git Bash on Windows or any shell with git, ssh, scp and tar.
#
# Steps (idempotent, re-runnable):
#   1. Back up the Postgres database (pg_dump) to /opt/backups on the droplet.
#   2. Upload `git archive <commit>` and unpack it over /opt/console.
#      Only tracked files are replaced; deploy/.env, deploy/nginx/ocrdocflow*.conf
#      and anything else untracked on the server stay as they are.
#   3. Apply every deploy/migration_*.sql, each in its own transaction.
#      A file whose objects already exist fails on its own and is reported
#      as "already applied"; nothing else is touched (all are DDL-only).
#   4. Rebuild and recreate only the `api` container, then restart nginx
#      (nginx caches the api container's IP — see DEPLOY.md). The shared
#      nginx also fronts ocrdocflow.qumaxa.com; a restart is a one-second blip.
#   5. Verify /admin/, /portal/ and the API, and show the crontab.
#
# Never runs `docker compose down`. Never touches the Postgres volume.
set -euo pipefail

COMMIT="${1:-HEAD}"
HOST="${2:-root@165.22.240.27}"
KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_DIR=/opt/console
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST")

cd "$(git rev-parse --show-toplevel)"
SHA=$(git rev-parse --short "$COMMIT")
STAMP=$(date -u +%Y%m%d-%H%M%S)
ARCHIVE="console-$SHA.tar.gz"

echo "== Releasing $COMMIT ($SHA) to $HOST:$REMOTE_DIR"
git archive --format=tar.gz -o "/tmp/$ARCHIVE" "$COMMIT"
scp -i "$KEY" "/tmp/$ARCHIVE" "$HOST:/tmp/$ARCHIVE"
rm -f "/tmp/$ARCHIVE"

"${SSH[@]}" bash -s -- "$SHA" "$STAMP" "$ARCHIVE" <<'REMOTE'
set -euo pipefail
SHA="$1"; STAMP="$2"; ARCHIVE="$3"
cd /opt/console

echo "== 1. Backup"
mkdir -p /opt/backups
docker exec deploy-db-1 pg_dump -U console -d console --no-owner | gzip > "/opt/backups/console-$STAMP-pre-$SHA.sql.gz"
ls -la "/opt/backups/console-$STAMP-pre-$SHA.sql.gz"

echo "== 2. Code"
if [ -f .release-sha ]; then echo "   previous release: $(cat .release-sha)"; fi
tar -xzf "/tmp/$ARCHIVE" -C /opt/console
rm -f "/tmp/$ARCHIVE"
echo "$SHA $STAMP" > .release-sha
echo "   now at $SHA"

echo "== 3. Migrations"
for f in deploy/migration_*.sql; do
  if docker exec -i deploy-db-1 psql -U console -d console -v ON_ERROR_STOP=1 --single-transaction -q < "$f" >/tmp/mig.log 2>&1; then
    echo "   applied          $f"
  elif grep -qi "already exists" /tmp/mig.log; then
    echo "   already applied  $f"
  else
    echo "   FAILED           $f"; cat /tmp/mig.log; exit 1
  fi
done

echo "== 4. Rebuild api, restart nginx"
cd deploy
docker compose up -d --build --no-deps api
sleep 3
docker compose restart nginx
cd ..

echo "== 5. Verify"
sleep 5
for path in /admin/ /portal/ /openapi.json; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --resolve console.qumaxa.com:443:127.0.0.1 "https://console.qumaxa.com$path" || true)
  echo "   $path -> HTTP $code"
done
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'deploy-|ocrdocflow|NAMES'
echo "== crontab (license watcher + master agent expected)"
crontab -l 2>/dev/null || echo "   (no crontab installed)"
docker compose -f deploy/docker-compose.yml logs --tail=15 api
REMOTE

echo "== Done. Rollback: on the droplet, gunzip -c /opt/backups/console-$STAMP-pre-$SHA.sql.gz | docker exec -i deploy-db-1 psql -U console -d console  (after dropping/recreating the schema), and re-run this script with the previous commit."
