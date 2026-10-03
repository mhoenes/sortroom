#!/usr/bin/env bash
# CI: start the image the ways the docs describe and check who Sortroom runs as and who owns the
# mounted folders afterwards.   bash tools/container_check.sh <image>
set -euo pipefail
image=${1:?usage: container_check.sh <image>}
fails=0

# start <name> <docker run options...>: a fresh config/, mailboxes/ and logs/ (owned by the runner,
# not by 1000, or by $OWNER) mounted like in docker-compose.yml; waits until the UI answers
start() {
  local name=$1; shift
  dir=$(mktemp -d)
  mkdir -p "$dir/config" "$dir/mailboxes/box" "$dir/logs"
  cp config/config.toml "$dir/config/"
  if [ -n "${OWNER:-}" ]; then sudo chown -R "$OWNER" "$dir"; fi
  docker run -d --name "$name" -e ADMIN_PASSWORD=ci-password-1 -e SORTROOM_SCHEDULER=off \
    -v "$dir/config:/app/config" -v "$dir/mailboxes:/app/mailboxes" -v "$dir/logs:/app/logs" "$@" "$image" >/dev/null
  for _ in $(seq 30); do
    if docker exec "$name" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2)" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "FAIL $name: the UI did not answer"; docker logs "$name"; fails=$((fails + 1)); return 1
}

# expect <name> <uid:gid>: Sortroom (PID 1 after the entrypoint) runs as uid:gid, and the folders and
# everything in them belong to it
expect() {
  local name=$1 want=$2
  local got owners
  got=$(docker exec "$name" sh -c "awk '/^Uid:/{u=\$2} /^Gid:/{g=\$2} END{print u\":\"g}' /proc/1/status")
  owners=$(sudo find "$dir" -mindepth 1 -printf '%U:%G\n' | sort -u | tr '\n' ' ')
  if [ "$got" = "$want" ] && [ "$owners" = "$want " ]; then
    echo "ok   $name: runs as $got, the folders belong to $owners"
  else
    echo "FAIL $name: runs as $got, folders belong to $owners- expected $want"; docker logs "$name"
    fails=$((fails + 1))
  fi
}

stop() { docker rm -f "$1" >/dev/null; sudo rm -rf "$dir"; }

start default && expect default 1000:1000; stop default
start puid -e PUID=1002 -e PGID=1003 && expect puid 1002:1003; stop puid

# docker run --user: no root at all; the folders must belong to that user already and stay so
OWNER=1004:1004 start user --user 1004:1004 && expect user 1004:1004; stop user

# the docker run command of the wiki page Installation, with .env made from .env.example: the UI answers
# on the host's port and the folders end up with 1000
dir=$(mktemp -d)
mkdir -p "$dir/config" "$dir/mailboxes" "$dir/logs"
cp config/config.toml "$dir/config/"
sed 's/^ADMIN_PASSWORD=$/ADMIN_PASSWORD=ci-password-1/' .env.example > "$dir/.env"
(cd "$dir" && docker run -d --name sortroom --restart unless-stopped \
  -p 8765:8765 \
  --env-file .env -e TZ=Europe/Berlin \
  -v "$PWD/config:/app/config" \
  -v "$PWD/mailboxes:/app/mailboxes" \
  -v "$PWD/logs:/app/logs" \
  "$image" >/dev/null)
for _ in $(seq 30); do curl -fsS -o /dev/null http://127.0.0.1:8765/health && break; sleep 1; done
if curl -fsS http://127.0.0.1:8765/login | grep -q 'name="password"'; then
  echo "ok   docker run from the docs: the login page answers on port 8765"
  rm -f "$dir/.env"  # only the mounted folders count below
  expect sortroom 1000:1000
else
  echo "FAIL docker run from the docs: no login page on port 8765"; docker logs sortroom; fails=$((fails + 1))
fi
stop sortroom

# root is refused
if out=$(docker run --rm -e PUID=0 "$image" 2>&1); then
  echo "FAIL PUID=0 started"; fails=$((fails + 1))
elif grep -q "PUID='0' is not allowed" <<<"$out"; then
  echo "ok   PUID=0 refused: $out"
else
  echo "FAIL PUID=0: unexpected output: $out"; fails=$((fails + 1))
fi

exit "$fails"
