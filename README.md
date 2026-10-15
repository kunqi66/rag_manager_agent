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
docker compose ps -a       # Milvus 首次启动约需 60-90 秒变成 healthy
```

**启动顺序**（compose 会自动保证，不用手动干预）：

```
mysql healthy → migrate(一次性) → api / worker / beat → api healthy → nginx
                 alembic upgrade head
```

> 🔴 **`rag-migrate` 的状态是 `Exited (0)` 是正常的** —— 它是一次性任务，跑完即退。
> 只有它**成功退出**之后 api / worker / beat 才会启动；迁移失败则它们一律不启动。
> 这是刻意的：schema 不对时，「进程起来了但每个请求 500」比「根本没起来」难查得多。
>
> ```bash
> docker compose ps -a            # 看 migrate 的退出码
> docker compose logs migrate     # 迁移失败的真正报错在这里，不在 api 日志里
> ```
>
> 迁移失败修复后：`docker compose up -d`（compose 会重跑 migrate）。

| 入口 | 地址 |
|---|---|
| 后端 API 文档 | http://<ECS-IP>/docs |
| 前端控制台 | http://<ECS-IP>/ |

> 前端产物由 nginx 直接挂载 `./frontend/dist`；该目录为空时 `/` 返回 404 属预期。
> 构建后即可访问：`cd frontend && npm install && npm run build`

### 本地开发（不含中间件）

本地**不跑** MySQL / Milvus / Redis——它们依赖 Docker，而开发机未安装。改为直连阿里云上的实例：

```bash
cp .env.example .env
# 把 MYSQL_HOST / MILVUS_HOST / REDIS_HOST 改成 ECS 地址

uv sync
uv run alembic upgrade head   # 🔴 必须先做：建表只有 Alembic 这一条路径
uv run main.py                # http://127.0.0.1:8000/docs
```

> 🔴 **`alembic upgrade head` 不能省。** 本地没有 compose 的 `migrate` 服务替你做这件事，
> 而 API 启动时会**只读地**校验「目标库是 `rag_kb` + 迁移已到 head + 模型表都存在」，
> 任一不满足就**直接拒绝启动**并打印该执行什么命令。
> 这不是多余的严格：缺表时 API 其实能正常启动、`/health` 也回 200，
> 直到第一个请求打过来才 500 —— 那种状态排查起来会先去怀疑业务代码。

> ⚠️ **安全**：ECS 安全组必须把 3306 / 19530 / 6379 限制为**你自己的公网 IP /32**，
> 绝不能对 `0.0.0.0/0` 开放。换网络后记得改白名单。
> 更稳妥的做法是改用 SSH 隧道（见 PRD §11.3 R2）。

### 两个探针的分工

| 路径 | 含义 | 用途 |
|---|---|---|
| `GET /health` | **存活**：进程还在、事件循环没卡死。**不查数据库**。 | 编排系统据此决定「要不要重启」 |
| `GET /ready` | **就绪**：数据库可达 + 迁移到 head + 模型表齐全。200 / **503** | 编排系统据此决定「能不能接流量」 |

```bash
curl -s localhost:8000/health   # {"code":0,...,"data":{"status":"up",...}}
curl -s localhost:8000/ready    # 就绪 200；未就绪 503，message 里写明缺什么、该跑哪条命令
```

> `/health` 刻意**不查库**：把依赖探活塞进存活探针，会让「MySQL 抖一下」升级成
> 「容器被反复重启」——重启治不好远端数据库，只会把一次抖动放大成重启循环。

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
uv run alembic upgrade head                      # 🔴 建表：只有这一条路径
uv run main.py                                   # 本地起 API（会先只读校验 schema）
uv add <pkg>                                     # 加依赖（勿手改 pyproject 依赖段）

uv run alembic current                           # 库当前版本
uv run alembic heads                             # 仓库最新版本（两者应一致）
uv run alembic history                           # 迁移历史

# 🔴 权限码同步（只增不删，能反复跑）—— 见下方「权限码」一节
uv run python -m backend.scripts.seed_org --sync-permissions

uv run celery -A backend.tasks worker -l info    # 队列 worker
uv run celery -A backend.tasks beat   -l info    # 定时调度

docker compose up -d                             # 起全套（含一次性 migrate）
docker compose ps -a                             # 看含已退出容器在内的状态
docker compose logs -f api                       # 看日志
docker compose logs migrate                      # 迁移日志（失败时先看这个）
docker compose down                              # 停（保留数据卷）
```

