#!/usr/bin/env bash
# Deploy (or update) the clipper on a Linux server over SSH.
#
#   deploy/deploy.sh ubuntu@203.0.113.10 [-i ~/.ssh/key.pem]
#
# Copies the code, config.toml and secrets/ (API keys, OAuth tokens), builds
# the image on the server and (re)starts it behind Caddy with HTTPS and a
# dashboard password. Re-run it after editing config.toml or secrets/.
# Domain: $CLIPPER_DOMAIN (default clipper.vsrppl.com).
set -euo pipefail

HOST="${1:-}"
[ -n "$HOST" ] || { sed -n '2,10p' "$0"; exit 1; }
shift
KEY=""
while [ $# -gt 0 ]; do
  case "$1" in
    -i) KEY="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done

DOMAIN="${CLIPPER_DOMAIN:-clipper.vsrppl.com}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE=/opt/gta6clipper
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30"
[ -n "$KEY" ] && SSH_OPTS="$SSH_OPTS -i $KEY -o IdentitiesOnly=yes"
# shellcheck disable=SC2086
ssh_() { ssh $SSH_OPTS "$HOST" "$@"; }

[ -f "$ROOT/config.toml" ] || { echo "config.toml missing (run: .venv/bin/clipper init)"; exit 1; }

echo "==> server setup"
ssh_ 'bash -s' < "$ROOT/deploy/setup-server.sh"

echo "==> dashboard password"
mkdir -p "$ROOT/secrets"
PW_FILE="$ROOT/secrets/dashboard.txt"
if [ ! -s "$PW_FILE" ]; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(18))' > "$PW_FILE"
  echo "    new password saved to secrets/dashboard.txt (user: admin)"
fi
chmod 600 "$PW_FILE"

echo "==> copying code, config and secrets"
rsync -az --delete -e "ssh $SSH_OPTS" \
  --exclude .git --exclude .venv --exclude data --exclude models --exclude secrets \
  --exclude config.toml --exclude deploy/.env --exclude '__pycache__' --exclude '*.egg-info' \
  "$ROOT/" "$HOST:$REMOTE/"
rsync -az -e "ssh $SSH_OPTS" "$ROOT/config.toml" "$HOST:$REMOTE/config.toml"
# (macOS's built-in openrsync has no --chmod, so lock the folder down afterwards)
rsync -az -e "ssh $SSH_OPTS" --exclude dashboard.txt --exclude auth.caddy --exclude instagram_tokens.json \
  "$ROOT/secrets/" "$HOST:$REMOTE/secrets/"
ssh_ "chmod 700 $REMOTE/secrets && find $REMOTE/secrets -type f -exec chmod 600 {} +"

echo "==> building and starting (first build compiles whisper.cpp: a few minutes)"
# the password travels over SSH stdin, never on a command line here
ssh_ "set -e; cd $REMOTE
  printf 'CLIPPER_DOMAIN=%s\n' '$DOMAIN' > deploy/.env
  PW=\$(tr -d '\r\n')  # a Windows-made password file ends in CRLF
  HASH=\$(sudo docker run --rm caddy:2 caddy hash-password --plaintext \"\$PW\")
  printf 'basic_auth {\n\tadmin %s\n}\n' \"\$HASH\" > secrets/auth.caddy
  chmod 600 secrets/auth.caddy
  cd deploy
  sudo docker compose up -d --build --remove-orphans
  sudo docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true
  sudo docker compose ps" < "$PW_FILE"

echo "==> checking https://$DOMAIN"
code=$(printf 'user = "admin:%s"\n' "$(tr -d '\r\n' < "$PW_FILE")" \
  | curl -s -K - -o /dev/null -w '%{http_code}' --max-time 20 "https://$DOMAIN/api/summary" || true)
if [ "$code" = "200" ]; then
  echo "    live: https://$DOMAIN  (user admin, password in secrets/dashboard.txt)"
else
  echo "    not reachable yet (HTTP $code). If DNS was just added, wait a few minutes and open https://$DOMAIN"
fi
