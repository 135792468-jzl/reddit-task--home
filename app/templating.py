"""Jinja2 模板实例，供入口与各路由共用，避免循环导入。"""
from pathlib import Path

from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent


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
