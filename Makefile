.DEFAULT_GOAL := help
.PHONY: help install update uninstall run test test-socket test-tmux test-all lint fmt docs docs-serve screenshots screenshot-live icons cache-report shell-report harness-src obsidian-plugin worktree worktree-merge worktree-remove worktree-clean worktrees clean-merged brew-stats brew-install brew-update brew-uninstall sync release

# Harness lives in the pydantic-ai repo (src/pydantic_ai_harness, docs/harness,
# tests/harness) and ships with each Pydantic AI release.
HARNESS_DIR := tmp/pydantic-ai
HARNESS_URL := https://github.com/pydantic/pydantic-ai.git

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}'

# Optional extras to install; `make install EXTRAS=` leaves out claude: models.
EXTRAS ?= claude

install: ## Install `pcode` on PATH (editable: source edits are live)
	uv tool install --python 3.15 --editable '.$(if $(EXTRAS),[$(EXTRAS)])' --reinstall --compile-bytecode

update: install ## Rebuild the tool env after dependency changes

uninstall: ## Remove the `pcode` command
	uv tool uninstall pcode

run: ## Run from source without installing (make run ARGS="--theme-preview")
	uv run pcode $(ARGS)

test: ## Run the fast suite in parallel (real-tmux regressions skipped)
	uv run pytest -n auto

test-socket: ## Run the fast suite with each session in a host over a socket (tests/socket_transport.py)
	uv run pytest -n auto --transport socket

test-tmux: ## Run only the real-tmux regressions, in parallel
	uv run pytest --tmux -m tmux -n auto

test-all: test test-socket test-tmux ## Run everything: the fast suite in-process and hosted, then the tmux regressions

lint: ## Check formatting and lint rules
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Apply formatting and autofixes
	uv run ruff check --fix .
	uv run ruff format .

docs: ## Build the docs site into site/ (fails on broken links)
	uv run --group docs zensical build --strict

docs-serve: ## Preview the docs site with live reload at http://localhost:8000
	uv run --group docs zensical serve

screenshots: ## Regenerate docs screenshots from scripted scenes (SCENES="tree jobs" for some, ARGS=--iterm for your iTerm2 colors)
	uv run python scripts/screenshots/run.py $(ARGS) $(SCENES)

screenshot-live: ## Play one scene in this terminal to screenshot it yourself (SCENE=review)
	uv run python scripts/screenshots/run.py --live $(or $(SCENE),review)

icons: ## Browse terminal icon candidates (ARGS="--category thinking --single-cell")
	uv run python scripts/icons.py $(ARGS)

cache-report: ## Report prompt-cache behavior from saved sessions (SESSION=latest|all|<id>)
	uv run python scripts/cache_report.py $(or $(SESSION),latest) $(ARGS)

shell-report: ## Report how the model used the shell tool in saved sessions (SESSION=latest|all|<id>)
	uv run python scripts/shell_report.py $(or $(SESSION),latest) $(ARGS)

harness-src: ## Check out upstream Harness source at the locked Pydantic AI release under tmp/
	@version=$$(awk '/^name = "pydantic-ai-slim"$$/ {getline; gsub(/version = |"/, ""); print; exit}' uv.lock); \
	if [ -z "$$version" ]; then echo 'no locked pydantic-ai-slim in uv.lock' >&2; exit 1; fi; \
	tag="v$$version"; \
	if [ ! -d $(HARNESS_DIR)/.git ]; then git clone --quiet --filter=blob:none --no-checkout $(HARNESS_URL) $(HARNESS_DIR); fi; \
	git -C $(HARNESS_DIR) rev-parse --verify --quiet "$$tag^{commit}" >/dev/null || git -C $(HARNESS_DIR) fetch --quiet origin tag "$$tag"; \
	git -C $(HARNESS_DIR) checkout --quiet --detach "$$tag"; \
	echo "$(HARNESS_DIR) @ $$tag"

obsidian-plugin: ## Build, install and enable the Obsidian bridge plugin in a vault (make obsidian-plugin VAULT=~/vaults/personal)
	@test -n "$(VAULT)" || { echo 'usage: make obsidian-plugin VAULT=path/to/vault' >&2; exit 1; }
	cd integrations/obsidian && npm ci --no-audit --no-fund && npm test && npm run build
	@vault="$(VAULT)"; case "$$vault" in "~"*) vault="$$HOME$${vault#\~}";; esac; \
		test -d "$$vault/.obsidian" || { echo "$$vault has no .obsidian folder: open it as a vault in Obsidian first" >&2; exit 1; }; \
		dest="$$(cd "$$vault" && pwd)/.obsidian/plugins/pcode-bridge" && mkdir -p "$$dest" \
		&& cp integrations/obsidian/manifest.json integrations/obsidian/main.js integrations/obsidian/styles.css "$$dest/" \
		&& echo "Installed into $$dest." \
		&& node integrations/obsidian/scripts/enable.mjs "$$vault" \
		&& echo "If Obsidian is open, reload it (Cmd+P > Reload app without saving). Community plugins must be on (Settings > Community plugins)."

worktree: ## Create an isolated worktree under .worktrees/ (make worktree NAME=fix-foo [BASE=ref]); `pcode --worktree` does this per session
	@uv run python -m pcode.worktree new $(NAME) $(if $(BASE),--base $(BASE))

worktree-merge: ## Merge a worktree's branch back into the mainline (make worktree-merge NAME=fix-foo)
	@uv run python -m pcode.worktree merge $(NAME)

worktree-remove: ## Delete a worktree, keeping its branch (make worktree-remove NAME=fix-foo)
	@uv run python -m pcode.worktree remove $(NAME)

worktrees: ## List worktrees
	@uv run python -m pcode.worktree list

worktree-clean: ## Delete every worktree with nothing uncommitted or unmerged, and its branch
	@uv run python -m pcode.worktree clean

sync: ## Set up hooks and identity, then align the mainline with origin (safe after a history rewrite; use instead of git pull there)
	@sh scripts/sync.sh

clean-merged: worktree-clean ## Also delete merged branches left behind, local and on origin (ARGS=--dry-run)
	@sh scripts/clean-merged-branches.sh $(ARGS)

release: ## Tag origin's master as the next release and push it (BUMP=minor|major, VERSION=x.y.z); CI publishes
	@sh scripts/release.sh

brew-stats: ## Homebrew downloads per release and in total (installs plus upgrades, not unique users)
	@gh api --paginate repos/cruxwell/pcode/releases \
		--jq '.[] | .tag_name as $$tag | .assets[] | select(.name | endswith("-src.tar.gz")) | "\($$tag)\t\(.download_count)"' \
		| awk -F'\t' '{ print; total += $$2 } END { print "total\t" total + 0 }'

brew-install: ## Alternative: install the latest release via Homebrew (ARGS=--HEAD for master)
	brew tap cruxwell/pcode https://github.com/cruxwell/pcode.git
	brew install $(ARGS) cruxwell/pcode/pcode

brew-update: ## Upgrade the Homebrew build (ARGS=--fetch-HEAD for a HEAD install)
	brew update
	brew upgrade $(ARGS) cruxwell/pcode/pcode

brew-uninstall: ## Remove the Homebrew build and tap
	brew uninstall pcode
	brew untap cruxwell/pcode
