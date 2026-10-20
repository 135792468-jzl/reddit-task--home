"""通用工具函数。"""
import secrets


def generate_public_token() -> str:
    """生成任务公开链接的随机 token。"""
    return secrets.token_urlsafe(32)


def safe_http_link(url):
    """仅当 url 以 http(s) 开头时返回原值，否则返回 None（防止 javascript: 等协议注入）。"""
    if url and url.lower().startswith(("http://", "https://")):
        return url
    return None
