.PHONY: install check lint format test build

install:
	poetry install

check: lint test
	poetry check --lock

lint:
	poetry run ruff check .
	poetry run ruff format --check .

format:
	poetry run ruff format .

test:
	poetry run pytest

build:
	poetry build
