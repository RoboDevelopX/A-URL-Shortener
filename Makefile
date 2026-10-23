# Developer entry points. Everything except `up`/`down`/`logs` needs uv on the host
# (https://docs.astral.sh/uv/); the stack itself only needs Docker.

COMPOSE ?= docker compose
UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help up down logs migrate test lint format

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-10s %s\n", $$1, $$2}'

up: ## Build and start the full stack, waiting until it is healthy
	$(COMPOSE) up -d --build --wait

down: ## Stop the stack (add V=1 to also delete volumes)
	$(COMPOSE) down $(if $(V),-v,)

logs: ## Tail logs from all services
	$(COMPOSE) logs -f

migrate: ## Apply database migrations
	$(COMPOSE) run --rm migrate

test: ## Run unit + integration tests with coverage (starts postgres and redis)
	$(COMPOSE) up -d --wait postgres redis
	$(UV) run pytest --cov --cov-report=term-missing

lint: ## Ruff lint, Ruff format check and mypy
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

format: ## Auto-fix lint issues and format code
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
