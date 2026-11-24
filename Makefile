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
	poetry run pytest --cov=photo_keeper --cov-branch --cov-report=term-missing --cov-fail-under=95

build:
	poetry build
