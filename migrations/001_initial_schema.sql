-- =====================================================================
-- 001_initial_schema.sql
-- Reddit 任务管理系统 —— 初始建表脚本
-- 执行位置：Supabase 控制台 → SQL Editor（New query → 粘贴 → Run）
-- =====================================================================

-- gen_random_uuid() 在 PG13+ 已内置；这里显式确保 pgcrypto 可用（兼容旧环境）。
create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------
-- 用户表
-- ---------------------------------------------------------------------
create table if not exists users (
    id            uuid primary key default gen_random_uuid(),
    username      text unique not null,
    password_hash text not null,
    is_admin      boolean not null default false,
    is_active     boolean not null default true,
    created_at    timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- 任务表（帖子任务与评论任务共用同一条 identity 序列，id 从 1 开始）
-- ---------------------------------------------------------------------
create table if not exists tasks (
    id                 bigint generated always as identity primary key,
    public_token       text unique not null,
    task_type          text not null check (task_type in ('post', 'comment')),
    title              text,
    body               text,
    subreddit          text,
    flair_text         text,
    media_path         text,
    media_type         text,
    target_post_url    text,
    target_comment_url text,
    comment_body       text,
    status             text not null default 'pending' check (status in ('pending', 'completed')),
    submitted_at       timestamptz,
    created_by         uuid references users(id) on delete set null,
    created_by_name    text not null,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

-- public_token 的 unique 约束已自动创建唯一索引，无需重复建普通索引。
-- 这里仅为 status 建索引，用于按状态筛选。
create index if not exists idx_tasks_status on tasks (status);

-- ---------------------------------------------------------------------
-- 任务提交凭证表
-- ---------------------------------------------------------------------
create table if not exists task_submissions (
    id                   bigint generated always as identity primary key,
    task_id              bigint not null references tasks(id) on delete cascade,
    result_url           text not null,
    screenshot_path      text not null,
    submitter_ip         inet,
    submitter_user_agent text,
    created_at           timestamptz not null default now()
);

create index if not exists idx_task_submissions_task_id on task_submissions (task_id);
