"""公开任务页面与提交接口（无需登录）。"""
import ipaddress
import logging
import os
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from app.db import get_db
from app.storage import (
    SCREENSHOT_MAX_BYTES,
    MediaValidationError,
    delete_media,
    move_media,
    public_media_url,
    store_screenshot,
)
from app.templating import templates
from app.utils import safe_http_link as _safe_link

log = logging.getLogger(__name__)

router = APIRouter()

# 简单内存限流：同一 token 每分钟最多 5 次提交请求。
# 生产环境应换 Redis：内存字典在多进程 / 多实例下各自独立，无法全局限流。
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW = 60.0
_rate_hits: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def _check_rate_limit(token: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(token, []) if now - t < _RATE_LIMIT_WINDOW]
        if len(hits) >= _RATE_LIMIT_MAX:
            _rate_hits[token] = hits
            return False
        hits.append(now)
        _rate_hits[token] = hits
        return True


def _client_ip(request: Request):
    """取客户端 IP：反向代理场景优先 X-Forwarded-For 的第一个 IP。"""
    xff = request.headers.get("x-forwarded-for")
    candidate = xff.split(",")[0].strip() if xff else ""
    if not candidate and request.client:
        candidate = request.client.host or ""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _fetch_task(conn, token):
    return conn.execute(
        """
        select id, public_token, task_type, title, body, subreddit, flair_text,
               media_path, media_type, target_post_url, comment_body, target_comment_url, status
        from tasks
        where public_token = %s
        """,
        (token,),
    ).fetchone()


def _build_pending_context(task):
    context = {
        "task": task,
        "safe_post_url": _safe_link(task["target_post_url"]),
        "safe_comment_url": _safe_link(task["target_comment_url"]),
    }
    if task["media_path"]:
        media_url = public_media_url(task["media_path"])
        if media_url:
            context["media_url"] = media_url
    return context


def _render_completed(request, task_id):
    resp = templates.TemplateResponse(
        request, "public/completed.html", {"task_id": task_id}
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _render_pending_error(request, task, errors, result_url="", confirm_checked=False):
    context = _build_pending_context(task)
    context.update(
        {
            "errors": errors,
            "form_result_url": result_url,
            "form_confirm": confirm_checked,
        }
    )
    resp = templates.TemplateResponse(
        request, "public/task.html", context, status_code=400
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/t/{token}")
def public_task_page(token: str, request: Request, conn=Depends(get_db)):
    """按 public_token 查看任务（公开）。"""
    task = _fetch_task(conn, token)

    if task is None:
        # 查不到或 token 不存在：统一返回 404，不透露 token 是否曾存在。
        return templates.TemplateResponse(
            request, "public/not_found.html", {}, status_code=404
        )

    if task["status"] == "completed":
        return _render_completed(request, task["id"])

    resp = templates.TemplateResponse(
        request, "public/task.html", _build_pending_context(task)
    )
    # pending 会变为 completed，避免浏览器缓存旧状态 / 旧的提交表单。
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/t/{token}/submit")
def submit_task(
    token: str,
    request: Request,
    result_url: str = Form(""),
    screenshot: Optional[UploadFile] = File(None),
    confirm: str = Form(""),
    conn=Depends(get_db),
):
    """提交完成凭证：校验 -> 上传临时截图 -> 原子锁定 -> 记录凭证。"""
    # 限流：同一 token 每分钟最多 5 次。
    if not _check_rate_limit(token):
        return JSONResponse(
            status_code=429, content={"detail": "Too many requests. Please try again later."}
        )

    task = _fetch_task(conn, token)
    if task is None:
        return templates.TemplateResponse(
            request, "public/not_found.html", {}, status_code=404
        )

    if task["status"] == "completed":
        return _render_completed(request, task["id"])

    # ---- 校验（失败返回原表单，不改变任务状态）----
    errors = []
    result_url = (result_url or "").strip()
    confirm_checked = (confirm or "").strip().lower() in ("on", "true", "1", "yes")

    if not result_url:
        errors.append("Result link is required")
    elif not _safe_link(result_url):
        errors.append("Result link must start with http:// or https://")

    if screenshot is None or not screenshot.filename:
        errors.append("Screenshot is required")
    else:
        ext = os.path.splitext(screenshot.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            errors.append("Screenshot must be JPEG or PNG")
        else:
            screenshot.file.seek(0, os.SEEK_END)
            size = screenshot.file.tell()
            screenshot.file.seek(0)
            if size > SCREENSHOT_MAX_BYTES:
                errors.append("Screenshot must be under 10 MB")

    if not confirm_checked:
        errors.append("Please check the confirmation box")

    if errors:
        return _render_pending_error(
            request, task, errors, result_url, confirm_checked
        )

    # ---- 上传截图到临时路径 ----
    try:
        temp_path, final_path = store_screenshot(screenshot.filename, screenshot.file)
    except MediaValidationError as exc:
        return _render_pending_error(
            request, task, [str(exc)], result_url, confirm_checked
        )
    except Exception:
        log.exception("截图上传失败")
        return _render_pending_error(
            request, task, ["Screenshot upload failed. Please try again."], result_url, confirm_checked
        )

    # ---- 原子条件更新：只有仍是 pending 才置为 completed ----
    row = conn.execute(
        """
        update tasks
        set status = 'completed', submitted_at = now(), updated_at = now()
        where public_token = %s and status = 'pending'
        returning id
        """,
        (token,),
    ).fetchone()

    if row is None:
        # 已被抢先提交：清理临时截图，渲染已完成页。
        try:
            delete_media(temp_path)
        except Exception:
            log.warning("清理临时截图失败: %s", temp_path, exc_info=True)
        return _render_completed(request, task["id"])

    task_id = row["id"]

    # ---- 同一事务插入 task_submissions ----
    submitter_ip = _client_ip(request)
    submitter_ua = (request.headers.get("user-agent") or "")[:512] or None
    try:
        conn.execute(
            """
            insert into task_submissions
                (task_id, result_url, screenshot_path, submitter_ip, submitter_user_agent)
            values (%s, %s, %s, %s::inet, %s)
            """,
            (task_id, result_url, final_path, submitter_ip, submitter_ua),
        )
        conn.commit()
    except Exception:
        # 回滚条件更新，任务状态回到 pending，让用户可重试。
        conn.rollback()
        log.exception("写入提交凭证失败")
        try:
            delete_media(temp_path)
        except Exception:
            log.warning("清理临时截图失败: %s", temp_path, exc_info=True)
        return _render_pending_error(
            request, task, ["Submission failed. Please try again."], result_url, confirm_checked
        )

    # ---- 临时截图移动到正式路径（提交已成功，移动失败不阻断）----
    try:
        move_media(temp_path, final_path)
    except Exception:
        log.warning("移动截图失败: %s -> %s", temp_path, final_path, exc_info=True)

    return _render_completed(request, task_id)
