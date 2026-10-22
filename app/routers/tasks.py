"""任务创建路由（需登录）。"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import get_current_user
from app.db import get_db
from app.storage import MediaValidationError, delete_media, public_media_url, store_media
from app.templating import templates
from app.utils import generate_public_token, safe_http_link

log = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks")

_STATUS_LABELS = {"pending": "待完成", "editing": "编辑中", "completed": "已完成"}


def _clean_subreddits(raw):
    """清洗多社区输入：去空格、去 r/ 前缀、过滤空值、去重（大小写不敏感，保持顺序）。"""
    result = []
    seen = set()
    for item in raw or []:
        name = (item or "").strip()
        if name.lower().startswith("r/"):
            name = name[2:].strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result


def _apply_list_filters(conditions, params, id, keyword, status, date, keyword_col):
    """把 id / keyword / status / date 追加为参数化筛选条件。

    keyword_col 为模糊匹配列名（帖子用 title、评论用 target_post_url），
    由调用方硬编码传入，不来自用户输入；其余值全部参数化。
    """
    status = (status or "").strip()
    if status in ("pending", "editing", "completed"):
        conditions.append("t.status = %s")
        params.append(status)

    id_clean = (id or "").strip()
    if id_clean and id_clean.isdigit():
        conditions.append("t.id = %s")
        params.append(int(id_clean))

    kw = (keyword or "").strip()
    if kw:
        conditions.append(f"t.{keyword_col} ILIKE %s")
        params.append(f"%{kw}%")

    date_clean = (date or "").strip()
    if date_clean:
        try:
            day = datetime.date.fromisoformat(date_clean)
        except ValueError:
            day = None
        if day is not None:
            conditions.append("DATE(t.created_at) = %s")
            params.append(day)


def _fetch_task_by_id(conn, task_id):
    """按 id 取任务（含全部内容字段）。"""
    return conn.execute(
        """
        select id, public_token, task_type, title, body, subreddits, flair_text,
               media_path, media_type, target_post_url, comment_body, target_comment_url,
               status, is_archived, remark, created_by_name, created_at
        from tasks
        where id = %s
        """,
        (task_id,),
    ).fetchone()


def _render_post_form(request, errors=None, values=None, status_code=200):
    values = values or {}
    return templates.TemplateResponse(
        request,
        "tasks/post_form.html",
        {
            "errors": errors or [],
            "title": values.get("title", ""),
            "body": values.get("body", ""),
            "subreddits": values.get("subreddits") or [""],
            "flair_text": values.get("flair_text", ""),
        },
        status_code=status_code,
    )


def _render_comment_form(request, errors=None, values=None, status_code=200):
    values = values or {}
    return templates.TemplateResponse(
        request,
        "tasks/comment_form.html",
        {
            "errors": errors or [],
            "target_post_url": values.get("target_post_url", ""),
            "comment_body": values.get("comment_body", ""),
            "target_comment_url": values.get("target_comment_url", ""),
        },
        status_code=status_code,
    )


def _render_success(request, task_id, absolute_url, back_url):
    return templates.TemplateResponse(
        request,
        "tasks/success.html",
        {"task_id": task_id, "absolute_url": absolute_url, "back_url": back_url},
    )


def _render_post_edit(request, task, values, errors=None, status_code=200):
    """渲染帖子编辑表单：优先回显用户提交值，否则回退到任务现有值。"""
    context = {
        "task": task,
        "errors": errors or [],
        "title": values.get("title", task["title"] or ""),
        "body": values.get("body", task["body"] or ""),
        "subreddits": values.get("subreddits") or (task["subreddits"] or [""]),
        "flair_text": values.get("flair_text", task["flair_text"] or ""),
    }
    if task["media_path"]:
        context["media_url"] = public_media_url(task["media_path"])
    return templates.TemplateResponse(
        request, "tasks/post_edit.html", context, status_code=status_code
    )


def _render_comment_edit(request, task, values, errors=None, status_code=200):
    """渲染评论编辑表单：优先回显用户提交值，否则回退到任务现有值。"""
    context = {
        "task": task,
        "errors": errors or [],
        "target_post_url": values.get("target_post_url", task["target_post_url"] or ""),
        "comment_body": values.get("comment_body", task["comment_body"] or ""),
        "target_comment_url": values.get("target_comment_url", task["target_comment_url"] or ""),
    }
    return templates.TemplateResponse(
        request, "tasks/comment_edit.html", context, status_code=status_code
    )


@router.get("/post/new")
def new_post_page(request: Request, current=Depends(get_current_user)):
    """录入帖子表单页。"""
    return _render_post_form(request)


@router.post("/post")
def create_post_task(
    request: Request,
    title: str = Form(...),
    body: str = Form(""),
    subreddits: list[str] = Form(default=[]),
    flair_text: str = Form(""),
    media: Optional[UploadFile] = File(None),
    current=Depends(get_current_user),
    conn=Depends(get_db),
):
    """处理帖子任务创建表单：成功渲染成功页，失败回显并显示错误。"""
    values = {
        "title": title,
        "body": body,
        "subreddits": subreddits,
        "flair_text": flair_text,
    }
    errors = []

    title_clean = (title or "").strip()
    if not title_clean:
        errors.append("帖子标题不能为空")
    elif len(title_clean) > 300:
        errors.append("帖子标题不能超过 300 字符")

    subreddits_clean = _clean_subreddits(subreddits)
    if not subreddits_clean:
        errors.append("目标社区不能为空")

    media_path = None
    media_type = None
    if media is not None and media.filename:
        try:
            media_path, media_type = store_media(media.filename, media.file)
        except MediaValidationError as exc:
            errors.append(str(exc))
        except Exception:
            log.exception("媒体上传失败")
            errors.append("媒体上传失败")

    if errors:
        return _render_post_form(request, errors, values, status_code=400)

    token = generate_public_token()
    row = conn.execute(
        """
        insert into tasks
            (public_token, task_type, title, body, subreddits, flair_text,
             media_path, media_type, created_by, created_by_name)
        values (%s, 'post', %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            token,
            title_clean,
            body,
            subreddits_clean,
            flair_text,
            media_path,
            media_type,
            current["id"],
            current["username"],
        ),
    ).fetchone()

    public_url = f"/t/{token}"
    absolute_url = str(request.base_url).rstrip("/") + public_url
    return _render_success(request, row["id"], absolute_url, "/tasks/post/new")


