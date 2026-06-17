BUILD_DIR=build

PYTHON ?= /usr/bin/env python3

all: deps lint man package install

# Install dependent pip packages, needed to lint or build
deps:
	$(PYTHON) -m pip install .[dev]

# Format using ruff
RUFF_CMD=$(PYTHON) -m ruff
format:
	$(RUFF_CMD) format .
	$(RUFF_CMD) check --fix .

# Check formatting using ruff
check_format:
	$(RUFF_CMD) format --check --diff .
	$(RUFF_CMD) check --diff .

MYPY_COMMAND=$(PYTHON) -m mypy --show-error-codes
check_types:
	$(MYPY_COMMAND) revup

pylint:
	$(PYTHON) -m pylint revup

# Lint check for formatting and type hints
# This needs pass before any merge.
lint: check_types check_format pylint

# Clean all artifacts
clean:
	rm -rf $(BUILD_DIR)
	rm -rf .mypy_cache

REVUP_VERSION:=$(shell $(PYTHON) revup/version.py)
REVUP_DATE ?= Apr 21, 2021

REVUP_VERSION_HASH?=${shell git rev-parse --short v$(REVUP_VERSION) || echo main}

package: man
	REVUP_VERSION_HASH=$(REVUP_VERSION_HASH) $(PYTHON) -m build --outdir $(BUILD_DIR)

install:
	$(PYTHON) -m pip install build/revup-$(REVUP_VERSION)-py3-none-any.whl --force-reinstall

upload_check:
	$(PYTHON) -m twine check build/revup-$(REVUP_VERSION).tar.gz

upload_test:
	$(PYTHON) -m twine upload --repository testpypi build/revup-$(REVUP_VERSION).tar.gz

upload:
	$(PYTHON) -m twine upload build/revup-$(REVUP_VERSION).tar.gz

man:
	mkdir -p revup/man1
	@for src in docs/*.md ; do \
		name=$$(basename $${src} .md) ; \
		scripts/build_manpage.sh "$${name}" "$(REVUP_VERSION)" "$(REVUP_DATE)" \
			"$${src}" "revup/man1/$${name}.1.gz" || exit 1 ; \
	done

test:
	$(PYTHON) -m pytest --cov=revup --cov-report=term-missing tests/

.PHONY: all deps man install package format check_format check_types pylint lint clean
