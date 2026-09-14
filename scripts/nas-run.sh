#!/bin/sh
set -eu

PROJECT_DIR="${PROJECT_DIR:-/volume1/docker/apt-gap-monitor}"
cd "$PROJECT_DIR"

if docker compose version >/dev/null 2>&1; then
  docker compose pull collector
  docker compose run --rm collector
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose pull collector
  docker-compose run --rm collector
else
  echo "Docker Compose를 찾을 수 없습니다." >&2
  exit 1
fi