@router.get("/comment/new")
def new_comment_page(request: Request, current=Depends(get_current_user)):
    """录入评论表单页。"""
    return _render_comment_form(request)


@router.post("/comment")
def create_comment_task(
    request: Request,
    target_post_url: str = Form(...),
    comment_body: str = Form(...),
    target_comment_url: str = Form(""),
    current=Depends(get_current_user),
    conn=Depends(get_db),
):
    """处理评论任务创建表单：成功渲染成功页，失败回显并显示错误。"""
    values = {
        "target_post_url": target_post_url,
        "comment_body": comment_body,
        "target_comment_url": target_comment_url,
    }
    errors = []

    url = (target_post_url or "").strip()
    if not url:
        errors.append("目标帖子链接不能为空")
    elif not url.lower().startswith(("http://", "https://")):
        errors.append("目标帖子链接必须以 http/https 开头")

    if not (comment_body or "").strip():
        errors.append("评论内容不能为空")

    if errors:
        return _render_comment_form(request, errors, values, status_code=400)

    target_comment_url = (target_comment_url or "").strip() or None

    token = generate_public_token()
    row = conn.execute(
        """
        insert into tasks
            (public_token, task_type, target_post_url, comment_body, target_comment_url,
             created_by, created_by_name)
        values (%s, 'comment', %s, %s, %s, %s, %s)
        returning id
        """,
        (
            token,
            url,
            comment_body,
            target_comment_url,
            current["id"],
            current["username"],
        ),
    ).fetchone()

    public_url = f"/t/{token}"
    absolute_url = str(request.base_url).rstrip("/") + public_url
    return _render_success(request, row["id"], absolute_url, "/tasks/comment/new")


