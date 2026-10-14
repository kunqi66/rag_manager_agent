# RAG 知识库管理平台

带**四维细粒度权限鉴权**的企业内部 RAG 知识库管理平台。

核心特征：权限不是事后打补丁，而是**在向量检索阶段就把无权内容挡在候选集之外**——Milvus 标量过滤下推到索引层，另加应用层兜底防越权。

> 📘 需求与技术设计见 [vibecoding_doc/PRD.md](vibecoding_doc/PRD.md)（唯一事实来源）
> 🤖 编码约定见 [CLAUDE.md](CLAUDE.md)

---

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 · FastAPI · SQLAlchemy(async) · Alembic |
| 元数据库 | MySQL 8.0 |
| 向量库 | Milvus 2.5（稠密 BGE + 内置 BM25 稀疏，RRF 融合） |
| 缓存/队列 | Redis · Celery（worker + beat） |
| 生成模型 | DeepSeek `deepseek-chat` |
| 向量模型 | `BAAI/bge-base-zh-v1.5`（768 维，远程 HTTP 调用） |
| 文档解析 | MinerU 官方 API → Markdown |
| 前端 | Vue 3 · TS · Vite · Pinia · Element Plus · ECharts |
| 部署 | docker compose（阿里云 ECS 单机） |

---

## 快速开始

### 容器部署（推荐，一价全包）

```bash
cp .env.example .env
# 填掉 .env 里所有 <必填> 项，尤其 JWT_SECRET：
#   openssl rand -hex 32

docker compose up -d
docker compose ps          # Milvus 首次启动约需 60-90 秒变成 healthy
```

| 入口 | 地址 |
|---|---|
| 后端 API 文档 | http://<ECS-IP>/docs |
| 前端控制台 | http://<ECS-IP>/ |

> 前端尚在建设中，`/` 暂时返回 404，属预期。构建后即可访问：
> `cd frontend && pnpm install && pnpm build`

### 本地开发（不含中间件）

本地**不跑** MySQL / Milvus / Redis——它们依赖 Docker，而开发机未安装。改为直连阿里云上的实例：

```bash
cp .env.example .env
# 把 MYSQL_HOST / MILVUS_HOST / REDIS_HOST 改成 ECS 地址

uv sync
uv run main.py             # http://127.0.0.1:8000/docs
```

> ⚠️ **安全**：ECS 安全组必须把 3306 / 19530 / 6379 限制为**你自己的公网 IP /32**，
> 绝不能对 `0.0.0.0/0` 开放。换网络后记得改白名单。
> 更稳妥的做法是改用 SSH 隧道（见 PRD §11.3 R2）。

---

## 目录结构

```
rag_manager_agent/
├── main.py                     # FastAPI 入口
├── docker-compose.yml          # 单机全量编排
├── .env.example                # 环境变量完整清单
├── CLAUDE.md                   # AI 编码约定
├── vibecoding_doc/PRD.md       # 需求与技术设计（SSOT）
├── deploy/mysql/init/          # MySQL 首次启动初始化脚本
├── nginx/nginx.conf            # 反代 + 前端托管（含 SSE 配置）
├── backend/
│   ├── api/                    # 路由层：只做校验 + 注入 + 转发
│   ├── engines/                # import / search / data_mining 三个引擎
│   ├── clients/                # mineru / embedding / llm / milvus / redis 封装
│   ├── service/                # org / auth / audit / stats 服务
│   ├── models/                 # SQLAlchemy ORM
│   ├── schemas/                # Pydantic DTO
│   ├── tasks/                  # Celery 任务
│   ├── core/                   # 配置 / 日志 / 异常 / 统一响应 / 鉴权依赖
│   └── faq/                    # 进程内 FAQ 向量索引
└── frontend/                   # Vue 3 SPA
```

分层铁律：`api/` 不写业务逻辑与 SQL；`engines/` 不 import FastAPI；`clients/` 不泄漏第三方异常。

---

## 常用命令

```bash
uv sync                                          # 安装依赖
uv run main.py                                   # 本地起 API
uv add <pkg>                                     # 加依赖（勿手改 pyproject 依赖段）

uv run alembic revision --autogenerate -m "xxx"  # 生成迁移
uv run alembic upgrade head                      # 应用迁移

uv run celery -A backend.tasks worker -l info    # 队列 worker
uv run celery -A backend.tasks beat   -l info    # 定时调度

docker compose up -d                             # 起全套
docker compose logs -f api                       # 看日志
docker compose down                              # 停（保留数据卷）
```

---

## 当前进度

| 期次 | 范围 | 验收标准 | 状态 |
|---|---|---|---|
| 骨架 | 目录重构 + 配置层 + compose 编排 | — | ✅ 完成 |
| P1 | 认证组织 + 知识导入 + 鉴权问答 | A1-A5、A9(场景一) | ⬜ 未开始 |
| P2 | 审计 + 运营看板 | A6 | ⬜ 未开始 |
| P3 | FAQ 聚类 + 缓存 + 缺口闭环 | A7、A8、A9(场景二) | ⬜ 未开始 |

## 上线前必办

- [ ] **确认 ECS 规格**（PRD O2）——Milvus 单机版自身需 ~4G，整机至少 4核8G
- [ ] **确认 MinerU 官方 API 的合规结论**（PRD R1）——文档会上传至第三方站点
- [ ] 确认 GPU 服务器上 BGE 服务的部署方式与网络可达性（PRD O3 / O4）
- [ ] 修掉 `user_context` 客户端传参导致的越权风险（PRD R3）
