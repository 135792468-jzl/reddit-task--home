"""Supabase Storage 上传工具。

服务端用 Service Role Key 直连 Storage REST API 上传。该密钥只存在于后端，
切勿下发到浏览器。
"""
import logging
import os
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "task-media")

# 扩展名 -> (mime_type, 类别)
ALLOWED_MEDIA = {
    ".jpg": ("image/jpeg", "image"),
    ".jpeg": ("image/jpeg", "image"),
    ".png": ("image/png", "image"),
    ".mp4": ("video/mp4", "video"),
}

# 提交截图只允许 JPEG / PNG
SCREENSHOT_ALLOWED = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

IMAGE_MAX_BYTES = 20 * 1024 * 1024        # 20 MB
VIDEO_MAX_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB
SCREENSHOT_MAX_BYTES = 10 * 1024 * 1024   # 10 MB

_CHUNK = 1024 * 1024  # 分片上传，每片 1 MB
_TEMP_PREFIX = "tmp/"  # 提交截图先落在临时路径


class MediaValidationError(ValueError):
    """媒体类型或大小不合法的业务错误。"""


def _upload_object(path: str, fileobj, mime_type: str) -> None:
    """把 fileobj 流式上传到指定路径。"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("缺少 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 环境变量")

    url = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{path}"
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": mime_type,
        },
        # 流式上传：按 1 MB 分片读取，避免把整段视频读进内存。
        content=iter(lambda: fileobj.read(_CHUNK), b""),
        timeout=600.0,
    )
    resp.raise_for_status()


def store_media(filename: str, fileobj) -> tuple[str, str]:
    """校验并上传媒体文件，返回 (存储路径, mime_type)。

    fileobj 需支持 seek/tell/read（例如 UploadFile.file）。
    类型或大小不合法时抛 MediaValidationError。
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_MEDIA:
        raise MediaValidationError("媒体仅支持 JPEG / PNG / MP4")

    mime_type, category = ALLOWED_MEDIA[ext]

    fileobj.seek(0, os.SEEK_END)
    size = fileobj.tell()
    fileobj.seek(0)

    max_bytes = IMAGE_MAX_BYTES if category == "image" else VIDEO_MAX_BYTES
    limit_label = "20 MB" if category == "image" else "1 GB"
    if size > max_bytes:
        raise MediaValidationError(f"媒体不能超过 {limit_label}")

    path = f"{uuid.uuid4()}{ext}"
    _upload_object(path, fileobj, mime_type)
    return path, mime_type


def store_screenshot(filename: str, fileobj) -> tuple[str, str]:
    """校验并上传提交截图到临时路径，返回 (临时路径, 正式路径)。

    只接受 JPEG / PNG，且不超过 10 MB。正式路径用于落库；提交成功后由
    move_media 把临时文件移动到正式路径。
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in SCREENSHOT_ALLOWED:
        raise MediaValidationError("Screenshot must be JPEG or PNG")

    fileobj.seek(0, os.SEEK_END)
    size = fileobj.tell()
    fileobj.seek(0)
    if size > SCREENSHOT_MAX_BYTES:
        raise MediaValidationError("Screenshot must be under 10 MB")

    name = f"{uuid.uuid4()}{ext}"
    temp_path = f"{_TEMP_PREFIX}{name}"
    final_path = name
    _upload_object(temp_path, fileobj, SCREENSHOT_ALLOWED[ext])
    return temp_path, final_path


def move_media(source_path: str, dest_path: str) -> None:
    """把对象从临时路径移动到正式路径（优先 move，不支持时退化为 copy + delete）。"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("缺少 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 环境变量")

    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "bucketId": STORAGE_BUCKET,
        "sourceKey": source_path,
        "destinationKey": dest_path,
    }

    try:
        resp = httpx.post(
            f"{SUPABASE_URL}/storage/v1/object/move",
            headers=headers,
            json=body,
            timeout=60.0,
        )
        resp.raise_for_status()
        return
    except httpx.HTTPStatusError:
        # move 端点可能不存在（旧版本 Supabase），退化为 copy + delete。
        pass

    copy_resp = httpx.post(
        f"{SUPABASE_URL}/storage/v1/object/copy",
        headers=headers,
        json=body,
        timeout=60.0,
    )
    copy_resp.raise_for_status()
    delete_media(source_path)


def delete_media(path: str) -> None:
    """删除对象；对象不存在（404）视为成功。"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    url = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{path}"
    resp = httpx.delete(
        url,
        headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
        timeout=60.0,
    )
    if resp.status_code not in (200, 202, 204, 404):
        resp.raise_for_status()


def public_media_url(path: str) -> str:
    """构造公开可访问的媒体 URL（要求存储桶为 public）。

    路径按纯文件名拼接，去掉前后多余斜杠。
    """
    if not SUPABASE_URL or not path:
        return ""
    path = path.strip("/")
    return f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{path}"


def signed_media_url(path: str, expires_in: int = 3600) -> str:
    """生成带有效期的签名 URL（默认 1 小时），用于私有桶里的提交截图。

    任何失败都返回空字符串而不是抛异常，调用方据此渲染占位框。
    """
    if not path:
        return ""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        log.warning("生成签名 URL 失败：缺少 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        return ""
    url = f"{SUPABASE_URL}/storage/v1/object/sign/{STORAGE_BUCKET}/{path}"
    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
            json={"expiresIn": expires_in},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        # HTTPStatusError（4xx/5xx，含对象不存在）或 RequestError（连接失败/超时）
        log.warning("生成签名 URL 失败 path=%s: %s", path, exc)
        return ""
    except ValueError as exc:
        # 响应不是合法 JSON
        log.warning("签名响应解析失败 path=%s: %s", path, exc)
        return ""
    signed = data.get("signedURL") or data.get("signedUrl")
    if not signed:
        log.warning("签名响应缺少 signedURL path=%s: %s", path, data)
        return ""
    if signed.startswith("http"):
        return signed
    return f"{SUPABASE_URL}/{signed.lstrip('/')}"