@router.get("/posts")
def list_posts(
    request: Request,
    id: str = None,
    keyword: str = None,
    status: str = None,
    date: str = None,
    current=Depends(get_current_user),
    conn=Depends(get_db),
):
    """帖子任务列表（按 id 倒序，支持 id/keyword/status/date 多条件筛选）。"""
    conditions = ["t.task_type = 'post'", "t.is_archived = false"]
    params: list = []
    _apply_list_filters(conditions, params, id, keyword, status, date, "title")

    where = " and ".join(conditions)
    rows = conn.execute(
        f"""
        select t.id, t.title, t.subreddits, t.flair_text, t.status, t.public_token,
               t.remark, t.created_at,
               (select s.result_url from task_submissions s
                where s.task_id = t.id order by s.id desc limit 1) as result_url
        from tasks t
        where {where}
        order by t.id desc
        """,
        params,
    ).fetchall()

    base = str(request.base_url).rstrip("/")
    tasks = []
    for r in rows:
        tasks.append(
            {
                "id": r["id"],
                "title": r["title"],
                "subreddits": r["subreddits"] or [],
                "flair_text": r["flair_text"],
                "status": r["status"],
                "remark": r["remark"],
                "created_at": r["created_at"],
                "public_url": f"/t/{r['public_token']}",
                "absolute_url": f"{base}/t/{r['public_token']}",
                "result_url": r["result_url"],
            }
        )

    return templates.TemplateResponse(request, "tasks/posts_list.html", {"tasks": tasks})


@router.get("/comments")
def list_comments(
    request: Request,
    id: str = None,
    keyword: str = None,
    status: str = None,
    date: str = None,
    current=Depends(get_current_user),
    conn=Depends(get_db),
):
    """评论任务列表（按 id 倒序，支持 id/keyword/status/date 多条件筛选）。"""
    conditions = ["t.task_type = 'comment'", "t.is_archived = false"]
    params: list = []
    _apply_list_filters(conditions, params, id, keyword, status, date, "target_post_url")

    where = " and ".join(conditions)
    rows = conn.execute(
        f"""
        select t.id, t.target_post_url, t.target_comment_url, t.status, t.public_token,
               t.remark, t.created_at,
               (select s.result_url from task_submissions s
                where s.task_id = t.id order by s.id desc limit 1) as result_url
        from tasks t
        where {where}
        order by t.id desc
        """,
        params,
    ).fetchall()

    base = str(request.base_url).rstrip("/")
    tasks = []
    for r in rows:
        tasks.append(
            {
                "id": r["id"],
                "target_post_url": r["target_post_url"],
                "is_reply": bool(r["target_comment_url"]),
                "status": r["status"],
                "remark": r["remark"],
                "created_at": r["created_at"],
                "public_url": f"/t/{r['public_token']}",
                "absolute_url": f"{base}/t/{r['public_token']}",
                "result_url": r["result_url"],
            }
        )

    return templates.TemplateResponse(request, "tasks/comments_list.html", {"tasks": tasks})


@router.get("/archived")
def list_archived(request: Request, current=Depends(get_current_user), conn=Depends(get_db)):
    """已归档任务列表：帖子 + 评论统一展示，按 id 倒序。"""
    rows = conn.execute(
        """
        select t.id, t.task_type, t.title, t.target_post_url, t.status, t.remark,
               t.created_at, t.public_token,
               (select s.result_url from task_submissions s
                where s.task_id = t.id order by s.id desc limit 1) as result_url
        from tasks t
        where t.is_archived = true
        order by t.id desc
        """
    ).fetchall()

    base = str(request.base_url).rstrip("/")
    tasks = []
    for r in rows:
        tasks.append(
            {
                "id": r["id"],
                "task_type": r["task_type"],
                "title": r["title"],
                "target_post_url": r["target_post_url"],
                "status": r["status"],
                "remark": r["remark"],
                "created_at": r["created_at"],
                "absolute_url": f"{base}/t/{r['public_token']}",
                "result_url": r["result_url"],
            }
        )

    return templates.TemplateResponse(request, "tasks/archived_list.html", {"tasks": tasks})


