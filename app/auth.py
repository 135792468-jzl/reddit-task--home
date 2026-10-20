"""密码哈希、会话写入与认证依赖。"""
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from passlib.context import CryptContext

from app.db import get_db

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 会话 cookie 中仅存 user id，使用该键名。
SESSION_USER_KEY = "user_id"


def hash_password(password: str) -> str:
    """对明文密码做 bcrypt 哈希，返回存储用哈希串。"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """校验明文密码与哈希是否匹配；任何异常都视为不匹配。"""
    try:
        return pwd_context.verify(plain_password, password_hash)
    except Exception:
        return False


def login_user(request: Request, user_id) -> None:
    """登录成功后写入会话，只存 user id（不含密码与 is_admin）。"""
    request.session[SESSION_USER_KEY] = str(user_id)


def logout_user(request: Request) -> None:
    """清除会话中的登录信息。"""
    request.session.pop(SESSION_USER_KEY, None)


def get_session_user_id(request: Request) -> Optional[str]:
    """读取会话中的 user id，未登录返回 None。"""
    return request.session.get(SESSION_USER_KEY)


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="未登录或会话已失效")


def get_current_user(request: Request, conn=Depends(get_db)):
    """FastAPI 依赖：校验会话 cookie，返回 users 行（dict）。

    未登录、用户不存在或 is_active=false 时抛 401，
    由 app.main 中的异常处理器统一重定向到登录页。
    """
    raw_id = get_session_user_id(request)
    if not raw_id:
        raise _unauthorized()

    try:
        user_id = UUID(raw_id)
    except (ValueError, TypeError):
        raise _unauthorized()

    row = conn.execute(
        "select id, username, is_admin, is_active from users where id = %s",
        (user_id,),
    ).fetchone()

    if row is None or not row["is_active"]:
        raise _unauthorized()

    # 供模板上下文处理器读取，用于渲染导航（是否登录、是否管理员）。
    request.state.current_user = row
    return row


def require_admin(user=Depends(get_current_user)):
    """FastAPI 依赖：在 get_current_user 基础上要求 is_admin=true，否则 403。"""
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
