# anon-tool — one entry point for both ways of running it.
#
#   make up        start the container (http://127.0.0.1:1407)
#   make down      stop it
#   make logs      follow its log
#   make native    run the same server directly (no Docker)
#   make test      run the full suite (engine + web + UI load check)
#   make smoke     build and exercise the container end to end

IMAGE  ?= anon-tool:local
PORT   ?= 1407
ANON_HOME ?= $(HOME)/.anon
COMPOSE ?= docker compose

.PHONY: help up down logs restart build native test smoke clean

help:
	@grep -E '^[a-z-]+:' $(MAKEFILE_LIST) | cut -d: -f1 | sed 's/^/  make /'

up: ## start the container (loopback only)
	$(COMPOSE) up -d --build
	@echo "anon-tool on http://127.0.0.1:$(PORT)"

down: ## stop the container
	$(COMPOSE) down

logs: ## follow the container log
	$(COMPOSE) logs -f --tail=50

restart: down up ## restart it

build: ## build the image only
	docker build -t $(IMAGE) .

native: ## run the server directly, without Docker
	python3 web/server.py --port $(PORT)

test: ## engine + web + UI load check (no Docker needed)
	python3 tests/test_anon.py
	python3 tests/test_web.py
	node tests/ui_load_check.mjs

smoke: ## build the container and exercise every endpoint
	bash scripts/smoke-docker.sh

clean: ## remove the image (the data in $(ANON_HOME) is untouched)
	docker rmi $(IMAGE) 2>/dev/null || true
