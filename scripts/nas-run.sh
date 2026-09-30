#!/bin/sh
set -eu

PROJECT_DIR="${PROJECT_DIR:-/volume1/docker/apt-gap-monitor}"
cd "$PROJECT_DIR"

# mkdir is atomic and available on DSM even when flock is not installed.
# Keep the lock on interruption: Docker may still be running in the daemon.
LOCK_DIR="$PROJECT_DIR/.collector.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -d "$LOCK_DIR" ]; then
    echo "Collector lock exists; skipping overlapping run: $LOCK_DIR" >&2
    exit 0
  fi
  echo "Cannot create collector lock: $LOCK_DIR" >&2
  exit 1
fi
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

status=0
if docker compose version >/dev/null 2>&1; then
  docker compose pull collector && docker compose run --rm collector || status=$?
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose pull collector && docker-compose run --rm collector || status=$?
else
  echo "Docker Compose를 찾을 수 없습니다." >&2
  status=1
fi
rmdir "$LOCK_DIR"
exit "$status"
