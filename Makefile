# TradeClaw — local dev & packaging
UV ?= uv
NPM ?= npm
FRONTEND := frontend

.PHONY: help install deps backend frontend build package test preview clean

.DEFAULT_GOAL := help

help:
	@echo "TradeClaw"
	@echo ""
	@echo "  make install    Python deps (uv sync) + frontend deps (npm ci)"
	@echo "  make backend    API server (uvicorn, default :8000)"
	@echo "  make frontend   Vite dev server"
	@echo "  make build      frontend dist/ + Python wheel & sdist"
	@echo "  make test       Python unit tests"
	@echo "  make preview    vite preview (after build)"
	@echo "  make clean      remove build outputs (frontend/dist, dist/)"
	@echo ""

install deps:
	$(UV) sync
	$(NPM) --prefix $(FRONTEND) ci

backend:
	$(UV) run python -m tradeclaw

frontend:
	$(NPM) --prefix $(FRONTEND) run dev

build:
	$(NPM) --prefix $(FRONTEND) run build
	$(UV) build

package: build

test:
	$(UV) run python -m unittest discover -s tests -v

preview:
	$(NPM) --prefix $(FRONTEND) run preview

clean:
	rm -rf $(FRONTEND)/dist dist
