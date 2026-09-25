# Aegis 2.0 — developer entrypoints
SHELL := /bin/bash
COMPOSE := docker compose -f infra/docker/docker-compose.yml --env-file .env --project-name aegis-2-0

.PHONY: help up down logs ps build test test-web lint typecheck fmt migrate seed graph clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	 awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up:            ## start the full local stack
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

seed:          ## seed topology and drive one demo incident end to end
	bash scripts/seed-demo.sh

test-web:      ## run the frontend test suite
	cd frontend && npm run test

graph:         ## rebuild the graphify knowledge graph
	graphify update .
