# CLAUDE.md

本文件是给 AI 编码助手的工程约定。**先读 [vibecoding_doc/PRD.md](vibecoding_doc/PRD.md)**——那是需求与技术设计的唯一事实来源（SSOT）；本文件只讲「怎么写」。

开发遵循 **spec 驱动**：每个功能先按 `.claude/skills/dev-spec` 生成 spec 文件（放 `specs/`），
评审通过后再照 spec 写代码。元流程与门禁见 [vibecoding_doc/开发过程总纲.md](vibecoding_doc/开发过程总纲.md)，
模板见 `vibecoding_doc/spec-templates/`。

---

## 项目一句话

带四维细粒度权限鉴权的企业内部 RAG 知识库管理平台。核心特征是**权限感知检索**：无权内容在 Milvus 向量检索阶段就被标量过滤挡在候选集之外，另有应用层兜底防越权。

---

## 技术栈（已锁定，不要替换）

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 + FastAPI + SQLAlchemy(async) + Alembic |
| 元数据库 | MySQL 8.0 |
| 向量库 | Milvus 2.5 standalone（稠密 BGE + 内置 BM25 稀疏，RRF 融合） |
| 缓存/队列/定时 | Redis + Celery（worker + beat） |
| 生成模型 | DeepSeek `deepseek-chat` |
| 向量模型 | `BAAI/bge-base-zh-v1.5`（768 维），远程 HTTP 调用 |
| 文档解析 | MinerU 官方 API → Markdown |
| 前端 | Vue 3 + TS + Vite + Pinia + Element Plus + ECharts |
| 部署 | docker compose（阿里云 ECS 单机） |

> 换栈前先改 PRD 的 §0 决策摘要，再动代码。

---

## 目录结构

```
rag_manager_agent/
├── main.py                  # FastAPI 入口
├── pyproject.toml           # uv 管理依赖
├── docker-compose.yml       # 9 个服务：etcd/minio/milvus/mysql/redis/api/worker/beat/nginx
├── .env.example             # 所有环境变量的完整清单（唯一事实来源）
├── .dockerignore
├── CLAUDE.md
├── vibecoding_doc/PRD.md    # SSOT
├── deploy/mysql/init/       # MySQL 容器首次启动初始化脚本
├── nginx/nginx.conf         # 反代 + 前端托管（含 SSE 与上传体积配置）
├── backend/
│   ├── api/                 # 路由层：只做校验 + 注入 + 转发
│   ├── engines/             # import_engine / search_engine / data_mining_engine
│   ├── clients/             # mineru / embedding / llm / milvus / redis 封装
│   ├── service/             # org / auth / audit / stats 服务
│   ├── models/              # SQLAlchemy ORM
│   ├── schemas/             # Pydantic DTO
│   ├── tasks/               # Celery 任务（celery_app.py 为实例定义）
│   ├── core/                # settings / logging / exceptions / response
│   ├── faq/                 # 进程内 FAQ 向量索引
│   └── Dockerfile           # API / worker / beat 共用
└── frontend/                # Vue 3 SPA（dist 由 nginx 挂载）
```

**分层铁律**

- `api/` 禁止出现业务逻辑与 SQL。
- `engines/` 禁止 import FastAPI 的任何东西。
- `clients/` 禁止把第三方异常类型泄漏到上层——统一转成 `core/exceptions.py` 里的自定义异常。
- 跨层数据传递一律用 `schemas/` 里的 Pydantic 模型，不传裸 dict。

---

## 常用命令

```bash
uv sync                                  # 安装依赖
uv run main.py                           # 本地起 API（127.0.0.1:8000）
uv add <pkg>                             # 加依赖（不要手改 pyproject 的依赖段）
uv run alembic revision --autogenerate -m "xxx"   # 生成迁移
uv run alembic upgrade head              # 应用迁移
uv run celery -A backend.tasks worker -l info     # Celery worker
uv run celery -A backend.tasks beat -l info       # Celery beat

docker compose up -d                     # 起全套（MySQL/Milvus/Redis/API/worker/beat/nginx）
docker compose logs -f api
```

