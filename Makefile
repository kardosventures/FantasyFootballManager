SHELL := /bin/zsh
DOCKER ?= $(shell command -v docker 2>/dev/null || echo /Applications/Docker.app/Contents/Resources/bin/docker)
COMPOSE := $(DOCKER) compose

.PHONY: bootstrap probe dev web api scheduler draft-monitor worker sync sync-sleeper sync-rankings sync-in-season sync-fantasypros historical-backfill manager projection-backtest championship-backtest league-readiness event-calendar draft-room notification-test test lint format migrate doctor backup restore rehydrate-docker replay-draft replay-events browser-agent browser-login browser-qualify-waiver browser-qualify-waiver-submit browser-qualify-waiver-reconcile codex-agent mac-agent install-launchd

bootstrap:
	@test -f .env || cp .env.example .env
	$(MAKE) doctor
	$(COMPOSE) build
	$(COMPOSE) run --rm migrate
	$(COMPOSE) run --rm api python -m app.cli bootstrap

probe:
	cd backend && ../.venv/bin/python -m app.cli probe

dev:
	$(COMPOSE) up --build

web:
	cd frontend && corepack pnpm install && corepack pnpm dev

api:
	cd backend && ../.venv/bin/python -m uvicorn app.main:app --reload

scheduler:
	cd backend && ../.venv/bin/python -m app.scheduler

draft-monitor:
	cd backend && ../.venv/bin/python -m app.draft_monitor

worker:
	cd backend && ../.venv/bin/python -m app.worker

sync sync-sleeper league-readiness:
	$(COMPOSE) run --rm api python -m app.cli bootstrap

sync-rankings:
	$(COMPOSE) run --rm api python -m app.cli rankings

sync-in-season:
	$(COMPOSE) run --rm api python -m app.cli in-season

sync-fantasypros:
	$(COMPOSE) run --rm api python -m app.cli fantasypros

historical-backfill:
	$(COMPOSE) run --rm api python -m app.cli historical-backfill

manager:
	$(COMPOSE) run --rm api python -m app.cli manager

projection-backtest:
	$(COMPOSE) run --rm api python -m app.cli projection-backtest

championship-backtest:
	$(COMPOSE) run --rm api python -m app.cli championship-backtest

draft-room:
	$(COMPOSE) run --rm api python -m app.cli draft-room

event-calendar:
	$(COMPOSE) run --rm api python -m app.cli calendar

replay-draft:
	$(COMPOSE) run --rm api python -m app.cli replay-draft

replay-events:
	$(COMPOSE) run --rm api python -m app.cli replay-events

test:
	cd backend && ../.venv/bin/python -m pytest tests
	cd frontend && corepack pnpm test
	cd browser-agent && corepack pnpm test
	cd codex-agent && node --test

lint:
	.venv/bin/python -m ruff check backend
	cd frontend && corepack pnpm lint
	cd browser-agent && corepack pnpm check
	cd codex-agent && node --check src/*.mjs

format:
	.venv/bin/python -m ruff format backend
	cd frontend && corepack pnpm format

migrate:
	$(COMPOSE) run --rm migrate

doctor:
	./scripts/doctor.sh

backup:
	./scripts/backup.sh

restore:
	@echo "Usage: ./scripts/restore.sh /absolute/path/to/backup.sql.gz"

rehydrate-docker:
	./scripts/rehydrate-docker.sh --apply

browser-agent:
	cd browser-agent && corepack pnpm start

codex-agent:
	cd codex-agent && node --env-file-if-exists=../.env src/main.mjs

browser-login:
	cd browser-agent && corepack pnpm run login

browser-qualify-waiver:
	cd browser-agent && corepack pnpm run qualify:waiver

browser-qualify-waiver-submit:
	cd browser-agent && corepack pnpm run qualify:waiver-submit

browser-qualify-waiver-reconcile:
	cd browser-agent && corepack pnpm run qualify:waiver-reconcile

mac-agent:
	@echo "The Swift mac-agent is deprecated. Use 'make browser-agent'."

install-launchd:
	./scripts/install-launchd.sh

notification-test:
	PYTHONPATH=backend .venv/bin/python scripts/send-test-slack.py
