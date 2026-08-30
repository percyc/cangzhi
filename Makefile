SHELL := /bin/bash
.PHONY: up upgrade build down down-volumes logs logs-api logs-web logs-worker ps doctor backup verify-backup migrate migrate-up migrate-create install-python install-node install test-api test-worker test-web test lint-api lint-worker lint typecheck-api typecheck-worker typecheck clean help

# Default target
.DEFAULT_GOAL := help

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

up: ## Start all services with Docker Compose (migrate runs automatically)
	docker compose up -d

upgrade: ## Build current code, run migrations, and recreate changed services
	docker compose up -d --build

build: ## Build all services
	docker compose build

down: ## Stop all services
	docker compose down

down-volumes: ## ⚠️  Stop all services AND DELETE postgres volume (all data lost!)
	docker compose down -v

logs: ## Show logs from all services
	docker compose logs -f

logs-api: ## Show logs from API service
	docker compose logs -f api

logs-web: ## Show logs from Web service
	docker compose logs -f web

logs-worker: ## Show logs from Worker service
	docker compose logs -f worker

ps: ## Show status of running services
	docker compose ps

doctor: ## Check containers, schema revision, and HTTP health endpoints
	@docker compose ps
	@docker compose exec -T postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -Atc "SELECT '\''schema='\'' || version_num FROM alembic_version"'
	@docker compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/readiness').read().decode())"
	@docker compose exec -T web node -e "const raw = (process.env.CANGZHI_WEB_BASE_PATH || '/_cangzhi').trim(); const p = raw && raw !== '/' ? '/' + raw.replace(/^\/+|\/+$$/g, '') : ''; require('http').get('http://localhost:3000' + p + '/api/health', r => { let b=''; r.on('data', c => b += c); r.on('end', () => { console.log(b); process.exit(r.statusCode === 200 ? 0 : 1) }) }).on('error', () => process.exit(1))"

backup: ## Create a database + storage backup
	scripts/backup.sh

verify-backup: ## Verify BACKUP=backups/cangzhi-... checksum and metadata
	@test -n "$(BACKUP)" || (echo "用法：make verify-backup BACKUP=backups/cangzhi-..." >&2; exit 2)
	scripts/verify-backup.sh "$(BACKUP)"

migrate: ## Run database migrations locally (uses .venv)
	@echo "Running migrations..."
	.venv/bin/python -m alembic upgrade head

migrate-up: ## Alias for migrate
	$(MAKE) migrate

migrate-create: ## Create new migration (usage: make migrate-create MSG="description")
	.venv/bin/python -m alembic revision --autogenerate -m "$(MSG)"

install-python: ## Install Python dependencies in virtual environment
	python3 -m venv .venv
	@echo "Installing API dependencies..."
	.venv/bin/pip install -r apps/api/requirements.txt
	@echo "Installing Worker dependencies..."
	.venv/bin/pip install -r apps/worker/requirements.txt
	@echo "Installing development dependencies..."
	.venv/bin/pip install alembic pytest pylint mypy

install-node: ## Install Node.js dependencies for web
	cd apps/web && npm ci

install: install-python install-node ## Install all dependencies locally

test-api: ## Run API tests locally (.venv required)
	.venv/bin/python -m pytest apps/api/tests/ -v

test-worker: ## Run Worker tests locally (.venv required)
	.venv/bin/python -m pytest apps/worker/tests/ -v

test-web: ## Run TypeScript checks and lint for web
	cd apps/web && npm run lint && npm run typecheck

test: test-api test-worker test-web ## Run all tests

lint-api: ## Lint API Python code
	.venv/bin/pylint apps/api/**/*.py

lint-worker: ## Lint Worker Python code
	.venv/bin/pylint apps/worker/**/*.py

lint: lint-api lint-worker ## Lint all Python code

typecheck-api: ## Type check API code
	.venv/bin/mypy apps/api

typecheck-worker: ## Type check Worker code
	.venv/bin/mypy apps/worker

typecheck: typecheck-api typecheck-worker ## Type check all Python code

clean: ## Clean up temporary files
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.pyd" -delete
	rm -rf .pytest_cache
	rm -rf .mypy_cache
