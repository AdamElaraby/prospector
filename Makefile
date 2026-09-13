VENV := .venv
# On Apple Silicon, force native execution even from a Rosetta (x86_64) shell —
# otherwise pip installs Intel wheels into a venv full of arm64 ones.
NATIVE := $(shell [ "$$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ] && echo arch -arm64)
PY := $(NATIVE) $(VENV)/bin/python
GOSOM_REPO := https://github.com/gosom/google-maps-scraper
GOSOM_BIN := bin/google-maps-scraper

.PHONY: help setup install-gosom clean-gosom check ui app

help:
	@echo "make setup          create the venv, install deps and the Playwright browser"
	@echo "make install-gosom  build gosom/google-maps-scraper into ./bin (needs Go)"
	@echo "make ui             open the local web UI in your browser"
	@echo "make check          verify the toolchain and config"

setup:
	python3 -m venv $(VENV)
	$(NATIVE) $(VENV)/bin/pip install -q --upgrade pip
	$(NATIVE) $(VENV)/bin/pip install -q -r requirements.txt
	$(NATIVE) $(VENV)/bin/playwright install chromium
	@[ -f config.local.yaml ] || cp config.yaml config.local.yaml
	@[ -f .env ] || cp .env.example .env
	@echo "done. Put your name in config.local.yaml and your OpenAI key in .env"

# Builds the MIT-licensed Go scraper from source into ./bin. Nothing is
# downloaded prebuilt: the source is cloned and compiled locally.
install-gosom:
	@command -v go >/dev/null 2>&1 || { echo "Go is not installed. brew install go"; exit 1; }
	@mkdir -p bin
	@rm -rf .gosom-build
	git clone --depth 1 $(GOSOM_REPO) .gosom-build
	cd .gosom-build && go mod download && go build -o ../$(GOSOM_BIN) .
	@rm -rf .gosom-build
	@echo "built $(GOSOM_BIN)"
	@./$(GOSOM_BIN) -h 2>&1 | head -3 || true

clean-gosom:
	rm -rf .gosom-build $(GOSOM_BIN)

check:
	$(PY) -m prospector.cli init

ui:
	$(PY) -m prospector.web $(if $(PORT),--port $(PORT))

app:
	bash packaging/build_app.sh
