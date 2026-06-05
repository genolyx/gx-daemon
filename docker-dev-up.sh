#!/usr/bin/env bash
# 개발용: 소스 마운트 + uvicorn --reload (docker-compose-dev.yml)
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env.dev ]]; then
  echo "Missing .env.dev — copy and edit:"
  echo "  cp .env.dev.example .env.dev"
  echo "Set HOST_UID, HOST_GID, DOCKER_GID from: id -u; id -g; getent group docker"
  exit 1
fi

mkdir -p session/dev
exec env ENV_FILE=.env.dev docker compose -f docker-compose-dev.yml -p dev \
  --env-file .env.dev up --build --force-recreate "$@"