**本地开发**：数据库直连阿里云（安全组 IP 白名单放行），不本地起 MySQL/Milvus。

---

## 代码规范

### 后端

- 全异步：路由、引擎、DB 访问一律 `async def` + `await`。**不要在异步函数里调用阻塞 IO**（`requests`、同步 `pymilvus` 之外的重操作）；确有阻塞需求时用 `run_in_threadpool`。
- 所有函数写类型注解，包括返回值。
- 所有接口返回统一结构 `{"code": 0, "message": "ok", "data": ...}`，由全局异常处理器兜底。
- 分页统一 `{"list": [], "total": 0, "page": 1, "page_size": 20}`。
- 配置一律走 `core/settings.py`（pydantic-settings），**禁止**在业务代码里写 `os.getenv`。
- 常量、阈值（`CHUNK_MAX_TOKENS`、`KB_MIN_SCORE` 等）全部进配置，禁止硬编码魔数。
- 日志用 `logging`，禁止 `print`。
- 中文注释，说明「为什么」而不是「做了什么」。

### 前端

- Vue 3 `<script setup lang="ts">` + Composition API，不用 Options API。
- TS 开 `strict`。接口类型集中放 `src/types/`，与后端 `schemas/` 一一对应。
- Element Plus 组件按需引入。
- 状态用 Pinia，**不要**在组件间用 event bus 传数据。
- 请求统一走 `src/api/` 下的封装，不要在组件里直接 `axios`。

---

## 安全红线（违反即视为严重缺陷）

1. **用户身份必须从 JWT 解析**。已有的 `AskRequest.user_context` / `SearchRequest.user_context` 是客户端传参，属越权漏洞——实现时一律用 JWT 载荷覆盖它，PRD §11.3 R3。
2. **LLM 输出与知识正文渲染前必须过 DOMPurify**，再 `v-html`。禁止直接渲染。
3. **密码用 `pwdlib[argon2]`**，禁止 md5/sha1/明文。JWT 用 `pyjwt`，禁止 `python-jose`。
4. **未授权内容一律不返回正文、不返回标题**。权限缺失提示默认只报数量，`ACL_REVEAL_BLOCKED_TITLES` 默认 `false`。
5. **解 MinerU 返回的 zip 前必须做路径穿越校验**（拒绝含 `..` 与绝对路径的条目）与解压体积上限。
6. **密钥一律走环境变量**，`.env` 不进仓库；`.env.example` 只放占位符。
7. **每个写接口独立校验功能权限**，不得依赖前端隐藏按钮。
8. 数据库操作一律参数化（SQLAlchemy ORM 或 `text()` + 绑定参数），禁止 f-string 拼 SQL。

---

## 检索实现要点（最容易写错的地方）

- 混合检索用 `hybrid_search` + `RRFRanker(k=60)`，两个子检索的 `limit` 取 `top_k * 3` 再融合——否则融合后候选太少。
- ACL 过滤表达式：`acl_global == true or array_contains_any(acl_departments, [...]) or ...`。见 PRD §6.2.2。
- **前置过滤之后仍必须做后置兜底**：用 MySQL 权威权限复核每个候选 `document_id`，不一致的剔除 + 告警。这是防越权的硬保证。
- 部门权限是**向下继承**：用户的有效部门集合 = 自身部门 + 全部祖先。授权「财务部」自动覆盖其子孙部门。
- Milvus 的 `content` 字段要设 `enable_analyzer=True` + `analyzer_params={"type": "chinese"}`，否则 BM25 只做精确匹配。
- SSE 必须用 `fetch` + `ReadableStream` 消费，**不能用 `EventSource`**（不支持 POST 与自定义 header）。Nginx 侧要 `proxy_buffering off`。

---

## 当前进度

| 期次 | 范围 | 验收标准 | 状态 |
|---|---|---|---|
| 骨架 | 目录改名 + 配置/日志/异常层 + compose 编排 | — | ✅ 已完成 |
| P1 | 认证组织 + 知识导入 + 鉴权问答 | A1-A5、A9(场景一) | 未开始 |
| P2 | 审计 + 运营看板 | A6 | 未开始 |
| P3 | FAQ 聚类 + 缓存 + 缺口闭环 | A7、A8、A9(场景二) | 未开始 |

