#!/usr/bin/env bash
# prod 기동/관리 (nipt-daemon deploy.sh / Makefile run-prod 와 동일 계열)
#
# 사용법:
#   ./build.sh              # prod 이미지 빌드만
#   ./build.sh up           # make run-prod 와 동일
#   ./build.sh down         # make down-prod
#   ./build.sh restart      # down → up
#   ./build.sh logs         # docker logs prod-gx-daemon
#   ./build.sh status       # compose ps
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "Missing .env — copy and edit:"
  echo "  cp .env.example .env"
  exit 1
fi

CMD="${1:-build}"

case "$CMD" in
  build)
    ENV_FILE=.env docker compose -p prod --env-file .env build
    ;;
  up)
    make run-prod
    ;;
  down)
    make down-prod
    ;;
  restart)
    make down-prod
    make run-prod
    ;;
  logs)
    make logs-prod
    ;;
  status)
    docker compose -p prod ps
    ;;
  *)
    echo "Usage: $0 [build|up|down|restart|logs|status]" >&2
    exit 1
    ;;
esac
