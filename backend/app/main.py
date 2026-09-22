"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .database import init_db
from .routers import classes, courses, exports, files, grading, rubrics, system
from .services.grading import shutdown_executor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("review-platform")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    init_db()
    try:
        from .storage import get_storage

        storage = get_storage()
        ok, detail = storage.health()
        logger.info("对象存储 %s 就绪=%s %s", storage.name, ok, detail or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("对象存储初始化失败（上传时会报错）：%s", exc)
    yield
    shutdown_executor()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=(
        "学生作业 / 实验报告自动评审平台后端：班级名单导入、评分细则管理、"
        "压缩包上传与学生匹配、AI 评审出分与评语、成绩按班级导出。"
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"服务器内部错误：{exc}"})


for module in (system, courses, classes, rubrics, grading, exports, files):
    app.include_router(module.router, prefix=settings.api_prefix)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "app": settings.app_name,
        "docs": "/docs",
        "api_prefix": settings.api_prefix,
    }
