.PHONY: all build cov fmt fmtfix test setup venv

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

build:
	# Explicitly use system env (apt-get install python3-build). Check
	# that we're not inside the dev venv. build will create a new one.
	python -c 'import os,sys;assert not \
	  sys.exec_prefix.startswith(os.path.abspath(os.curdir)), \
	  sys.exec_prefix'
	# Reproducible whl file using SOURCE_DATE_EPOCH.
	SOURCE_DATE_EPOCH=$$(git log -1 --pretty=%ct) python3 -m build
	# Reproducible tar.gz by fixing user+timestamp.
	dt=$$(git log -1 --pretty=%ct) && \
	  tgz=$$(ls -t dist/*.tar.gz | head -n1) && \
	  tempd=$$(mktemp -d) && \
	  tar -C "$$tempd" -xzf "$$tgz" && \
	  tar -C "$$tempd" --sort=name --mtime=@$$dt \
	    --owner=0 --group=0 --numeric-owner \
	    -cf - "$$(basename "$$tgz" .tar.gz)" | \
	    gzip -n >"$$tgz" && rm -rf "$$tempd"
	@echo
	twine --version 2>/dev/null | awk '\
	  /^twine version/{ver=$$3} \
	  END{if(ver<6.1){print "twine " ver " too old";exit 1}}'
	@echo
	@echo do: twine upload --repository testpypi dist/aptberg-1.1...

setup: venv

venv: .venv
	@echo "To activate the venv manually:"
	@echo ". .venv/bin/activate"
	@echo

.venv:
	python3 -m venv .venv --prompt aptberg-dev
	. .venv/bin/activate && \
	  pip install -e '.[dev]'
