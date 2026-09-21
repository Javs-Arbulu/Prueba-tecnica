.DEFAULT_GOAL := help
COMPOSE := docker compose

help: ## Show the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the stack (migrated + seeded)
	$(COMPOSE) up --build

down: ## Stop the stack
	$(COMPOSE) down

reset: ## Stop the stack and drop the database volume
	$(COMPOSE) down -v

shell: ## Open a shell inside the web container
	$(COMPOSE) run --rm web bash

test: ## Run the test suite with coverage
	$(COMPOSE) run --rm web test --cov --cov-report=term-missing

lint: ## Run ruff and black in check mode
	$(COMPOSE) run --rm web bash -lc "ruff check . && black --check ."

typecheck: ## Run mypy over the application code
	$(COMPOSE) run --rm web mypy apps config

format: ## Apply ruff --fix and black
	$(COMPOSE) run --rm web bash -lc "ruff check --fix . && black ."

migrations: ## Create migrations
	$(COMPOSE) run --rm web python manage.py makemigrations

migrate: ## Apply migrations
	$(COMPOSE) run --rm web python manage.py migrate

seed: ## Load the demo dataset
	$(COMPOSE) run --rm web python manage.py seed_demo

schema: ## Regenerate docs/openapi.yaml
	$(COMPOSE) run --rm web python manage.py spectacular --file docs/openapi.yaml

.PHONY: help up down reset shell test lint typecheck format migrations migrate seed schema
