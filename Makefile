# One place for the commands that must be identical locally and in CI. Summon-bot had a
# Procfile, a run.sh and three ad-hoc asyncio.run entry points that each set the
# environment differently — this file plus .github/workflows/ci.yml is the same list.
#: Prefer the project venv when one exists (``make install`` creates it), so the same
#: interpreter runs in CI, in a container and on a laptop that happens to have another
#: Python first on PATH — "make check works for me" is not a check.
VENV_PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,)
PY ?= $(or $(VENV_PY),python)
UV ?= uv

.PHONY: help install run test lint format docs check migrate seed doctor jobs import-legacy docker-build docker-up

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## create the venv and install runtime + dev dependencies
	$(UV) sync --all-extras 2>/dev/null || pip install -e ".[dev]"

run:  ## run the bot (long polling; MODE=webhook for webhooks)
	$(PY) -m waifu bot

test:  ## the test suite (in-memory SQLite, real services, no mocks of our own layers)
	$(PY) -m pytest tests/ -q

lint:  ## ruff + the generated docs must be current
	$(PY) -m ruff check waifu tests scripts
	$(PY) -m ruff format --check waifu tests scripts
	$(PY) scripts/gen_reference_docs.py --check

format:  ## apply ruff's fixes and formatting
	$(PY) -m ruff check waifu tests scripts --fix
	$(PY) -m ruff format waifu tests scripts

docs:  ## regenerate docs/COMMANDS.md and docs/SUMMON_PARITY.md from the routers
	$(PY) scripts/gen_reference_docs.py

check: lint test  ## what CI runs

migrate:  ## create/upgrade the schema + tier ladders (the roster is uploaded, not shipped)
	$(PY) -m waifu migrate

seed:  ## ladders, plus the optional catalogue with `make seed CATALOGUE=1`
	$(PY) -m waifu seed $(if $(CATALOGUE),--catalogue,)

doctor:  ## config + database + plugin-registration self-check
	$(PY) -m waifu doctor

jobs:  ## one scheduler pass (cron-friendly alternative to the resident loop)
	$(PY) -m waifu jobs --name $(or $(NAME),all)

import-legacy:  ## migrate a Summon-bot database: make import-legacy DB=./summon.db
	$(PY) -m waifu import-legacy "$(DB)" $(if $(DRY),--dry-run,)

docker-build:  ## build the image
	docker build -t waifu-bot .

docker-up:  ## bot + postgres + redis
	docker compose up --build -d
