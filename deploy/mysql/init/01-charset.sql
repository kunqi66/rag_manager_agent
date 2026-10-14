-- MySQL 容器首次启动时自动执行（docker-entrypoint-initdb.d）。
-- 建表由 Alembic 负责，这里只保证库级字符集正确，避免中文乱码。

ALTER DATABASE `rag_kb`
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;
