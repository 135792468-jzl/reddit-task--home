"""Jinja2 模板实例，供入口与各路由共用，避免循环导入。"""
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent

# 前端展示时间统一使用北京时间（UTC+8）。
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _format_time(value):
    """把时间统一转成北京时间并格式化为 YYYY-MM-DD HH:mm:ss（去微秒、去时区后缀）。

    数据库时间为 timestamptz（UTC），psycopg 返回带 tzinfo 的 datetime；
    这里统一 astimezone 到 Asia/Shanghai。空值返回空字符串，不报错。
    """
    if not value:
        return ""
    if isinstance(value, datetime.datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(_SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, datetime.date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _inject_current_user(request):
    """把认证依赖写入 request.state 的当前用户注入所有模板上下文。"""
    return {"current_user": getattr(request.state, "current_user", None)}


_NAV_ACTIVE = {
    "/tasks/post/new": "post_new",
    "/tasks/post": "post_new",
    "/tasks/comment/new": "comment_new",
    "/tasks/comment": "comment_new",
    "/tasks/posts": "posts",
    "/tasks/comments": "comments",
    "/tasks/archived": "archived",
}


def _inject_active_page(request):
    """根据当前路径高亮导航项；路由可先写 request.state.active_page 覆盖。"""
    override = getattr(request.state, "active_page", None)
    if override:
        return {"active_page": override}
    path = request.url.path.rstrip("/") or "/"
    if path == "/admin/users" or path.startswith("/admin/users/"):
        return {"active_page": "admin_users"}
    return {"active_page": _NAV_ACTIVE.get(path)}


templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates"),
    context_processors=[_inject_current_user, _inject_active_page],
)
templates.env.filters["format_time"] = _format_time