> 🔴 **`seed_org.py` 不带参数跑 = `wipe` + 重灌**（清空组织与权限 6 张表，
> 连经接口建的用户和部门一起没）。**在已用起来的库上只想补权限码时，不要跑它** ——
> 用 `--sync-permissions`，它只补库里缺的权限行与角色授权，**不删任何东西**，
> 也不碰 `sys_user` / `sys_department`。

> ⚠️ **迁移在本项目一律手写**，不用 `--autogenerate`：本机连的是云端 `rag_kb`，
> 不适合让工具对一份不完全可控的库做全库 diff。`--autogenerate` 只在本地试验时用。

---

## 数据库 schema

建表由 `alembic/versions/` 下的**手写迁移**负责，共 8 条：

| 版本 | 内容 |
|---|---|
| `0001` | 组织与权限 6 表（`sys_department` / `sys_permission` / `sys_role` / `sys_user` / `sys_user_role` / `sys_role_permission`） |
| `0002` | 知识导入 4 表（`kb_document` / `kb_document_permission` / `kb_chunk` / `kb_import_task`） |
| `0003` | 问答 3 表（`qa_session` / `qa_message` / `qa_log`） |
| `0004` | `sys_audit_log` |
| `0005` | 运营看板预聚合 2 表（`stats_hourly` / `stats_daily`） |
| `0006` | FAQ 2 表（`faq_candidate` / `faq`） |
| `0007` | `knowledge_gap` + `kb_import_task.gap_id` |
| `0008` | 删除 `kb_document_permission.created_at`（修历史迁移与 ORM 的漂移） |

> 🔴 **`sys_audit_log` 是多个写操作的硬前置**：审计与业务**同事务**写入（刻意的取舍 ——
> 宁可一起失败，也不要「业务成功而留痕静默丢掉」）。因此表不存在时，**整个写操作会失败**。
> 部署顺序永远是「先迁移，再起 API」。

> 🔴 **迁移只允许指向 `rag_kb`。** `alembic/env.py` 会拒绝 `MYSQL_DB` 为其它值的运行 ——
> 那台实例上不止一个库，且 `MYSQL_DB` 是环境变量，一次复制 `.env` 的手滑就能把整套表
> 建到隔壁库上，而**迁移命令不会因此报任何错**。

---

## 当前进度

| 期次 | 范围 | 验收标准 | 状态 |
|---|---|---|---|
| 骨架 | 目录重构 + 配置层 + compose 编排 | — | ✅ 完成 |
| P1 | 认证组织 + 知识导入 + 鉴权问答 | A1-A5、A9(场景一) | ✅ 后端代码已开完（be-01…be-05） |
| P2 | 审计 + 运营看板 | A6 | ✅ 后端代码已开完（be-06 / be-07） |
| P3 | FAQ 聚类 + 缓存 + 缺口闭环 | A7、A8、A9(场景二) | ✅ 后端代码已开完（be-08 / be-09） |

> ✅ **T2 接口层已真跑过（2026-10-15），8 个脚本零 FAIL。**
> `startup` 72、`org` 68、`import` 68、`search` **121**、`audit` 102、
> `stats` 152、`faq` **195**、`gap` 220。
> 剩下的 SKIP 绝大多数是 MinerU 与 Celery worker。

> 🔴 **T3 不再是「只能在 ECS 上跑」了。** 2026-10-15 打通了两条本地依赖：
> Milvus **本来就是直连云端的**（原先 T3 写死 skip、理由「本机无 Docker」**是错的**），
> BGE 用 `backend/scripts/dev_bge_server.py` 在本地 `127.0.0.1:9997` 补上。
> 于是 `verify_search.py` 的 T3 从 **11 条写死 skip** 变成**逐项探活后真跑**：
> A4 引用卡片、A9 SSE 真流（实测 **297 帧**）、done 的五个字段，全部实测通过。
> SKIP 从 11 降到 **2**，剩两条需要专门的注入/断连脚手架。
>
> ⚠️ T3 **必须走真 socket**：T2 的 `ASGITransport` 不反映分帧，
> 「delta 逐帧到达」在它下面**恒真**，等于没测。

