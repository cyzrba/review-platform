"""测试环境：用本地磁盘存储 + 临时 SQLite，不依赖 MinIO。"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="review-platform-test-"))

os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["LOCAL_STORAGE_DIR"] = str(_TMP / "objects")
os.environ["AI_REVIEWER"] = "mock"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _cleanup():
    yield
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def tmp_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="case-", dir=_TMP))
    yield path
    shutil.rmtree(path, ignore_errors=True)
