PYTHON ?= python3
VENV ?= .venv
POLL ?= 5
LOOP_INTERVAL ?= 60
LOOP_LIMIT ?= 5
LOCAL_BIN ?= $(HOME)/.local/bin

# Omnipresence / G2 bridge defaults. Override per-invocation, e.g.
#   make start-omni G2_PUBLIC_URL=https://other-host.ts.net
G2_PUBLIC_URL ?= https://fabians-macbook-pro.tail3387a8.ts.net
G2_PORT ?= 3456
G2_TOKEN_FILE ?= $(HOME)/.morpheus/g2-token
G2_DIR := plugins/g2-bridge

VENV_PY := $(VENV)/bin/python
MORPHEUS := $(VENV)/bin/morpheus
LOCAL_MORPHEUS := $(LOCAL_BIN)/morpheus

.PHONY: help bootstrap install install-cli uninstall-cli start up dashboard desktop daemon daemon-start daemon-stop daemon-status status loop-runner loop-runner-stop loop-runner-status watch doctor logs loop-logs graph-status test clean start-omni omni-off omni-status g2-bridge g2-token

help:
	@printf "Morpheus dev commands\n\n"
	@printf "  make start         Install/reload daemon, then open the Morpheus cockpit\n"
	@printf "  make dashboard     Open the Morpheus cockpit without touching daemon state\n"
	@printf "  make desktop       Launch the desktop chat-agent cockpit (browser/Electron)\n"
	@printf "  make install-cli   Put a morpheus shim on your user PATH (default: ~/.local/bin)\n"
	@printf "  make daemon        Install/reload the launchd daemon from this repo venv\n"
	@printf "  make loop-runner   Install/reload launchd loop runner for due prompt loops\n"
	@printf "  make status        Show watcher + loop-runner health\n"
	@printf "  make watch         Run foreground watcher instead of launchd\n"
	@printf "  make graph-status  Show v0.7 mission graph table counts\n"
	@printf "  make doctor        Diagnose iTerm2 Python API setup\n"
	@printf "  make logs          Tail ~/.morpheus/daemon.log\n"
	@printf "  make loop-logs     Tail ~/.morpheus/loop-runner.log\n"
	@printf "  make test          Run lightweight local checks\n"
	@printf "  make start-omni    Omnipresence: omni on + init, loop runner, tailscale serve, G2 bridge\n"
	@printf "  make g2-bridge     Start only the G2 bridge (foreground) with the persisted token\n"
	@printf "  make omni-status   Show omnipresence settings, template loops, and recent pushes\n"
	@printf "  make omni-off      Disable omnipresence pushes\n"
	@printf "\nOverride polling with POLL=2, e.g. make start POLL=2\n"
	@printf "Override the glasses URL with G2_PUBLIC_URL=https://your-mac.your-tailnet.ts.net\n"

$(VENV_PY):
	$(PYTHON) -m venv $(VENV)

bootstrap: $(VENV_PY)
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -e .

install: bootstrap

install-cli: bootstrap
	@mkdir -p "$(LOCAL_BIN)"
	@if [ -e "$(LOCAL_MORPHEUS)" ] && [ ! -L "$(LOCAL_MORPHEUS)" ]; then \
		printf "Refusing to overwrite non-symlink: %s\n" "$(LOCAL_MORPHEUS)"; \
		exit 1; \
	fi
	@ln -sfn "$(abspath $(MORPHEUS))" "$(LOCAL_MORPHEUS)"
	@printf "Installed morpheus shim: %s -> %s\n" "$(LOCAL_MORPHEUS)" "$(abspath $(MORPHEUS))"
	@"$(LOCAL_MORPHEUS)" version
	@case ":$$PATH:" in \
		*:"$(LOCAL_BIN)":*) \
			printf "Ready: run 'morpheus' from any directory to use that directory as the cockpit cwd.\n"; \
			;; \
		*) \
			printf "\nAdd this to your shell profile so new terminals can find it:\n"; \
			printf '  export PATH="%s:$$PATH"\n' "$(LOCAL_BIN)"; \
			;; \
	esac

