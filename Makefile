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
SLIM_PORT ?= 1408
ANON_HOME ?= $(HOME)/.anon
COMPOSE ?= docker compose

.PHONY: help up up-slim down logs restart build native test smoke clean model model-stop bench-model check-container recall coverage mutate hooks backup restore

help:
	@grep -E '^[a-z-]+:' $(MAKEFILE_LIST) | cut -d: -f1 | sed 's/^/  make /'

up: ## start the container (loopback only). MODEL=1 keeps the suggestion model attached
	$(COMPOSE) $(if $(MODEL),-f docker-compose.yml -f docker-compose.model.yml,) up -d --build
	@echo "anon-tool on http://127.0.0.1:$(PORT)"
	@python3 scripts/check-container-fresh.py  # a build that is up but stale is not "up"

up-slim: ## start the text-only variant (no converter, port $(SLIM_PORT))
	# The port is passed explicitly: compose reads ANON_SLIM_PORT, and printing a different
	# variable than the one that binds the socket is how a URL ends up being wrong.
	ANON_SLIM_PORT=$(SLIM_PORT) $(COMPOSE) --profile slim up -d --build anon-tool-slim
	@echo "anon-tool (slim) on http://127.0.0.1:$(SLIM_PORT)"
	@python3 scripts/check-container-fresh.py  # the same invariant on the slim path

down: ## stop the container and the suggestion model, if it is running
	# The model override is included on purpose: bringing down only the default project leaves the
	# model container running (it shares the UI's network namespace, so an orphan keeps the socket).
	$(COMPOSE) -f docker-compose.yml -f docker-compose.model.yml down

logs: ## follow the container log
	$(COMPOSE) logs -f --tail=50

restart: down up ## restart it

build: ## build the image only
	docker build -t $(IMAGE) .

native: ## run the server directly, without Docker
	python3 web/server.py --port $(PORT)

test: ## engine + web + local-model seam + backup + MCP + git guard + UI load + doc numbers (no Docker)
	python3 tests/test_anon.py
	python3 tests/test_web.py
	python3 tests/test_suggest.py
	python3 tests/test_backup.py
	python3 tests/test_mcp.py
	python3 tests/test_git_guard.py
	node tests/ui_load_check.mjs
	python3 scripts/check-doc-numbers.py

hooks: ## install git-hooks/ into .git/hooks/ (pre-commit refuses clean/smudge filters)
	bash scripts/install-git-hooks.sh

model: ## download the local suggestion model (~2 GB, checksum-checked) and start it beside the UI
	bash scripts/fetch-suggest-model.sh
	$(COMPOSE) -f docker-compose.yml -f docker-compose.model.yml up -d

model-stop: ## stop the UI and the suggestion model
	$(COMPOSE) -f docker-compose.yml -f docker-compose.model.yml down

bench-model: ## measure the seam against a loopback model: make bench-model URL=... MODEL=...
	python3 scripts/bench-suggest.py --url "$${URL:?usage: make bench-model URL=<endpoint> MODEL=<name>}" --model "$${MODEL:?usage: make bench-model URL=<endpoint> MODEL=<name>}"

smoke: ## build the container and exercise every endpoint
	bash scripts/smoke-docker.sh

check-container: ## fail if a running container does not serve the current code
	python3 scripts/check-container-fresh.py

recall: ## measure the engine's recall on the labelled corpus (fp-sweep measures the other direction)
	python3 scripts/recall-sweep.py

coverage: ## how much of the shipped engine the tests run (a floor below fails)
	python3 scripts/coverage.py

mutate: ## break one invariant at a time; every defect must make a test fail
	python3 scripts/mutate.py

backup: ## encrypt the private store (maps + dictionary) into an archive: BACKUP=path to choose it
	bash scripts/anon-home-backup.sh backup $(if $(BACKUP),--out=$(BACKUP),)

restore: ## write a backup back: make restore FILE=<archive> [FORCE=1]
	bash scripts/anon-home-backup.sh restore "$${FILE:?usage: make restore FILE=<archive>}" $(if $(FORCE),--force,)

clean: ## remove the image (the data in $(ANON_HOME) is untouched)
	docker rmi $(IMAGE) 2>/dev/null || true
