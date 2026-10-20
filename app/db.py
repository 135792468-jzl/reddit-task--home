"""数据库连接：psycopg 3 + 连接池。

连接串从环境变量 DATABASE_URL 读取（见 .env.example）。
行工厂统一使用 dict_row，查询结果以 dict 形式返回，方便直接传给 Jinja2 模板。
"""
import os

from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# 先加载项目根目录的 .env，确保 DATABASE_URL 等变量在导入时即可用。
load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "缺少环境变量 DATABASE_URL。请复制 .env.example 为 .env 并填写 Supabase 连接串。"
    )

# 连接池：min_size 便于本地开发；max_size 按并发量调整。
# 使用 Supabase 时建议连接 Transaction pooler（端口 6543）的连接串。
pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
)


def get_db():
    """FastAPI 依赖：从连接池借用一个连接，请求结束后自动归还。"""
    with pool.connection() as conn:
        yield conn