uninstall-cli:
	@if [ -L "$(LOCAL_MORPHEUS)" ] && [ "$$(readlink "$(LOCAL_MORPHEUS)")" = "$(abspath $(MORPHEUS))" ]; then \
		rm "$(LOCAL_MORPHEUS)"; \
		printf "Removed morpheus shim: %s\n" "$(LOCAL_MORPHEUS)"; \
	else \
		printf "No Morpheus-owned shim found at %s\n" "$(LOCAL_MORPHEUS)"; \
	fi

daemon daemon-start: bootstrap
	$(MORPHEUS) install-daemon --poll $(POLL)

loop-runner: bootstrap
	$(MORPHEUS) install-loop-runner --interval $(LOOP_INTERVAL) --limit $(LOOP_LIMIT)

start up: daemon
	$(MORPHEUS)

dashboard: bootstrap
	$(MORPHEUS)

desktop: bootstrap
	$(MORPHEUS) desktop

watch: bootstrap
	$(MORPHEUS) watch --poll $(POLL)

daemon-stop: bootstrap
	$(MORPHEUS) uninstall-daemon

loop-runner-stop: bootstrap
	$(MORPHEUS) uninstall-loop-runner

daemon-status status: bootstrap
	$(MORPHEUS) daemon-status
	$(MORPHEUS) loop-runner-status

loop-runner-status: bootstrap
	$(MORPHEUS) loop-runner-status

doctor: bootstrap
	$(MORPHEUS) doctor

graph-status: bootstrap
	$(MORPHEUS) graph status

logs:
	tail -f $(HOME)/.morpheus/daemon.log

loop-logs:
	tail -f $(HOME)/.morpheus/loop-runner.log

test: bootstrap
	$(VENV_PY) -m compileall morpheus
	$(VENV_PY) -m unittest discover
	git diff --check

clean:
	rm -rf $(VENV) build dist *.egg-info

g2-token:
	@mkdir -p "$(HOME)/.morpheus"
	@if [ ! -s "$(G2_TOKEN_FILE)" ]; then \
		umask 177; openssl rand -hex 24 > "$(G2_TOKEN_FILE)"; \
		printf "Generated G2 bridge token: %s\n" "$(G2_TOKEN_FILE)"; \
	fi

g2-bridge: bootstrap g2-token
	@if [ ! -d "$(G2_DIR)/node_modules" ]; then npm --prefix "$(G2_DIR)" install; fi
	@printf "G2 bridge: public URL %s (token file %s)\n" "$(G2_PUBLIC_URL)" "$(G2_TOKEN_FILE)"
	MORPHEUS_G2_TOKEN="$$(cat "$(G2_TOKEN_FILE)")" \
	MORPHEUS_G2_PUBLIC_URL="$(G2_PUBLIC_URL)" \
	MORPHEUS_G2_ALLOWED_ORIGINS="$(G2_PUBLIC_URL)" \
	MORPHEUS_BIN="$(abspath $(MORPHEUS))" \
	PORT=$(G2_PORT) \
	npm --prefix "$(G2_DIR)" start

start-omni: bootstrap loop-runner
	$(MORPHEUS) omni on
	$(MORPHEUS) omni init
	@if command -v tailscale >/dev/null 2>&1; then \
		tailscale serve --bg $(G2_PORT) || printf "warning: 'tailscale serve --bg %s' failed — glasses need it to reach the bridge\n" "$(G2_PORT)"; \
	else \
		printf "warning: tailscale not found — run 'tailscale serve --bg %s' yourself so the glasses can reach the bridge\n" "$(G2_PORT)"; \
	fi
	$(MAKE) g2-bridge

omni-off: bootstrap
	$(MORPHEUS) omni off

omni-status: bootstrap
	$(MORPHEUS) omni status
