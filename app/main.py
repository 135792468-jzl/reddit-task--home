"""FastAPI 入口：装配中间件、异常处理与路由。"""
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

import app.db  # noqa: F401  # 导入即加载 .env 并初始化连接池
from app.auth import get_current_user
from app.routers import admin_users
from app.routers import auth as auth_routes
from app.routers import public
from app.routers import tasks
from app.templating import templates

app = FastAPI(title="Reddit 任务管理系统")

# SessionMiddleware 的 cookie：httponly 固定为 True；same_site 默认/显式 "lax"；
# secure 由 https_only 控制（生产走 HTTPS 时把 SESSION_HTTPS_ONLY 设为 true）。
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-only-insecure-secret-change-me"),
    https_only=os.environ.get("SESSION_HTTPS_ONLY", "false").lower() == "true",
    same_site="lax",
)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    # 未登录 / 会话失效 -> 跳转登录页
    if exc.status_code == 401:
        return RedirectResponse(url="/login", status_code=302)
    # 其余错误渲染统一错误页（中文提示）
    message = exc.detail if isinstance(exc.detail, str) else ""
    if message == "Not Found":
        message = "页面不存在"
    if not message:
        message = {403: "无权限访问", 404: "页面不存在", 405: "方法不允许"}.get(
            exc.status_code, "请求出错"
        )
    return templates.TemplateResponse(
        request,
        "error.html",
        {"status_code": exc.status_code, "message": message},
        status_code=exc.status_code,
    )


app.include_router(auth_routes.router)
app.include_router(admin_users.router)
app.include_router(tasks.router)
app.include_router(public.router)

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)


@app.get("/")
def index(request: Request, user=Depends(get_current_user)):
    """首页（后台页面，需登录）。"""
    return templates.TemplateResponse(
        request,
        "index.html",
        {"app_name": app.title, "username": user["username"]},
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
