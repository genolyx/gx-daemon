#!/usr/bin/env bash
# 프로덕션 이미지 빌드 + 서비스 기동/관리 (docker-compose.yml)
# 사용 전: cp .env.compose.example .env.compose && 편집
#
# 사용법:
#   ./build.sh              # 이미지 빌드만
#   ./build.sh up           # 이미지 빌드 + 컨테이너 기동
#   ./build.sh down         # 컨테이너 중지 및 제거
#   ./build.sh restart      # down → build → up
#   ./build.sh logs         # 실시간 로그 조회
#   ./build.sh status       # 컨테이너 상태 확인
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env.compose ]]; then
  echo "Missing .env.compose — copy and edit:"
  echo "  cp .env.compose.example .env.compose"
  echo "Set HOST_UID, HOST_GID, DOCKER_GID from: id -u; id -g; getent group docker"
  exit 1
fi

CMD="${1:-build}"

case "$CMD" in
  build)
    docker compose --env-file .env.compose up --build --no-start
    ;;
  up)
    docker compose --env-file .env.compose up -d --build
    docker compose --env-file .env.compose ps
    ;;
  down)
    docker compose --env-file .env.compose down
    ;;
  restart)
    docker compose --env-file .env.compose down
    docker compose --env-file .env.compose up -d --build
    docker compose --env-file .env.compose ps
    ;;
  logs)
    docker compose --env-file .env.compose logs -f
    ;;
  status)
    docker compose --env-file .env.compose ps
    ;;
  *)
    echo "Usage: $0 [build|up|down|restart|logs|status]" >&2
    exit 1
    ;;
esac
