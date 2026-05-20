PYTHON ?= python3
VENV ?= .venv
POLL ?= 5

VENV_PY := $(VENV)/bin/python
MORPHEUS := $(VENV)/bin/morpheus

.PHONY: help bootstrap install start up dashboard daemon daemon-start daemon-stop daemon-status status watch doctor logs graph-status test clean

help:
	@printf "Morpheus dev commands\n\n"
	@printf "  make start         Install/reload daemon, then open the Morpheus cockpit\n"
	@printf "  make dashboard     Open the Morpheus cockpit without touching daemon state\n"
	@printf "  make daemon        Install/reload the launchd daemon from this repo venv\n"
	@printf "  make status        Show daemon health\n"
	@printf "  make watch         Run foreground watcher instead of launchd\n"
	@printf "  make graph-status  Show v0.7 mission graph table counts\n"
	@printf "  make doctor        Diagnose iTerm2 Python API setup\n"
	@printf "  make logs          Tail ~/.morpheus/daemon.log\n"
	@printf "  make test          Run lightweight local checks\n"
	@printf "\nOverride polling with POLL=2, e.g. make start POLL=2\n"

$(VENV_PY):
	$(PYTHON) -m venv $(VENV)

bootstrap: $(VENV_PY)
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -e .

install: bootstrap

daemon daemon-start: bootstrap
	$(MORPHEUS) install-daemon --poll $(POLL)

start up: daemon
	$(MORPHEUS)

dashboard: bootstrap
	$(MORPHEUS)

watch: bootstrap
	$(MORPHEUS) watch --poll $(POLL)

daemon-stop: bootstrap
	$(MORPHEUS) uninstall-daemon

daemon-status status: bootstrap
	$(MORPHEUS) daemon-status

doctor: bootstrap
	$(MORPHEUS) doctor

graph-status: bootstrap
	$(MORPHEUS) graph status

logs:
	tail -f $(HOME)/.morpheus/daemon.log

test: bootstrap
	$(VENV_PY) -m compileall morpheus
	$(VENV_PY) -m unittest discover
	git diff --check

clean:
	rm -rf $(VENV) build dist *.egg-info
