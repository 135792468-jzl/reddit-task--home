# Reddit 任务管理系统

需要登录使用的任务管理系统：登录用户录入帖子/评论任务，系统生成任务 ID 与公开链接；任何人可通过公开链接查看任务并提交完成凭证，提交后链接锁定。前端由 Jinja2 服务端渲染，后端 FastAPI，数据库为 Supabase 托管的 PostgreSQL。

## 目录结构

```
app/
  main.py                # FastAPI 入口
  db.py                  # psycopg 3 连接池（DATABASE_URL）
  auth.py                # 密码哈希（bcrypt）与会话工具
  routers/               # 业务路由（后续实现）
  templates/             # Jinja2 模板
migrations/
  001_initial_schema.sql # 建表脚本
.env.example             # 环境变量样例（不含真实值）
requirements.txt
```

## 环境要求

- Python 3.10+
- 一个 Supabase 项目（托管 PostgreSQL 与 Storage）

## 快速开始

1. 创建并激活虚拟环境：

   ```bat
   python -m venv .venv
   .venv\Scripts\activate
   ```

2. 安装依赖：

   ```bat
   pip install -r requirements.txt
   ```

3. 配置环境变量：

   ```bat
   copy .env.example .env
   ```

   编辑 `.env`，填入：
   - `DATABASE_URL`：Supabase 连接串（见下方「获取连接串」）
   - `SESSION_SECRET`：会话签名密钥，生成方式
     `python -c "import secrets; print(secrets.token_hex(32))"`

4. 建表：在 Supabase SQL Editor 中执行 `migrations/001_initial_schema.sql`（见下方「建表步骤」）。

5. 启动：

   ```bat
   uvicorn app.main:app --reload
   ```

   浏览器打开 http://127.0.0.1:8000 ，看到占位首页即说明骨架可运行。

## 获取 Supabase 连接串

Supabase 控制台 → 项目 → Project Settings → Database → Connection string：

- 推荐 **Transaction pooler**（端口 6543），适配连接池。
- 连接串形如：
  `postgresql://postgres.[PROJECT_REF]:[PASSWORD]@aws-0-[REGION].pooler.supabase.com:6543/postgres`

把 `[PROJECT_REF]`、`[PASSWORD]`、`[REGION]` 替换成你自己的值。

## 在 Supabase SQL Editor 中建表

1. 登录 [Supabase](https://supabase.com)，进入你的项目。
2. 左侧菜单点击 **SQL Editor**。
3. 点击 **New query**。
4. 打开本地 `migrations/001_initial_schema.sql`，把全部内容粘贴进去。
5. 点击 **Run**（或 Ctrl+Enter）。
6. 验证：左侧 **Table Editor**（或 Database → Tables）中应出现 `users`、`tasks`、`task_submissions` 三张表。

脚本是幂等的（`create ... if not exists`），重复执行不会报错。

## 说明

- **bcrypt 版本**：`requirements.txt` 把 `bcrypt` 锁定在 `4.0.1`，因为 `bcrypt>=4.1` 移除了 passlib 用于探测版本的 `__about__` 属性，会产生告警。锁定后 passlib 的 bcrypt 方案正常工作。
- **public_token 索引**：`public_token` 上的 `unique` 约束已自动创建唯一索引，因此未再单独建普通索引；`status` 非唯一，已显式建索引 `idx_tasks_status`。
- **created_by**：`on delete set null`（删除用户不删任务），同时 `created_by_name` 存创建人用户名快照，删账号后仍可显示创建人。
- **updated_at**：当前按需求仅 `default now()`。如需在行更新时自动刷新，可后续加触发器：

  ```sql
  create or replace function set_updated_at() returns trigger as $$
  begin
    new.updated_at = now();
    return new;
  end $$ language plpgsql;

  create trigger trg_tasks_updated_at
    before update on tasks
    for each row execute function set_updated_at();
  ```