@router.get("/{task_id}")
def task_detail(task_id: int, request: Request, current=Depends(get_current_user), conn=Depends(get_db)):
    """任务详情：上半部分任务内容，下半部分提交记录（截图用签名 URL）。"""
    task = _fetch_task_by_id(conn, task_id)

    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 详情页导航高亮：按任务类型归属到帖子/评论任务。
    request.state.active_page = "posts" if task["task_type"] == "post" else "comments"

    # 提交记录单独查询，避免 JOIN 让任务内容重复。
    submissions = conn.execute(
        """
        select result_url, screenshot_path, submitter_ip, submitter_user_agent, created_at
        from task_submissions
        where task_id = %s
        order by created_at desc
        """,
        (task_id,),
    ).fetchall()

    sub_rows = []
    for s in submissions:
        # task-media 为公开桶，直接用公开 URL，不再生成会过期的签名 URL。
        sub_rows.append(
            {
                "result_url": s["result_url"],
                "screenshot_url": public_media_url(s["screenshot_path"]),
                "submitter_ip": s["submitter_ip"],
                "submitter_user_agent": s["submitter_user_agent"],
                "created_at": s["created_at"],
            }
        )

    base = str(request.base_url).rstrip("/")
    context = {
        "task": task,
        "submissions": sub_rows,
        "absolute_url": base + f"/tasks/{task_id}",
        "public_url": f"/t/{task['public_token']}",
        "public_absolute_url": base + f"/t/{task['public_token']}",
        "safe_post_url": safe_http_link(task["target_post_url"]),
        "safe_comment_url": safe_http_link(task["target_comment_url"]),
        "flash": request.session.pop("flash", None),
    }
    if task["media_path"]:
        context["media_url"] = public_media_url(task["media_path"])

    return templates.TemplateResponse(request, "tasks/detail.html", context)


@router.post("/{task_id}/status")
def set_task_status(task_id: int, request: Request, status: str = Form(...), current=Depends(get_current_user), conn=Depends(get_db)):
    """手动切换任务状态（待完成 / 编辑中 / 已完成 可互相切换）。

    只有「离开已完成」时才重新生成公开链接（旧链接立即失效）。
    """
    if status not in _STATUS_LABELS:
        request.session["flash"] = "无效的状态"
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    task = conn.execute(
        "select id, status from tasks where id = %s", (task_id,)
    ).fetchone()

    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    old_status = task["status"]
    if old_status == status:
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    if old_status == "completed" and status != "completed":
        # 离开已完成：重新生成公开链接，旧链接立即失效。
        new_token = generate_public_token()
        conn.execute(
            """
            update tasks
            set status = %s, public_token = %s, submitted_at = null, updated_at = now()
            where id = %s and status = 'completed'
            """,
            (status, new_token, task_id),
        )
        request.session["flash"] = f"已切换为{_STATUS_LABELS[status]}，新公开链接已生成"
    elif status == "completed":
        conn.execute(
            "update tasks set status = %s, submitted_at = now(), updated_at = now() where id = %s",
            (status, task_id),
        )
        request.session["flash"] = f"已切换为{_STATUS_LABELS[status]}"
    else:
        conn.execute(
            "update tasks set status = %s, updated_at = now() where id = %s",
            (status, task_id),
        )
        request.session["flash"] = f"已切换为{_STATUS_LABELS[status]}"

    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@router.post("/{task_id}/archive")
def archive_task(task_id: int, current=Depends(get_current_user), conn=Depends(get_db)):
    """归档任务（列表页操作列「归档」按钮）。"""
    task = conn.execute("select id, task_type from tasks where id = %s", (task_id,)).fetchone()
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    conn.execute(
        "update tasks set is_archived = true, updated_at = now() where id = %s", (task_id,)
    )
    back = "/tasks/posts" if task["task_type"] == "post" else "/tasks/comments"
    return RedirectResponse(url=back, status_code=303)


@router.post("/{task_id}/unarchive")
def unarchive_task(task_id: int, current=Depends(get_current_user), conn=Depends(get_db)):
    """取消归档（归档列表页操作列「取消归档」按钮）。"""
    task = conn.execute("select id from tasks where id = %s", (task_id,)).fetchone()
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    conn.execute(
        "update tasks set is_archived = false, updated_at = now() where id = %s", (task_id,)
    )
    return RedirectResponse(url="/tasks/archived", status_code=303)


@router.post("/{task_id}/remark")
def set_task_remark(task_id: int, remark: str = Form(""), current=Depends(get_current_user), conn=Depends(get_db)):
    """保存任务备注（列表页 / 详情页内联编辑，Fetch 调用，返回 JSON 不刷新页面）。"""
    task = conn.execute("select id from tasks where id = %s", (task_id,)).fetchone()
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    remark_clean = (remark or "").strip()
    conn.execute(
        "update tasks set remark = %s, updated_at = now() where id = %s",
        (remark_clean or None, task_id),
    )
    return JSONResponse({"ok": True, "remark": remark_clean})


