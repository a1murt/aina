# Qost Twin — developer commands (SPEC §18).
# Compatible with GNU make 3.81 (macOS default).

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
UV ?= uv
PNPM ?= pnpm
WEB := web
# PROFILE= (infrastructure only) | demo (all services incl. sim) | prod (all except sim)
PROFILE ?=
PROFILE_FLAGS := $(if $(PROFILE),--profile $(PROFILE),)
ALL_PROFILES := --profile demo --profile prod
# Host-side URLs for tools and integration tests (containers use the values from .env).
HOST_DATABASE_URL ?= postgresql+asyncpg://qost:qost@localhost:5432/qost
HOST_REDIS_URL ?= redis://localhost:6379/0

export NEXT_TELEMETRY_DISABLED := 1

.PHONY: help up down ps logs check test fmt py-lint py-type py-test web-check web-install \
	migrate config-check seed demo demo-reset tagmap ml-dataset ml-train history

help: ## list targets
	@grep -E '^[a-z][a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "} {printf "  %-13s %s\n", $$1, $$2}'

.env:
	cp .env.example .env

# ------------------------------------------------------------------ stack

up: .env ## start infrastructure and wait until healthy (PROFILE=demo|prod adds services)
	$(COMPOSE) $(PROFILE_FLAGS) up -d --build --wait --wait-timeout 300

down: ## stop and remove all containers of every profile (named volumes are kept)
	$(COMPOSE) $(ALL_PROFILES) down --remove-orphans

ps: ## container status
	$(COMPOSE) $(ALL_PROFILES) ps

logs: ## follow logs (all profiles)
	$(COMPOSE) $(ALL_PROFILES) logs -f --tail=100

migrate: up ## apply Alembic migrations to the compose database
	DATABASE_URL=$(HOST_DATABASE_URL) $(UV) run --package qost-api \
		alembic -c services/api/alembic.ini upgrade head

# ------------------------------------------------------------------ quality

check: py-lint py-type py-test web-check ## ruff + mypy + pytest (no integration) + eslint + tsc

py-lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

py-type:
	$(UV) run mypy

py-test:
	$(UV) run pytest -m "not integration" -q

web-check: web-install
	$(PNPM) --dir $(WEB) lint
	$(PNPM) --dir $(WEB) typecheck

web-install: $(WEB)/node_modules/.modules.yaml

$(WEB)/node_modules/.modules.yaml: $(WEB)/package.json $(WEB)/pnpm-lock.yaml
	$(PNPM) --dir $(WEB) install --frozen-lockfile
	@touch $@

test: up ## all tests including integration (starts the infrastructure first)
	TEST_DATABASE_URL=$(HOST_DATABASE_URL) TEST_REDIS_URL=$(HOST_REDIS_URL) \
		$(UV) run pytest -q

fmt: ## format Python and apply safe lint fixes
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

config-check: ## validate config/*.yaml (and the tag map, if present)
	$(UV) run python -m twin_core.config

# ------------------------------------------------------------------ later stages

seed: ## users of all roles, reference data, calendar, plan (M4)
	@echo "make seed: not implemented until M4"

history: ## sim backfill 01.09 -> demo_start into the DB, then the engine recomputes it (run after migrate)
	DATABASE_URL=$(HOST_DATABASE_URL) $(UV) run --package qost-sim \
		python -m qost_sim backfill --sink db: --batch-size 5000
	DATABASE_URL=$(HOST_DATABASE_URL) REDIS_URL=$(HOST_REDIS_URL) $(UV) run --package qost-engine \
		python -m qost_engine replay

demo: ## migrations -> seed -> backfill -> case import -> live (M4)
	@echo "make demo: not implemented until M4"

demo-reset: ## reset the live tail to demo_start (M4/M9)
	@echo "make demo-reset: not implemented until M4"

tagmap: ## generate config/tag_map.demo.yaml from the OPC UA address space (M2)
	$(UV) run --package qost-sim python -m qost_sim tagmap --out config/tag_map.demo.yaml

ml-dataset: ## PdM dataset via sim ml-dataset mode (M7)
	$(UV) run --package qost-ml python -m qost_ml dataset

ml-train: ## train LightGBM models, write model cards (M7)
	$(UV) run --package qost-ml python -m qost_ml train
