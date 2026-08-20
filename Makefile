.PHONY: test lint integration up down smoke

test:  # unit tests only (fast, no Docker)
	uv run pytest tests/unit -q

lint:
	uv run ruff check src tests

integration:
	uv run pytest tests/integration -q -m integration

up:
	docker compose -f docker/docker-compose.yml up -d --build

down:
	docker compose -f docker/docker-compose.yml down

smoke:
	KAFKA_BOOTSTRAP=localhost:29092 uv run python scripts/smoke_test.py