@router.get("/{task_id}/edit")
def edit_task_page(task_id: int, request: Request, current=Depends(get_current_user), conn=Depends(get_db)):
    """编辑任务表单页（仅「编辑中」状态可进入）。"""
    task = _fetch_task_by_id(conn, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] != "editing":
        request.session["flash"] = "仅「编辑中」状态的任务可修改"
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    request.state.active_page = "posts" if task["task_type"] == "post" else "comments"
    if task["task_type"] == "post":
        return _render_post_edit(request, task, {})
    return _render_comment_edit(request, task, {})


@router.post("/{task_id}/edit")
def update_task(
    task_id: int,
    request: Request,
    title: str = Form(""),
    body: str = Form(""),
    subreddits: list[str] = Form(default=[]),
    flair_text: str = Form(""),
    target_post_url: str = Form(""),
    comment_body: str = Form(""),
    target_comment_url: str = Form(""),
    media: Optional[UploadFile] = File(None),
    current=Depends(get_current_user),
    conn=Depends(get_db),
):
    """保存任务内容修改（仅「编辑中」状态）。"""
    task = _fetch_task_by_id(conn, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] != "editing":
        request.session["flash"] = "仅「编辑中」状态的任务可修改"
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    if task["task_type"] == "post":
        return _update_post_task(
            request, task, title, body, subreddits, flair_text, media, conn
        )
    return _update_comment_task(
        request, task, target_post_url, comment_body, target_comment_url, conn
    )


def _update_post_task(request, task, title, body, subreddits, flair_text, media, conn):
    values = {"title": title, "body": body, "subreddits": subreddits, "flair_text": flair_text}
    errors = []

    title_clean = (title or "").strip()
    if not title_clean:
        errors.append("帖子标题不能为空")
    elif len(title_clean) > 300:
        errors.append("帖子标题不能超过 300 字符")

    subreddits_clean = _clean_subreddits(subreddits)
    if not subreddits_clean:
        errors.append("目标社区不能为空")

    new_media_path = task["media_path"]
    new_media_type = task["media_type"]
    if media is not None and media.filename:
        try:
            new_media_path, new_media_type = store_media(media.filename, media.file)
        except MediaValidationError as exc:
            errors.append(str(exc))
        except Exception:
            log.exception("媒体上传失败")
            errors.append("媒体上传失败")

    if errors:
        return _render_post_edit(request, task, values, errors, status_code=400)

    conn.execute(
        """
        update tasks
        set title = %s, body = %s, subreddits = %s, flair_text = %s,
            media_path = %s, media_type = %s, updated_at = now()
        where id = %s
        """,
        (title_clean, body, subreddits_clean, flair_text, new_media_path, new_media_type, task["id"]),
    )
    conn.commit()

    # 替换了旧媒体：删除旧文件以节省空间，失败只告警不阻断。
    if new_media_path and task["media_path"] and new_media_path != task["media_path"]:
        try:
            delete_media(task["media_path"])
        except Exception:
            log.warning("删除旧媒体失败: %s", task["media_path"], exc_info=True)

    request.session["flash"] = "已保存修改"
    return RedirectResponse(url=f"/tasks/{task['id']}", status_code=303)


def _update_comment_task(request, task, target_post_url, comment_body, target_comment_url, conn):
    values = {
        "target_post_url": target_post_url,
        "comment_body": comment_body,
        "target_comment_url": target_comment_url,
    }
    errors = []

    url = (target_post_url or "").strip()
    if not url:
        errors.append("目标帖子链接不能为空")
    elif not url.lower().startswith(("http://", "https://")):
        errors.append("目标帖子链接必须以 http/https 开头")

    if not (comment_body or "").strip():
        errors.append("评论内容不能为空")

    if errors:
        return _render_comment_edit(request, task, values, errors, status_code=400)

    target_comment_url = (target_comment_url or "").strip() or None
    conn.execute(
        """
        update tasks
        set target_post_url = %s, comment_body = %s, target_comment_url = %s, updated_at = now()
        where id = %s
        """,
        (url, comment_body, target_comment_url, task["id"]),
    )
    conn.commit()

    request.session["flash"] = "已保存修改"
    return RedirectResponse(url=f"/tasks/{task['id']}", status_code=303)
