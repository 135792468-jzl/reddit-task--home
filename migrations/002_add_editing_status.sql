-- =====================================================================
-- 002_add_editing_status.sql
-- 新增「编辑中」状态：待完成 / 编辑中 / 已完成 三种状态可互相切换。
-- 执行位置：Supabase 控制台 → SQL Editor（New query → 粘贴 → Run）
-- =====================================================================

-- 初始建表里的 status CHECK 约束没有显式命名，PostgreSQL 默认命名为
-- 表名_列名_check = tasks_status_check。若你的库里约束名不同，请先
-- 用 \d tasks 或查询 pg_constraint 确认后再改。
alter table tasks drop constraint if exists tasks_status_check;

alter table tasks add constraint tasks_status_check
    check (status in ('pending', 'editing', 'completed'));
