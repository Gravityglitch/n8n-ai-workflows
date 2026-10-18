.DEFAULT_GOAL := help
.PHONY: help install lint fmt test run lint-workflows up down logs build

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies with uv
	uv sync

lint: ## Ruff lint + format check + workflow lint
	uv run ruff check .
	uv run ruff format --check .
	uv run python -m tools.lint_workflows workflows/ --strict

fmt: ## Auto-format and auto-fix
	uv run ruff format .
	uv run ruff check --fix .

test: ## Tests with the 80% coverage gate
	uv run pytest -q --cov=sidecar --cov=tools --cov-report=term-missing --cov-fail-under=80

run: ## Run the sidecar locally with reload (fixture LLM, dry-run adapters)
	uv run uvicorn sidecar.api:app --reload

lint-workflows: ## Validate every workflow JSON under workflows/
	uv run python -m tools.lint_workflows workflows/ --strict

up: ## Start n8n + sidecar with docker compose
	docker compose up --build -d

down: ## Stop the stack
	docker compose down

logs: ## Follow sidecar logs
	docker compose logs -f sidecar

build: ## Build the sidecar image
	docker build -t n8n-ai-sidecar .
