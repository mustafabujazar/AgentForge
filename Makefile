# =============================================================================
# AgentForge — developer & operations tasks
#
# Windows-first but OS-aware. GNU Make is not bundled with Windows; install it
# with `winget install ezwinports.make` (or `choco install make`). On Linux /
# macOS `make` is available with bash.
#
#   make help
#
# Without GNU Make (plain PowerShell), use the raw commands documented in
# docs/deployment.md — every target below is a thin one-line wrapper.
#
# Interpreter paths are quoted so the same recipes work whether GNU Make uses
# cmd.exe or a POSIX sh (Git Bash/MSYS).
#
# Variables:
#   PYTHON=py -3.12    override the interpreter used to bootstrap the venv
# =============================================================================

ifneq ($(OS),Windows_NT)
  VENV_BIN := venv/bin
else
  VENV_BIN := venv/Scripts
endif

PYTHON ?= python
PY     := $(VENV_BIN)/python
PIP    := "$(PY)" -m pip

.DEFAULT_GOAL := help
.PHONY: help install install-backend install-frontend dev dev-frontend build \
        test lint demo db-init docker-build docker-up docker-down docker-logs \
        docker-ps compose-config health clean check

help:
	@echo "AgentForge targets:"
	@echo "  install          Create venv, install Python + frontend dependencies"
	@echo "  install-backend  Create venv and install requirements.txt"
	@echo "  install-frontend Install frontend npm dependencies"
	@echo "  dev              Run FastAPI with autoreload on 127.0.0.1:8000"
	@echo "  dev-frontend     Run the Vite dev server (proxies /api to :8000)"
	@echo "  build            Build the production SPA into frontend/dist"
	@echo "  test             Run pytest (set LLM_PROVIDER=mock for offline runs)"
	@echo "  lint             Byte-compile app/ + frontend lint if configured"
	@echo "  demo             Run scripts/demo_run.py end-to-end demo"
	@echo "  db-init          Create tables from SQLAlchemy models"
	@echo "  docker-build     Build the production image (agentforge-backend:local)"
	@echo "  docker-up        docker compose up -d --build (Postgres + backend)"
	@echo "  docker-down      docker compose down (keeps named volumes)"
	@echo "  docker-logs      Follow backend container logs"
	@echo "  docker-ps        Show compose service status"
	@echo "  compose-config   Validate the compose file (no daemon needed)"
	@echo "  health           Curl-equivalent check of GET /api/health"
	@echo "  clean            Remove caches and built frontend assets"
	@echo "  check            lint + test + compose-config (pre-push gate)"

install: install-backend install-frontend

install-backend:
	$(PYTHON) -m venv venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

install-frontend:
	npm --prefix frontend install

dev:
	"$(PY)" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

dev-frontend:
	npm --prefix frontend run dev

build:
	npm --prefix frontend run build

test:
	"$(PY)" -m pytest -q

lint:
	"$(PY)" -m compileall -q app
	npm --prefix frontend run lint --if-present

demo:
	"$(PY)" scripts/demo_run.py

db-init:
	"$(PY)" -c "from app.db.base import init_db; init_db(); print('OK: database initialised')"

docker-build:
	docker build -t agentforge-backend:local .

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f backend

docker-ps:
	docker compose ps

compose-config:
	docker compose config --quiet
	@echo "OK: docker-compose.yml is valid"

health:
	"$(PY)" -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5).read().decode())"

clean:
	"$(PY)" -c "import shutil; [shutil.rmtree(p, ignore_errors=True) for p in ('.pytest_cache', 'frontend/dist')]"

check: lint test compose-config
