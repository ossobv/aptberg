.PHONY: all cov fmt fmtfix test setup venv

all: setup

test: setup
	. .venv/bin/activate && pytest

cov: setup
	. .venv/bin/activate && pytest --cov --cov-report=term \
	  --cov-report=html

fmt: setup
	# E203 warns about list[a : b], which ruff creates.
	. .venv/bin/activate && flake8 --extend-ignore=E203 src
	. .venv/bin/activate && flake8 --extend-ignore=E203 \
            --max-line-length=120 tests
	! LC_ALL=C grep -rnP --exclude='*.pyc' --exclude='*.swp' '[^\x00-\x7F]' \
	  src tests
	ruff check src tests
	ruff format --check src tests

fmtfix: setup
	ruff check --fix src tests
	ruff format src tests

setup: venv

venv: .venv
	@echo "To activate the venv manually:"
	@echo ". .venv/bin/activate"
	@echo

.venv:
	python3 -m venv .venv --prompt aptberg-dev
	. .venv/bin/activate && \
	  pip install -e '.[dev]'
