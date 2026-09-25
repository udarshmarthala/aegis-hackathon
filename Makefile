# Aegis 2.0 — developer entrypoints
SHELL := /bin/bash
COMPOSE := docker compose -f infra/docker/docker-compose.yml --env-file .env --project-name aegis-2-0
# The backend venv's interpreter, on Windows or POSIX.
PY := $(firstword $(abspath $(wildcard backend/.venv/Scripts/python.exe backend/.venv/bin/python)) python)

# ---- reference workload images (INC-043) --------------------------------------
# One Dockerfile, two versions. 1.4.2 is the bad deploy: it ships the pool leak
# and the upgraded dependency. The dependency is the one the known-issue fixture
# names (backend/tests/fixtures/nimble_known_issue.json, `component`/`version`),
# read from that file so the image and the evidence cannot drift apart.
WORKLOAD_IMAGE := aegis-2.0-workload
WORKLOAD_GOOD_TAG := 1.4.1
WORKLOAD_BAD_TAG := 1.4.2
KNOWN_ISSUE_FIXTURE := backend/tests/fixtures/nimble_known_issue.json
DEPENDENCY_NAME := $(shell $(PY) -c "import json;print(json.load(open('$(KNOWN_ISSUE_FIXTURE)'))['component'])" 2>/dev/null || echo httpcore)
DEPENDENCY_VERSION := $(shell $(PY) -c "import json;print(json.load(open('$(KNOWN_ISSUE_FIXTURE)'))['version'])" 2>/dev/null || echo 1.0.9)
# The release before the upgrade (httpx 0.28 accepts any httpcore 1.x). The
# compose build args default to the same values, so `up --build` reproduces the
# healthy image instead of replacing it with an unlabelled one.
DEPENDENCY_PREVIOUS_VERSION := 1.0.7

.PHONY: help up down logs ps build test test-web lint typecheck fmt migrate seed graph clean
.PHONY: workload-images inject-043 reset-043 kill-worker smoke-bedrock demo-scripted seed-horizon

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	 awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: workload-images ## start the full local stack
	$(COMPOSE) up -d --build

down:          ## stop the stack, keep volumes
	$(COMPOSE) down

logs:          ## tail all service logs
	$(COMPOSE) logs -f --tail=120

test:          ## run the backend test suite
	cd backend && python -m pytest -q

lint:          ## ruff + eslint
	cd backend && python -m ruff check src tests
	cd frontend && npm run lint

typecheck:     ## mypy + tsc
	cd backend && python -m mypy src
	cd frontend && npx tsc --noEmit

ps:            ## show stack status
	$(COMPOSE) ps

migrate:       ## apply database migrations (the api and worker also do this on boot)
	$(COMPOSE) exec -T api python -m aegis.persistence.migrate

seed:          ## seed topology, drive one demo incident, then seed INC-042 memory
	bash scripts/seed-demo.sh
	$(PY) scripts/seed_horizon.py

seed-horizon:  ## seed only the INC-042 memory and remediation history (idempotent)
	$(PY) scripts/seed_horizon.py

workload-images: ## build aegis-2.0-workload:1.4.1 (healthy) and :1.4.2 (pool leak)
	docker build -t $(WORKLOAD_IMAGE):$(WORKLOAD_GOOD_TAG) \
	  --build-arg WORKLOAD_VERSION=$(WORKLOAD_GOOD_TAG) \
	  --build-arg DEPENDENCY_NAME=$(DEPENDENCY_NAME) \
	  --build-arg DEPENDENCY_VERSION=$(DEPENDENCY_PREVIOUS_VERSION) workload
	docker build -t $(WORKLOAD_IMAGE):$(WORKLOAD_BAD_TAG) \
	  --build-arg WORKLOAD_VERSION=$(WORKLOAD_BAD_TAG) \
	  --build-arg BAKED_FAULT=pool_leak \
	  --build-arg DEPENDENCY_NAME=$(DEPENDENCY_NAME) \
	  --build-arg DEPENDENCY_VERSION=$(DEPENDENCY_VERSION) workload

inject-043: workload-images ## the bad deploy: checkout -> 1.4.2 via the runtime adapter, recorded
	$(PY) scripts/inject_043.py

reset-043:     ## checkout back to 1.4.1 via the runtime adapter, fault cleared
	$(PY) scripts/reset_043.py

kill-worker:   ## SIGKILL the worker mid-incident, bring it back, show where it resumed
	@# `docker kill` counts as a manual stop, which restart policies honour, so
	@# the container is started explicitly. A crash from inside the process (the
	@# war room's kill switch) is restarted by the policy on its own.
	@cid=$$($(COMPOSE) ps -q worker); \
	 if [ -z "$$cid" ]; then echo "worker is not running"; exit 1; fi; \
	 since=$$(date -u +%Y-%m-%dT%H:%M:%SZ); \
	 docker kill --signal=SIGKILL $$cid >/dev/null && echo "worker killed (SIGKILL, no drain)"; \
	 sleep 3; \
	 if [ "$$(docker inspect -f '{{.State.Running}}' $$cid)" != "true" ]; then \
	   docker start $$cid >/dev/null && echo "worker started again"; \
	 fi; \
	 for i in $$(seq 1 20); do \
	   line=$$(docker logs --since $$since $$cid 2>&1 | grep -iE 'resum' | tail -n 3); \
	   if [ -n "$$line" ]; then echo "$$line"; exit 0; fi; \
	   sleep 1; \
	 done; \
	 echo "no resume logged yet (no incident in flight?) - see: $(COMPOSE) logs worker"

smoke-bedrock: ## one real Bedrock call through the configured AWS profile
	$(PY) scripts/smoke_bedrock.py

demo-scripted: ## the INC-043 golden path in scripted mode: zero network, zero keys
	cd backend && $(PY) -m pytest -q -p no:cacheprovider -k golden tests/unit
