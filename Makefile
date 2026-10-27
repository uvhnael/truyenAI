.PHONY: install db-up migrate dev-api dev-worker dev-web test lint e2e build up up-gpu worker-gpu-down down

tts-model:
	cd backend && uv run python scripts/prepare_kokoro.py

install:
	cd backend && uv sync --frozen
	cd frontend && npm ci

db-up:
	docker compose up -d postgres redis

migrate:
	cd backend && uv run python -m app.migrate

dev-api:
	cd backend && uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 18080

dev-worker:
	cd backend && uv run arq app.worker.WorkerSettings

dev-web:
	cd frontend && npm run dev -- --port 13000

test:
	cd backend && uv run pytest -q

lint:
	cd backend && uv run ruff check app
	cd frontend && npm run lint

e2e:
	bash scripts/e2e.sh

build:
	docker compose build

up:
	docker compose up -d --build
	@printf 'Web: http://localhost:13000 | API: http://localhost:18080/docs\n'

up-gpu:
	docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
	@printf 'Web: http://localhost:13000 | API: http://localhost:18080/docs | worker TTS_DEVICE=cuda\n'

worker-gpu-down:
	docker compose -f docker-compose.yml up -d --build --force-recreate worker

down:
	docker compose down
