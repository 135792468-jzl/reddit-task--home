"""账号管理路由（仅管理员，前缀 /admin/users）。

所有路由都通过 require_admin 依赖强制管理员权限。
“删除”采用软删除（is_active = false）。
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from psycopg import errors as pg_errors

from app.auth import hash_password, require_admin
from app.db import get_db
from app.templating import templates

router = APIRouter(prefix="/admin/users")


def _fetch_users(conn):
    """查询所有账号，仅返回页面需要的字段（不含密码哈希）。"""
    rows = conn.execute(
        "select id, username, is_admin, is_active, created_at from users order by created_at"
    ).fetchall()
    return [
        {
            "id": str(r["id"]),
            "username": r["username"],
            "is_admin": r["is_admin"],
            "is_active": r["is_active"],
            "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M:%S") if r["created_at"] else "",
        }
        for r in rows
    ]


def _render_list(request, conn, current, error=None, status_code=200):
    """渲染账号列表页，可选附带错误提示与状态码。"""
    return templates.TemplateResponse(
        request,
        "admin/users_list.html",
        {
            "users": _fetch_users(conn),
            "current_user_id": str(current["id"]),
            "error": error,
        },
        status_code=status_code,
    )


def _get_user(conn, user_id):
    return conn.execute(
        "select id, username, is_admin, is_active from users where id = %s",
        (user_id,),
    ).fetchone()


def _lock_active_admin_ids(conn):
    """在事务内锁定并返回所有“启用中的管理员”id。

    先 FOR UPDATE 锁住全部 active admin 行再统计，能让并发的两个“删管理员”请求串行化：
    后到者阻塞在前一个事务的锁上，等它提交后再统计只会看到剩下的管理员，
    从而避免两个请求同时删掉最后两个管理员。
    """
    rows = conn.execute(
        "select id from users where is_admin = true and is_active = true for update"
    ).fetchall()
    return [r["id"] for r in rows]


@router.get("")
def list_users(request: Request, current=Depends(require_admin), conn=Depends(get_db)):
    """列出所有账号。"""
    return _render_list(request, conn, current)


@router.get("/new")
def new_user_page(request: Request, current=Depends(require_admin)):
    """新建账号表单页。"""
    return templates.TemplateResponse(
        request, "admin/users_form.html", {"errors": [], "username": ""}
    )


@router.post("")
def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: str = Form(""),
    current=Depends(require_admin),
    conn=Depends(get_db),
):
    """创建账号。用户名唯一，密码至少 8 位。"""
    username = username.strip()
    errors = []
    if not username:
        errors.append("用户名不能为空")
    if len(password) < 8:
        errors.append("密码至少 8 位")
    if errors:
        return templates.TemplateResponse(
            request,
            "admin/users_form.html",
            {"errors": errors, "username": username},
            status_code=400,
        )

    # 复选框：选中时提交 "on"，未选中不提交该字段（默认 ""）。
    admin_flag = is_admin.strip().lower() in ("on", "true", "1", "yes")

    try:
        conn.execute(
            "insert into users (username, password_hash, is_admin) values (%s, %s, %s)",
            (username, hash_password(password), admin_flag),
        )
    except pg_errors.UniqueViolation:
        conn.rollback()
        return templates.TemplateResponse(
            request,
            "admin/users_form.html",
            {"errors": ["用户名已存在"], "username": username},
            status_code=400,
        )

    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/{user_id}/delete")
def delete_user(
    user_id: UUID,
    request: Request,
    current=Depends(require_admin),
    conn=Depends(get_db),
):
    """软删除账号（is_active = false）。"""
    if user_id == current["id"]:
        return _render_list(request, conn, current, error="不能禁用或删除当前登录的自己", status_code=400)

    target = _get_user(conn, user_id)
    if target is None:
        return _render_list(request, conn, current, error="用户不存在", status_code=404)

    active_admin_ids = _lock_active_admin_ids(conn)
    if user_id in active_admin_ids and len(active_admin_ids) == 1:
        return _render_list(request, conn, current, error="不能删除或禁用系统中最后一个管理员", status_code=400)

    conn.execute("update users set is_active = false where id = %s", (user_id,))
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: UUID,
    request: Request,
    password: str = Form(...),
    current=Depends(require_admin),
    conn=Depends(get_db),
):
    """管理员重置他人密码。"""
    if len(password) < 8:
        return _render_list(request, conn, current, error="密码至少 8 位", status_code=400)

    target = _get_user(conn, user_id)
    if target is None:
        return _render_list(request, conn, current, error="用户不存在", status_code=404)

    conn.execute(
        "update users set password_hash = %s where id = %s",
        (hash_password(password), user_id),
    )
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/{user_id}/toggle-admin")
def toggle_admin(
    user_id: UUID,
    request: Request,
    current=Depends(require_admin),
    conn=Depends(get_db),
):
    """切换管理员身份；降级最后一个启用管理员会被拒绝。"""
    active_admin_ids = _lock_active_admin_ids(conn)

    target = _get_user(conn, user_id)
    if target is None:
        return _render_list(request, conn, current, error="用户不存在", status_code=404)

    new_is_admin = not target["is_admin"]
    if target["is_admin"]:
        # 降级：不允许把最后一个启用管理员降级
        if user_id in active_admin_ids and len(active_admin_ids) == 1:
            return _render_list(request, conn, current, error="不能降级系统中最后一个管理员", status_code=400)

    conn.execute("update users set is_admin = %s where id = %s", (new_is_admin, user_id))
    return RedirectResponse(url="/admin/users", status_code=303)
