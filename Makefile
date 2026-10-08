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
	migrate config-check seed demo demo-reset tagmap ml-dataset ml-train forecast-validate

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
	$(UV) run pytest -m "not integration and not ml and not validation" -q

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

demo: ## migrations -> seed -> backfill -> case import -> live (M4)
	@echo "make demo: not implemented until M4"

demo-reset: ## reset the live tail to demo_start (M4/M9)
	@echo "make demo-reset: not implemented until M4"

tagmap: ## generate config/tag_map.demo.yaml from the OPC UA address space (M2)
	$(UV) run --package qost-sim python -m qost_sim tagmap --out config/tag_map.demo.yaml

ML_RAW := ml/data/raw
ML_FEATURES := ml/data/features

ml-dataset: ## PdM dataset: sim ml-dataset (12 months, seed 7) -> 15-min features + labels (M7)
	$(UV) run --package qost-sim python -m qost_sim ml-dataset --out $(ML_RAW)
	$(UV) run --package qost-ml python -m qost_ml dataset --raw $(ML_RAW) --out $(ML_FEATURES)

forecast-validate: ## fast forecast vs the virtual plant: bias and P10-P90 coverage over 60 seeds (M6)
	$(UV) run --package qost-sim python -m qost_sim.forecast_validation --seeds 60

ml-train: ## train LightGBM robot/conveyor -> ml/models, model cards, T-ML metric checks (M7)
	@test -f $(ML_FEATURES)/meta.json || $(MAKE) ml-dataset
	$(UV) run --package qost-ml python -m qost_ml train --features $(ML_FEATURES) --models ml/models
	$(UV) run pytest -m ml -q
