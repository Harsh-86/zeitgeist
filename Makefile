.PHONY: test lint up down smoke

test:
	uv run pytest tests/unit -q

lint:
	uv run ruff check src tests

up:
	docker compose -f docker/docker-compose.yml up -d --build

down:
	docker compose -f docker/docker-compose.yml down

smoke:
	uv run python scripts/smoke_test.py