> ⚠️ **「代码已开完」≠「已验收通过」。** SKIP 一律**不是**通过 ——
> `verify_import.py` 的 68 项里，真正的导入链路（A2-1…A2-10）一条都没验过；
> 聚合任务真跑、beat 到点触发、清理任务真删仍需要 worker/beat。

## 权限码（前后端 + 数据的隐式契约）

字典 §5.6 冻结为 **28 个展开码**：`org:*` 13 + `kb:*` 6 + `stats:*` 2 + `faq:*` 5 + `gap:*` 2。

契约有**四处**，缺一处就出问题，而且**前后端都不报错**：
① 字典 §5.6；② `seed_org.py` 的 `PERMISSIONS` / `ROLE_PERMISSIONS`；
③ 前端常量（`src/constants/`）；④ **线上 `sys_permission` 表本身**。

> 🔴 后果是按钮对**所有人（含 `sys_admin`）静默不可见**，或接口对**所有人** 403。
> 2026-10-15 实测到过一次：库停在第 19 条，`stats:*` / `faq:*` / `gap:*` 一条都没有，
> 于是 be-06…be-09 的全部接口对所有人 403。
> **改权限码的完整动作 = 三处同动 + 跑一次 `--sync-permissions`。**

## 本地开发：把 BGE 补上

应用通过 HTTP 调 BGE（`EMBEDDING_BASE_URL`，默认 `http://127.0.0.1:9997/v1`），
**不把 torch 装进应用镜像**（PRD D17：BGE 只跑在公司 GPU 服务器上）。
但开发机上那个端口默认没人监听，症状是问答第一步就断，
而报错只写「**向量服务调用失败：网络异常**」—— 里面**没有「BGE」两个字**，
极易被误读成 LLM 的问题。

```bash
# 1) 先自证口径一致（拿库里已存的切片重算向量，与已存的比余弦；应为 1.0000）
uv run python backend/scripts/dev_bge_server.py --selftest
# 2) 起服务（另开一个终端，CPU 即可）
uv run python backend/scripts/dev_bge_server.py --serve
```

> 🔴 **换任何 BGE 实现前都必须先跑 `--selftest`。** 池化方式（BGE 用 CLS）或
> 归一化口径不同时，照样返回 768 维**但语义错位**，检索会安静地返回一堆不相关结果。
> 「服务起来了、能返回 768 维」**不是证据**。
> torch 装在独立的 `.venv-bge`，不进应用依赖。换回公司 GPU 时只改 `EMBEDDING_BASE_URL`。

> ⚠️ **`.env` 的键名写错不会报错。** pydantic-settings 对未知键静默忽略
> （`extra="ignore"`）。实测踩过：写的是 `DEEP_SEEK_KEY`，而字段是 `DEEPSEEK_API_KEY`
> ⇒ 整条被丢掉 ⇒ 问答在 LLM 那步才失败。**改完 `.env` 必须重启 API**
> —— `--reload` 只盯 `.py`，`.env` 变了不会重载。

## 上线前必办

- [ ] **T3 的剩余两层**——聚合任务真跑、beat 到点触发，需要 worker + beat
- [ ] **给测试文档配 `kb_document_permission`**——✅ 已灌（107 行），A4/A5 可演示
- [ ] **确认 ECS 规格**（PRD O2）——Milvus 单机版自身需 ~4G，整机至少 4核8G
- [ ] **确认 MinerU 官方 API 的合规结论**（PRD R1）——文档会上传至第三方站点
- [ ] 确认 GPU 服务器上 BGE 服务的部署方式与网络可达性（PRD O3 / O4）
- [ ] **给开发期 Redis 加密码并限制来源 IP**——当前实例公网可达且无密码
- [x] ~~修掉 `user_context` 客户端传参导致的越权风险（PRD R3）~~——已整体删除字段，非「用 JWT 覆盖」
