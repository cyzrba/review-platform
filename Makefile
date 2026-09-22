.PHONY: help setup backend frontend test smoke dev clean-reset

PY := backend/.venv/bin/python

help:
	@echo "make setup    安装后端依赖（需要已装 uv）"
	@echo "make backend  启动后端 http://127.0.0.1:8010"
	@echo "make frontend 启动前端 http://localhost:5173"
	@echo "make test     跑后端测试"
	@echo "make smoke    用真实存储跑一遍端到端冒烟"
	@echo "make minio    用 docker compose 起 MinIO"
	@echo "make clean-reset 清空本地 SQLite 数据库与本地对象目录"

setup:
	cd backend && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r pyproject.toml pytest httpx

backend:
	cd backend && PYTHONPATH=. ./.venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010

frontend:
	cd frontend && npm install && npm run dev

test:
	cd backend && ./.venv/bin/python -m pytest -q

smoke:
	cd backend && PYTHONPATH=. ./.venv/bin/python scripts/smoke_test.py

minio:
	docker compose up -d

clean-reset:
	rm -f backend/data/review.db backend/data/review.db-wal backend/data/review.db-shm
	rm -rf backend/data/objects
	@echo "已清空本地数据库与本地对象目录（MinIO 里的对象需自行在控制台删除）"
