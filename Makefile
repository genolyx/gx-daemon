SHELL := /bin/bash
GIT_SHA := $(shell git rev-parse --short HEAD)

COMPOSE := docker compose
COMPOSE_DEV := $(COMPOSE) -f docker-compose-dev.yml

.PHONY: run-dev run-prod reload-dev down-dev down-prod rebuild-dev rebuild-prod \
        verify-dev verify-prod smoke-dev smoke-prod logs-dev logs-prod ps

# ===== DEV (dev Portal → dev-api.genolyx.com, host :8011) =====
run-dev:
	@mkdir -p session/dev
	@ENV_FILE=.env.dev GIT_SHA=$(GIT_SHA) \
	$(COMPOSE) -p dev --env-file .env.dev up -d --build --force-recreate

reload-dev:
	@mkdir -p session/dev
	@ENV_FILE=.env.dev GIT_SHA=$(GIT_SHA) \
	$(COMPOSE_DEV) -p dev --env-file .env.dev up --build --force-recreate

rebuild-dev:
	@mkdir -p session/dev
	@ENV_FILE=.env.dev GIT_SHA=$(GIT_SHA) \
	$(COMPOSE) -p dev --env-file .env.dev build --no-cache --pull && \
	ENV_FILE=.env.dev GIT_SHA=$(GIT_SHA) \
	$(COMPOSE) -p dev --env-file .env.dev up -d --force-recreate

verify-dev:
	@CID=$$($(COMPOSE) -p dev ps -q gx-daemon); \
	echo "CID=$$CID"; \
	docker inspect $$CID --format 'container.image={{.Image}}' || true

smoke-dev:
	@docker exec -it dev-gx-daemon bash -lc '\
	  set -e; \
	  echo "[who]"; id; \
	  echo "[bin]"; which docker; docker --version; \
	  echo "[sock]"; ls -l /var/run/docker.sock; \
	  echo "[ps]"; docker ps | head \
	'

logs-dev:
	@docker logs -f dev-gx-daemon

down-dev:
	@$(COMPOSE) -p dev down

# ===== PROD (prod Portal → api.genolyx.com, host :8010) =====
run-prod:
	@mkdir -p session/prod
	@ENV_FILE=.env GIT_SHA=$(GIT_SHA) \
	$(COMPOSE) -p prod --env-file .env up -d --build --force-recreate

rebuild-prod:
	@mkdir -p session/prod
	@ENV_FILE=.env GIT_SHA=$(GIT_SHA) \
	$(COMPOSE) -p prod --env-file .env build --no-cache --pull && \
	ENV_FILE=.env GIT_SHA=$(GIT_SHA) \
	$(COMPOSE) -p prod --env-file .env up -d --force-recreate

verify-prod:
	@CID=$$($(COMPOSE) -p prod ps -q gx-daemon); \
	echo "CID=$$CID"; \
	docker inspect $$CID --format 'container.image={{.Image}}' || true

smoke-prod:
	@docker exec -it prod-gx-daemon bash -lc '\
	  set -e; \
	  echo "[who]"; id; \
	  echo "[bin]"; which docker; docker --version; \
	  echo "[sock]"; ls -l /var/run/docker.sock; \
	  echo "[ps]"; docker ps | head \
	'

deploy-dev:
	@mkdir -p session/dev
	@ENV_FILE=.env.dev $(COMPOSE) -p dev --env-file .env.dev down
	@ENV_FILE=.env.dev $(COMPOSE) -p dev --env-file .env.dev up -d --build

deploy-prod:
	@mkdir -p session/prod
	@ENV_FILE=.env $(COMPOSE) -p prod --env-file .env down
	@ENV_FILE=.env $(COMPOSE) -p prod --env-file .env up -d --build

logs-prod:
	@docker logs -f prod-gx-daemon

down-prod:
	@$(COMPOSE) -p prod down

# ===== Misc =====
ps:
	@docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}"
