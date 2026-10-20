"""登录 / 登出路由。"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.auth import login_user, logout_user, verify_password
from app.db import get_db
from app.templating import templates

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    """渲染登录页。"""
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    conn=Depends(get_db),
):
    """校验用户名密码；成功写入会话并跳转首页，失败统一提示。"""
    row = conn.execute(
        "select id, username, password_hash, is_active from users where username = %s",
        (username,),
    ).fetchone()

    # 用户不存在 / 密码错误 / 账号已禁用，统一返回同一提示，不区分原因。
    if row is None or not row["is_active"] or not verify_password(password, row["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html", {"error": "用户名或密码错误"}
        )

    login_user(request, row["id"])
    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    """清空会话并跳转登录页。"""
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)