**现状**：骨架层已就位——`backend/core/`（settings / logging / exceptions / response）可用，
`backend/tasks/` 的 Celery 实例已注册 5 个任务与 2 个 beat 计划，
`docker-compose.yml` 编排 9 个服务。
3 个 API 路由仍是 `TODO` 壳子；`engines/`、`clients/`、`service/`、`config/`、`models/`、
`schemas/`、`faq/`、`frontend/` 仍为空。

---

## 下一步（P1）

骨架（目录重构 + 配置层 + compose 编排）已完成。P1 按此顺序推进：

1. `backend/models/` + Alembic 初始化 → 建 §7.2 的全部表
2. `backend/core/security.py` + `deps.py` → JWT 签发校验、`get_current_user`、功能权限依赖
3. `backend/api/auth_router.py`、`org_router.py` → 认证与组织管理
4. `backend/clients/` → mineru / embedding / llm / milvus 四个客户端封装
5. `backend/engines/import_engine` → 解析切片入库链路
6. `backend/engines/search_engine` → 前置过滤混合检索 + 后置兜底 + SSE 问答

> ⚠️ 第 2 步之前先修掉 `user_context` 越权问题（安全红线第 1 条）。

---

## 已知坑（踩过的）

**工具链**

- **Starlette 1.7 的 `include_router` 不再把子路由摊平进 `app.routes`**，而是包成
  `_IncludedRouter` 对象。要检查路由是否注册成功，用 `app.openapi()["paths"]`，
  **不要**数 `app.routes`——会漏报。
- `pymysql`/`aiomysql` 连 MySQL 8 的默认 `caching_sha2_password` 需要 `cryptography` 包，
  否则非 SSL 连接直接认证失败。加 `aiomysql` 时一并加 `cryptography`。
- 生产环境配置自检（`settings.py` 的 model_validator）会让缺失密钥的应用**启动即失败**，
  这是刻意设计，不要改成 warning。
- Windows 控制台是 GBK，`uv run python` 打印中文会乱码。验证脚本请断言布尔值，别靠肉眼读中文输出。

**Celery**

- **`retry_backoff` / `retry_backoff_max` 对手动的 `self.retry()` 不生效**，会被静默忽略。
  这两个参数只被 `celery/app/autoretry.py` 读取，服务于 `autoretry_for=` 声明式重试。
  手动重试若不给 `countdown`，Celery 退回 `default_retry_delay = 180` 秒
  （`celery/app/task.py:222` 与 `:755`）。实测版本 Celery 5.6.3。
  → 凡是用 `bind=True` + `self.retry()`，一律显式算退避：
  `countdown = min(cap, base * 2 ** self.request.retries)`。
- Windows 上 worker 必须 `-P solo`（或 `-P threads`），默认 prefork 池在 Windows 上不可用。
- `celery_app.py` 的 `include` 列表漏写任务模块 → worker 报
  `Received unregistered task`，**任务不执行且不报错到业务层**，很难查。

**环境约束**

- 本机**没有 Docker**，Milvus 单机版依赖 Docker → 本地跑不了真 Milvus，开发靠直连云端。
  另：本机也没有 WSL 发行版、没有 psql/mysql/redis/conda。
- 公司 GPU 服务器**只跑 BGE embedding**（PRD D17）；MinerU 走官方 API，有文档出境合规风险（PRD R1）。
- MinerU 官方 API 是**异步**的，只返回 `full_zip_url`，必须下载解压，不回正文。
- ECS 规格尚未确认（PRD O2），暂按 4C8G 规划；低于此 Milvus 会 OOM。
- PRD §11.4 还有 5 项待确认（O2-O6），涉及 GPU 服务部署方式与网络可达性，动 P1 之前先问清楚。

**编排**

- MinIO 镜像版本被 Milvus 锁死在 `RELEASE.2023-03-20T20-16-18Z`，**不要升级**。
- `docker-compose.yml` 里 3306 / 19530 / 6379 对外发布是为了本地直连开发，
  必须靠安全组限制来源 IP（PRD R2）。Redis 已强制 `--requirepass`。
