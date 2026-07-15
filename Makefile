# DoYouTrade — local dev & packaging
UV ?= uv
NPM ?= npm
FRONTEND := frontend
REPO_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
METRICS_PROJECT_ROOT ?= $(REPO_ROOT)
METRICS_OPTOUT_SCRIPT := scripts/metrics_project_optout.py

SKILLS_SRC ?= .doyoutrade/skills
SKILLS_TARGETS ?= .claude/skills .codex/skills .cursor/skills

.PHONY: help install deps migrate backend frontend build package test test-e2e preview clean sync-skills unsync-skills metrics-project-private metrics-project-private-apply metrics-project-private-clean metrics-project-private-status metrics-project-private-verify

.DEFAULT_GOAL := help

help:
	@echo "DoYouTrade"
	@echo ""
	@echo "  make install    Python deps (uv sync) + frontend deps (npm ci)"
	@echo "  make migrate    alembic upgrade head (uses config database.url, same as runtime)"
	@echo "  make backend    migrate then API server (uvicorn, default :8000)"
	@echo "  make frontend   Vite dev server"
	@echo "  make build      frontend dist/ + Python wheel & sdist"
	@echo "  make test       Python unit tests"
	@echo "  make test-e2e   End-to-end runtime tests (reads config.yaml + E2E overrides)"
	@echo "  make preview    vite preview (after build)"
	@echo "  make clean      remove build outputs (frontend/dist, dist/)"
	@echo "  make sync-skills    mirror $(SKILLS_SRC)/*/ into $(SKILLS_TARGETS) as relative symlinks"
	@echo "  make unsync-skills  remove every symlink under $(SKILLS_TARGETS) that points into $(SKILLS_SRC)"
	@echo "  make metrics-project-private        block dashboard metrics for this repo, clean local history, verify"
	@echo "  make metrics-project-private-apply  inject the current-project opt-out into local metrics hooks"
	@echo "  make metrics-project-private-clean  delete local Claude/Codex transcripts for this repo"
	@echo "  make metrics-project-private-status count local transcripts for this repo"
	@echo "  make metrics-project-private-verify verify the installed hooks short-circuit for this repo"
	@echo ""

install deps:
	$(UV) sync --extra doc-processing
	$(NPM) --prefix $(FRONTEND) ci

migrate:
	$(UV) run python -c "import asyncio; from doyoutrade.config import get_config; from doyoutrade.persistence.runtime_state import run_migrations; asyncio.run(run_migrations(get_config().database.url))"

backend: migrate
	$(UV) run python -m doyoutrade

frontend:
	$(NPM) --prefix $(FRONTEND) run dev

build:
	$(NPM) --prefix $(FRONTEND) run build
	$(UV) build

package: build

test:
	$(UV) run python -m unittest discover -s tests -v

test-e2e:
	DOYOUTRADE_E2E=1 DOYOUTRADE_E2E_PROFILE=$${DOYOUTRADE_E2E_PROFILE:-isolated} $(UV) run python -m unittest discover -s tests/e2e -v

preview:
	$(NPM) --prefix $(FRONTEND) run preview

clean:
	rm -rf $(FRONTEND)/dist dist

sync-skills:
	@set -e; \
	src="$(SKILLS_SRC)"; \
	if [ ! -d "$$src" ]; then echo "skill source not found: $$src" >&2; exit 1; fi; \
	for target in $(SKILLS_TARGETS); do \
		mkdir -p "$$target"; \
		echo "==> $$src -> $$target"; \
		slashes=$$(printf '%s' "$$target" | tr -cd '/' | wc -c | tr -d ' '); \
		depth=$$((slashes + 1)); \
		prefix=""; i=0; while [ $$i -lt $$depth ]; do prefix="../$$prefix"; i=$$((i+1)); done; \
		for entry in "$$target"/*; do \
			[ -L "$$entry" ] || continue; \
			link=$$(readlink "$$entry"); \
			case "$$link" in \
				*"$$src"/*) \
					name=$$(basename "$$entry"); \
					if [ ! -d "$$src/$$name" ]; then echo "  prune $$entry (source gone)"; rm "$$entry"; fi ;; \
			esac; \
		done; \
		for s in "$$src"/*/; do \
			[ -d "$$s" ] || continue; \
			name=$$(basename "$$s"); \
			dest="$$target/$$name"; \
			want="$$prefix$$src/$$name"; \
			if [ -L "$$dest" ]; then \
				current=$$(readlink "$$dest"); \
				if [ "$$current" = "$$want" ]; then continue; fi; \
				rm "$$dest"; \
			elif [ -e "$$dest" ]; then \
				echo "  skip $$dest (not a symlink)"; continue; \
			fi; \
			ln -s "$$want" "$$dest"; \
			echo "  link $$dest -> $$want"; \
		done; \
	done

unsync-skills:
	@set -e; \
	src="$(SKILLS_SRC)"; \
	for target in $(SKILLS_TARGETS); do \
		[ -d "$$target" ] || continue; \
		echo "==> remove $$src links from $$target"; \
		for entry in "$$target"/*; do \
			[ -L "$$entry" ] || continue; \
			link=$$(readlink "$$entry"); \
			case "$$link" in \
				*"$$src"/*) echo "  remove $$entry"; rm "$$entry" ;; \
			esac; \
		done; \
	done

metrics-project-private:
	$(UV) run python $(METRICS_OPTOUT_SCRIPT) --project-root "$(METRICS_PROJECT_ROOT)" protect

metrics-project-private-apply:
	$(UV) run python $(METRICS_OPTOUT_SCRIPT) --project-root "$(METRICS_PROJECT_ROOT)" apply

metrics-project-private-clean:
	$(UV) run python $(METRICS_OPTOUT_SCRIPT) --project-root "$(METRICS_PROJECT_ROOT)" cleanup

metrics-project-private-status:
	$(UV) run python $(METRICS_OPTOUT_SCRIPT) --project-root "$(METRICS_PROJECT_ROOT)" status

metrics-project-private-verify:
	$(UV) run python $(METRICS_OPTOUT_SCRIPT) --project-root "$(METRICS_PROJECT_ROOT)" verify
