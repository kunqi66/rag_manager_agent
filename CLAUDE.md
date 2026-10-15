# CLAUDE.md

本文件是给 AI 编码助手的工程约定。**先读 [vibecoding_doc/PRD.md](vibecoding_doc/PRD.md)**——那是需求与技术设计的唯一事实来源（SSOT）；本文件只讲「怎么写」。

开发遵循 **spec 驱动**：每个功能先按 `.claude/skills/dev-spec` 生成 spec 文件（放 `vibecoding_doc/specs/`，没有则创建），
评审通过后再照 spec 写代码。元流程与门禁见 [vibecoding_doc/开发过程总纲.md](vibecoding_doc/开发过程总纲.md)，
模板见 `vibecoding_doc/spec-templates/`。

前端原型图走 `.claude/skills/pencil-draw`（Pencil MCP）——设计令牌、屏幕网格、语法坑都在里面，画图前先读。

前端代码走 `.claude/skills/dev-frontend`——从 `fe-0X` 模块概要设计 + pencil 原型生成 Vue 代码。
**必须先出 spec 再动代码**；原型与设计对不上时以**设计为准**（原型里有≠需求里有），
处理原则与登录页样板见该 skill 的 `登录页实例.md`。

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
| P1 | 认证组织 + 知识导入 + 鉴权问答 | A1-A5、A9(场景一) | ✅ **代码全开完**（be-01 / be-02 / be-03 / be-04 / be-05） |
| P2 | 审计 + 运营看板 | A6 | ✅ **代码全开完**（be-06 / be-07），**T2 已验**，T3 待 worker/beat |
| P3 | FAQ 聚类 + 缓存 + 缺口闭环 | A7、A8、A9(场景二) | ✅ **后端代码全开完**（be-08 / be-09），**T2 已验**，A7 缓存链路 **T3 已实测**，其余待 worker |
| 环境收尾 | 迁移到 head + 权限种子 + T2 接口层 | — | ✅ **已完成**（2026-10-15），8 个脚本零 FAIL |

**现状（2026-10-15）**：**迁移已到 head `0008`，库表齐全，T2 接口层真跑过。**
`backend/core/`（settings / logging / exceptions / response / db）可用，
`backend/tasks/` 的 Celery 实例已注册全部任务与 beat 计划，
`docker-compose.yml` 编排 9 个服务 + 一个一次性的 `migrate` 服务。
`backend/models/` 共 17 张表，全部由 `alembic/versions/0001…0008` 建出。
`backend/scripts/seed_org.py` 灌入 5 部门 / 3 角色 / 20 用户 / **28 权限码**
（权限码补灌走 `--sync-permissions`，只增不删 —— 见「权限码」块）。

**验收总账：8 个脚本、零 FAIL、全部 `EXIT=0`** ——
`startup` 72、`org` 68、`import` 68、`search` 99、`audit` 102、`stats` 151、`faq` 181、`gap` 220。
剩余 SKIP 全是 Milvus / BGE / DeepSeek / MinerU / Celery worker（本机无 Docker）。

**be-01 认证已实现，验收 36/36 通过**（`backend/scripts/verify_auth.py`，可直接跑）：
`core/security.py`（JWT 签发校验 + Argon2；全项目**唯一**允许 `import jwt` / `pwdlib` 的文件）、
`core/deps.py`（`get_current_user` / `require_permission`）、`schemas/auth.py`、
`api/auth_router.py`（login / refresh / logout / me 四个接口，已挂进 `main.py`）、
`service/auth_service.py`、`service/session_store.py`（`rag:auth:*` 四个键的**唯一**定义处）、
`clients/redis_client.py`。
**be-02 组织管理已实现，验收 68/68 通过**（`backend/scripts/verify_org.py`，可直接跑）：
`schemas/org.py`（13 个 DTO）、`service/org_service.py`、`api/org_router.py`（**15 个端点**，已挂进 `main.py`）；
`core/exceptions.py` 新增 `ConflictError` + `ParamError`，`core/settings.py` 新增两个组织域配置。
spec 见 `vibecoding_doc/specs/P1-08-be-org.md`（状态：已实现）。
be-03 有代码（`core/security.py` / `core/deps.py` / `core/db.py` / `schemas/auth.py`）且**已补 spec**
（`P1-09-be-security-deps.md`，回填+收口），R3 第二步（删 `user_context`）已随 be-05 完成 —— 见下。

**be-04 知识导入已实现，验收 50/50 通过**（`backend/scripts/verify_import.py`）：
`models/` 新增 4 张表（`kb_document` / `kb_document_permission` / `kb_chunk` / `kb_import_task`，
迁移 `0002` 已应用到云端 rag_kb）、`schemas/kb_import.py`（18 个 DTO）、
`clients/` 补齐 **mineru / embedding / milvus** 三个封装、
`engines/import_engine/`（`chunking` 切片 / `tokenizer` BGE 口径计数 / `lifecycle` 台账与权限 /
`pipeline` worker 侧流水线）、`tasks/import_tasks.py`（2 个任务，含自算退避）、
`api/import_router.py`（**12 个端点**，全部挂 `require_permission`）。
spec 见 `vibecoding_doc/specs/P1-10-be-kb-import.md`（状态：已实现）。

> ⚠️ **verification 的分层**（`verify_import.py` 自述）：T1 离线 + T1.5 引擎层共 50 项全绿；
> **T2（接口层）需 Redis、T3（链路）需 Milvus+worker**。
> ✅ **T2 已于 2026-10-15 真跑过**（MySQL/Redis 均可达）；剩余缺口在 T3（本机无 Docker ⇒ 起不了 Milvus）。
> T2/T3 的 SKIP 项在脚本里逐条列明依赖，**不要把它当成「已验收通过」**。

**be-05 鉴权检索问答已实现，验收 89/89 通过**（`backend/scripts/verify_search.py`）：
`models/` 新增 3 张表（`qa_session` / `qa_message` / `qa_log`，迁移 `0003` **已应用**）、
`schemas/query.py`（DTO 全集 + `SseEvent.make` 的**事件名/载荷配对校验**）、
`clients/llm_client.py`（DeepSeek 流式，含 `stream_options.include_usage`，**无重试**）、
`engines/search_engine/`（`acl` 前置表达式 + **后置兜底复核** / `retrieval` 四步检索编排 /
`qa` Prompt 与 SSE 事件流 / `session` 三表落库）、
`api/query_router.py`（**7 个端点**，改造+补 2 个）。
顺带补齐 `clients/milvus_client.py` 的 `collection_ready` / `hybrid_search` / `dense_search`
（原文件只有写入侧 —— 我 spec 里曾把这一行误标成「已实现」，见 P1-11 §2 的更正块）。

> 🔴 **安全红线 1 至此全部收尾**：`query_router.py` 的 `class UserContext` 与
> `SearchRequest.user_context` / `AskRequest.user_context` 已**整体删除**（PRD §11.3 R3）。
> 不是「用 JWT 覆盖」，是删除 —— 覆盖的话字段还在，下一个人照着旧文档写客户端就又把口子开回来。
> 守门用例：`verify_search.py` 的 T1-40a / T1-40b。

> ⚠️ **verification 的分层**（`verify_search.py` 自述）：T1 离线（ACL 表达式与注入剔除 /
> 探测缓存键 / Prompt 组装 / SSE 帧格式 / 迁移与模型列一致）+ T1.5 **假会话**（用假 `AsyncSession`
> 驱动 `session.py`：归属校验、历史取窗、删除语义）共 **89 项全绿**；
> **T2（接口层）需 MySQL+Redis、T3（链路）需 Milvus+BGE+DeepSeek**。
> ✅ **T2 已于 2026-10-15 真跑过**；剩余缺口在 T3（本机无 Docker ⇒ 起不了 Milvus）。
> 同样**不要**把 SKIP 当成通过。

**be-06 审计已实现，验收 85/85 通过**（`backend/scripts/verify_audit.py`）：
`models/audit_log.py`（`sys_audit_log`，迁移 `0004` **已应用**）、
`schemas/audit.py`、`service/audit_service.py`（**两条留痕路径** `record()` 同事务 /
`record_detached()` 自开事务 + `record_read()` 带去重 + `count_action()`）、
`api/stats_router.py`（be-07 的文件，be-06 只注册 1 个端点 `GET /api/stats/audit-logs`）、
`tasks/audit_tasks.py`（保留期分批清理，beat 每天 03:17）。
`core/db.py` 顺带抽出 **`fresh_session()`** —— 任务侧取 session 的**唯一**入口
（`import_tasks._with_session` 已改为委托它，消掉一处重复实现）。
spec 见 `vibecoding_doc/specs/P2-12-be-audit.md`（状态：已实现）。

> 🔴 **`record()` 与 `record_detached()` 的分工是本模块最容易用错的地方**：
> `record()` **自己不 commit**，靠调用方提交；可 `auth.login_failed`（失败分支直接 `raise`）、
> `auth.logout`（签名里连 `db` 都没有）、`document.read`（纯读路径）**根本没有调用方事务** ——
> 在那里用 `record()`，行会被 `add` 进 session 然后随 session 关闭**静默蒸发**，
> 审计看起来「代码写了」而库里一条都没有。判据是**这条路径有没有业务事务可搭**。
> 守门用例：`verify_audit.py` 的 T1.5-13 / T1.5-15 / T1.5-16。

> 🔴 **调用点清单是本模块的真正交付物**（spec §5.4）。`audit_service` 本身只是薄封装，
> 真正的风险是**某个写操作忘了调它** —— 那种缺陷在运行时完全无声（接口照回 200，
> 只是审计表少一行，没人会去数）。
> 因此 `verify_audit.py` 的 T1-40…T1-42 是**静态核查**：扫全仓源码，
> 断言每个应有调用点的动作至少被引用一次（**实测落地 27 处** —— 表里 28 行减去一行
> 条件不成立的 `reset_user_password`：「若有」的答案是**没有**，全项目无密码重置端点）。
>
> 🔴 **这条反向断言曾经自己变成缺陷（2026-10-15 抓到）。** 它原先写的是
> 「be-08/be-09 的 5 个动作此刻**不该**有调用点」—— 在 be-06 交付时那是**对的**
> （那两个模块还没开工）。可 be-08/be-09 落地后没人回来改它，于是它
> **一直在替真正的漏审辩护**：`FAQ_PUBLISH` / `FAQ_REJECT` / `FAQ_CACHE_TOGGLE`
> 三个动作在 `audit_service` 里定义了、`_DETAIL_WHITELIST` 也配好了，
> **却一个调用点都没有** —— 发布 FAQ、驳回候选、开关缓存这三件写操作
> **不留任何痕**，而用例是绿的。
> **修法**：跟着实现的推进**同步翻转**断言方向 —— 现在是 `T1-41b`，
> 把这 5 个动作逐个点名要求「必须有调用点」，并逐个断言行真的落进了 `session.added`
> （含 `user_id` / `target_id`）。
>
> ⚠️ **一般化**：「此刻不该有」这类断言**自带保质期**，而它过期时**不会变红，
> 只会失效**（继续绿、继续不守任何东西）。凡写下这种断言，就在同一行注明
> 「哪个模块落地后必须翻转它」，否则它一定会变成假绿。

> 🔴 **be-04 的「运行时探针」已按期收口**。`lifecycle._record_read` 原先写成
> 「`audit_service` 有就调、没有就跳过并告警」（be-06 尚未落地时的过渡手法），
> 它自己的 docstring 就写明「一旦 be-06 落地，`except ImportError` 分支就变成**静默漏审计**」。
> 现在直连 `record_read`：**调用关系硬、运行时失败软** —— 函数被改名会在导入期炸出来，
> 而写库失败仍被 `record_detached` 吞掉（读路径是热路径，不能因留痕失败阻断看文档）。
> 守门用例：T1-44。

> ⚠️ **verification 的分层**（`verify_audit.py` 自述）：T1 离线 + T1.5 假会话共 **85 项全绿**；
> **T2（接口层）需 MySQL+Redis、T3（链路）需 worker+beat**。
> ✅ **T2 已于 2026-10-15 真跑过**；剩余缺口在 T3（worker/beat 未起）。
> T2 里最重要的一条是 **T2-4**：断言「刚才那次登录**自己**留下了一条 `auth.login`」——
> 端到端自证留痕真的落库了。

> 🔴 **迁移 `0004` 是 be-06 的硬前置**。`record()` 与 `retry_import_task` 的
> `count_action()` 都会写/读 `sys_audit_log`：表不存在时**整个业务写操作都会失败**。
> 这是刻意的（审计与业务**同事务**，宁可一起失败也不要「业务成功而留痕丢了」），
> 但要知道它的存在 —— 部署顺序是「先 `alembic upgrade head`，再起 API」。

`engines/` 现有 `import_engine` + `search_engine` + **`data_mining_engine`**；
`clients/` 已补齐 **llm**；`config/` 未建。
`data_mining_router` 的 **8 个端点全部实现**（be-08 六个 + be-09 两个），**已无 `TODO` 壳子**。

**be-07 运营看板已实现，验收 122/122 通过**（`backend/scripts/verify_stats.py`）：
`models/stats.py`（`stats_hourly` / `stats_daily` 两表，迁移 `0005` **已应用**）、
`schemas/stats.py`、`service/stats_service.py`（聚合 + 5 个查询 + `assert_admin_scope` 角色硬闸）、
`tasks/stats_tasks.py`（改写为薄壳：`aggregate_hourly` / `backfill_range` / `purge_stats`）、
`api/stats_router.py`（追加 **5 个端点**，路径 `/api/stats/dashboard/*`，同文件同 router）、
`beat_schedule` 新增 `purge-stats-daily` = `04:23`（字典 §9 #116）。
spec 见 `vibecoding_doc/specs/P2-13-be-stats.md`（状态：已实现）。

> 🔴 **路径必须带 `dashboard` 段**（`/api/stats/dashboard/overview`）。
> 本模块首版**漏了这一段**，写成 `/api/stats/overview` —— 后果是前端 5 张图
> **全部 404**，而接口侧"工作正常"。三份独立来源一致要求带段：PRD §8.5、字典 §5.6、
> `frontend/src/api/stats.ts`（fe-05 已实现的真正消费方）；本模块自己的 spec §4.1 也写对了，
> **错的是代码**。已修正。
> **没拦住它的原因值得记**：① 验收脚本那条「路由已注册」断言的 `expected`
> 是**照实现抄的** —— 自证式断言只能证明「代码等于代码」，永远绿；
> ② T2 接口层的路径断言因 MySQL 不可达一直 SKIP，从没真跑过。
> → 修法不只是改路径：`verify_stats.py` 的 **T1-67a** 现在**从前端源码正则抠出它真正请求的
> URL** 再断言后端都注册了（并反向查孤儿路径）。实现与消费方成了两个独立来源。
> 该用例已做**变异测试**（把路径改回错的，T1-67a / T1-67a3 / T1-67 三条同时变红）。

> 🔴 **预聚合的语义是「重算覆盖」，不是「累加」**（`INSERT ... ON DUPLICATE KEY UPDATE`）。
> 三个理由：① 迟到行会跨桶（`created_at` 落在上一小时），累加会让结果**取决于任务几时跑**，
> 即不可复现；② 重算天然幂等，重试与回填都不需要额外机制；③ 回填就是**同一条代码路径**
> —— 不存在第二份「修正用」实现可能与定时版分歧。守门用例：T1-50（静态）与
> **T2-24（端到端：同一窗口跑两次，逐值相同）**。
> 🔴 **若哪天有人把 `upsert_buckets` 改成 `pv = pv + ...`，T2-24 会看到 pv 翻倍** ——
> 这是本模块唯一能拦住它的地方。

> 🔴 **TOP 榜与直方图必须「逐桶」算**。查询侧合并多桶用的是**求和**，
> 所以把「窗口级 TOP」写进每个桶，3 小时窗口会把计数**乘 3**，而数字看起来
> 完全正常（只会觉得"大家问得挺多"）。**实现期真的踩到了**：首稿就是这么写的。
> 修法是 `aggregate_range` 用**一趟明细流式查询**逐桶算 histogram / top_questions /
> top_documents（顺手省掉两趟 `GROUP BY`）。守门用例：T1.5-31~33。

> 🔴 **桶键必须按 `grain` 取整**（`_normalize_bucket`）。明细趟的 `created_at` 带分秒，
> 不取整就与聚合趟的整点键对不上 ⇒ **直方图和 TOP 榜恒空，而 `pv`/`uv` 全对**。
> 症状是「数字都对，就是图表没数据」—— 两趟各自都"没报错"。
> 另一个同源陷阱：`stats_daily` 必须转 `date` 再比（拿 `datetime` 比 `DATE` 列时
> `'2026-10-14' >= '2026-10-14 09:30:00'` 是 **False**，当天那桶被静默漏掉；
> 窗口起点恰为 00:00:00 时又变 True ⇒ 看起来像「偶发」）。
> 守门用例：T1-13/14/62。

> ⚠️ **`uv` 跨桶不可加，`uv_is_exact` 是给前端的免责声明**。桶内去重是「人数」，
> 多桶求和是「人次」。响应里只回一个数字，前端无从判断能不能加「人」字 ——
> 而把「人次」当「人数」展示是一个**必然发生**的误读。
> 🔴 另一个易错的量：**均价必须按 `pv` 加权**，不是各桶 `latency_avg_ms` 的算术平均
> （1 次提问的桶与 5000 次的桶等权，10 个桶就能把均值拉偏几倍）。守门用例 T1.5-07。

> 🔴 **迁移 `0005` 是 be-07 的硬前置**，与 `0004` 同理：表不存在时 5 个看板端点
> 一律 500，且 `aggregate_hourly` 每 10 分钟失败一次。
> 部署顺序仍是「先 `alembic upgrade head`，再起 API」。
> 新表**无外键**（聚合表独立于业务表，业务表删行不该被它拖住），
> 但**两表指标列完全同构** —— 不同构的后果是「切到 30 天口径（走日表）某个指标变 0」
> 而小时口径正常。守门用例：T1-42。

**be-08 FAQ 挖掘已实现，验收 157/157 通过**（`backend/scripts/verify_faq.py`）：
`models/faq.py`（`faq_candidate` / `faq` 两表，迁移 `0006` **已应用**）、
`schemas/mining.py`（14 个 DTO，与 `frontend/src/types/governance.ts` **逐字段对账**）、
`engines/data_mining_engine/`（`similarity` 纯计算共用件 / `faq_mining` 聚类与审核流转）、
`faq/faq_cache.py`（进程内 numpy 索引）、`service/faq_service.py`（装载 + 订阅刷新 + 定时重载）、
`api/data_mining_router.py`（**6 个端点已实现**，另 3 个 `gaps` 端点为 be-09 的 `TODO`）、
`clients/llm_client.py` 补 `chat()`（非流式，给聚类归并问题用）。
spec 见 `vibecoding_doc/specs/P3-14-be-faq-mining.md`（状态：已实现）。

> ⚠️ **verification 的分层**（`verify_faq.py` 自述）：T1 离线 + T1.5 **假会话**共 **157 项全绿**；
> **T2（接口层）需 MySQL+Redis、T3（链路）需 Milvus+BGE+DeepSeek+worker**。
> ✅ **T2 已于 2026-10-15 真跑过**；剩余缺口在 T3（本机无 Docker ⇒ 起不了 Milvus/worker）。
> ⚠️ T2 这次**连登录都没过去**（HTTP 502）—— 云端 MySQL 与开发期 Redis 都不可达。
> 同样**不要**把 SKIP 当成通过。

> 🔴 **be-05 的 FAQ 降级桩已收口，问答链路真的接上缓存了**。be-05 当时只做到
> 「观测到命中」，**没有使用**命中结果 —— 也就是说 A7 的「加速应答」当时**永远过不了**，
> 而代码看起来"实现了"。现在 `qa.py` 在 `faq_hit` 为真时**直接返回标准答案、
> 不检索、不调 LLM**，`token_in = token_out = 0`、`llm_called = false`。
> 三条容易只做一半的配套（漏掉任何一条都会把修复变成新 bug）：
> ① `citations` **照发空数组**（不是不发这个事件 —— 不发会让前端把上一轮的引用卡片留在这一轮）；
> ② **不发 `blocked` 事件**（FAQ 没检索，谈不上受限，发了前端会渲染出「有 0 篇不可见」的提示条）；
> ③ **命中也要落 `qa_log`**（不落的话，已发布 FAQ 的问法会永远留在挖掘窗口里，
> 每轮聚类都把它当新候选重新提一遍）。守门用例：T1 的「be-05 回填」6 条。

> 🔴 **启用缓存必须能立刻生效，且不能靠「重启」这条退路**：`PUT /faq/{id}/cache`
> 在本进程 `upsert` / `remove` 之后**发 `PUBLISH faq:changed`**，其它进程订阅后增量更新；
> 另有 `FAQ_CACHE_RELOAD_INTERVAL_SECONDS`（300 s）的全量重载兜底 ——
> 断订阅、Redis 抖动、消息丢失都靠它自愈。
> ⚠️ 但**兜底周期就是最坏情况下的生效延迟**：把 300 调大等于把「实时」降级成「五分钟」。
> 守门用例：T3-5（多进程一致性，API 侧发布、worker 侧 1 s 内命中）—— **未跑**。

> 🔴 **`FaqCache.remove` 必须重建行号表**，不能只 `pop` 掉那个键。
> 删中间一行后，剩下的键仍指着旧行号，下一次 `upsert` 会写到**别人**那一行或长出重复行 ——
> 症状是「改了 A 的缓存开关，B 的答案变了」，两边都不报错。
> 这条用例已做**变异测试**（用一个「只 pop 不重建」的子类跑同一段断言，
> 明确看到它变红；且断言被 `try/except` 包住，回归时报 FAIL 而不是把脚本炸停）。
> 守门用例：T1 的 `remove 后不变式`（白盒断言 `_index` 的值恰是 `0..n-1` 的排列）。

> ⚠️ **be-08 的 `related_doc_ids` 存的是 `document_id`，不是 `chunk_id`**。
> 簇内成员是 `qa_log.allowed_ids`（**chunk id**），必须反查 `kb_chunk` 换成 document id ——
> 存错的话标题**永远查不到**，而列表"有值"看起来完全正常（与 be-07 的 #129 同款静默失效）。
> 反查用**一条 `IN`** 而不是 N+1。守门用例：T1.5 的两条 `resolve_document_ids`。

> ⚠️ **be-08 的验证脚本自己踩过 4 次「用例写错、代码是对的」**，都记进脚本注释了，
> 因为这类错最容易被误读成「实现有 bug」而把对的代码改坏：
> ① 拿池内的向量去测 `nearest_existing`（它当然自命中 1.0）⇒「低于阈值返回 None」永远红；
> ② 用「base + 两个极近扰动」测 `representative_index`，三点与质心夹角几乎相同，
>    argmax 由浮点噪声决定 ⇒ 时红时绿；
> ③ 用 `@router\.(post|put)\((.*?)async def` 切写端点，只截到装饰器、
>    而 `require_permission` 在**函数签名**里（`async def` 之后）⇒ 三条全判失败；
> ④ 按第一个 `]` 切 `pyproject` 依赖列表，而 `pwdlib[argon2]` 自带方括号 ⇒
>    只读出 3 个依赖，断言却"正常工作"。
> **教训**：断言要落在**能区分对错的具体值**上；「没报错」「长度对上了」这类断言
> 会跟着错误的实现一起绿。

**be-09 缺口闭环已实现，验收 212/212 通过**（`backend/scripts/verify_gap.py`）：
`models/knowledge_gap.py`（`knowledge_gap` 表，迁移 `0007` **已应用**）、
`models/kb_import_task.py` 补 **`gap_id`** 列（迁移 `0007` 同批，销字典 §9 #64）、
`schemas/mining.py` 补 `GapStatus` 别名 + `KnowledgeGapItemDTO` / `GapQuery` /
`GapTaskResult` / `GapAggregateResultDTO`、
`engines/data_mining_engine/gap.py`（`is_miss` 判定 / `aggregate_gaps` 聚合 /
`_auto_close` 自动收口 / `convert_gap_to_task` 转建编排 / `list_gaps`）、
`tasks/mining_tasks.py` 的 `aggregate_knowledge_gaps`（原为空壳，
**beat 每 10 分钟**，销字典 §9 #63）、`api/data_mining_router.py` 的 2 个 `gaps` 端点。
`clients/llm_client.py` 的 `chat()` 顺带**加了 `timeout_seconds` 形参**（见下）。
spec 见 `vibecoding_doc/specs/P3-15-be-gap-closure.md`（状态：已实现）。

> 🔴 **「两处口径必须成对地想」是本模块的核心**：`is_miss` 把 `faq_hit=True` **短路排除**
> （FAQ 答上的问题恰恰最不可能是缺口），而 `_auto_close` 把 `faq_hit=True` **当成最强的
> 闭环证据**（补料后这条问题被沉淀成 FAQ 了）。同一个字段，一处排除一处指望 ——
> 只顺着一边写，另一边必然漏，且**两处都不报错**。
> 另外两个反例：`allowed_ids` 空而 `recalled_ids` 非空 = **缺权限不是缺知识**
> （计 `skipped_by_acl`，不入池）；而 `top_score` 只统计**准入集合**，
> 所以它单独分不开「低分但准入」与「高分但被挡」。守门用例：T1 的 `is_miss` 四分支与两条反例。

> 🔴 **`is_miss` 的签名被实现期推翻过**。冻结设计里是 **2 个入参**（`top_score, allowed_ids`）——
> 那个形状**表达不了上面两条反例**。现在是 4 个 **keyword-only** 入参
> （`faq_hit` / `recalled_ids` / `allowed_ids` / `top_score`），
> T1 有一条**签名断言**（arity + `KEYWORD_ONLY`）守它。
> 4 个 bool/list 位置传参必错且不会报错，keyword-only 是这里唯一的安全形状。

> 🔴 **聚合的幂等靠缺口自己的 `last_seen_at` 当水位线**，不引入 Redis 键。
> beat 每 10 分钟 × 回溯窗口 3 小时 ⇒ 没有水位线时同一条 `qa_log` 会被累加 **18 次**，
> `frequency` 虚高一个数量级 —— 而「虚高」只表现为「运营觉得这问题好热门」，不报任何错。
> 另两处**同样静默**的实现细节：
> ① 未命中行的 SQL **必须升序**（反了会让较早那条碰到较晚那条刚抬高的水位线，**少计**）；
> ② 新建的缺口**必须立刻进内存匹配池**（否则同窗口内第 2 条同问题会再建一条，
> 结论是「同一问题两条缺口、各 `frequency=1`」）。守门用例：T1.5 的幂等 / 顺序 / 匹配池三条。

> 🔴 **自动关闭的扫描下界不能用聚合窗口的 `since`**，要用 `min(gap.last_seen_at)`。
> 管理员可能隔半天才处理缺口，那条「命中提问」落在更早的窗口里，之后每一轮又都扫不到它
> ⇒ 这条缺口**永远关不上**，且没有任何报错。守门用例：`_auto_close` 的**签名里没有 `since`**。

> ⚠️ **`convert_gap_to_task` 与 be-04 的 `create_supplement_task` 不是同一事务**。
> 后者自己 `commit`（be-04 的既有契约），所以「任务建好了、缺口还是 `open`」是可能的中间态。
> 补偿是 `gap_id` 落库 + `_find_open_task`：管理员再点一次会**复用**那条任务并补上 `tasked`，
> 而不是建出第二条（一条缺口补两次料 = 两份重复文档）。
> spec 原文曾写「同事务」，**是错的**，实现期已更正（P3-15 §5.5）。

> 🔴 **实现期抓到两个「定义了却没接上」的缺陷**，两者都在导入/启动时**不报错**：
> ① `gap.py` 写 `audit_service.TargetType.GAP`，而该模块导出的名字是 **`AuditTargetType`**
> （`AttributeError` —— 第一次跑 T1.5 就炸了；**这正是「假会话层」存在的意义**：
> 那两条审计调用在 T2 之前一次都没被执行过）；
> ② `GAP_LLM_SUGGEST_TIMEOUT_SECONDS` 定义了却**没传给** `llm_client.chat()`
> ⇒ 只表现为「偶尔一整轮聚合慢了 60 秒」，没人能解释。
> 修法：给 `chat()` 加 `timeout_seconds` 形参（**不写成 `or 默认值`**，
> 否则传 `0` 会被当成没传，而 `httpx.Timeout(read=0)` 是合法的「立即超时」）。

> ⚠️ **verification 的分层**（`verify_gap.py` 自述）：T1 离线 + T1.5 **假会话**共 **212 项全绿**；
> **T2（接口层）需 MySQL+Redis、T3（链路）需 Milvus+BGE+DeepSeek+worker**。
> ✅ **T2 已于 2026-10-15 真跑过**；剩余缺口在 T3（本机无 Docker ⇒ 起不了 Milvus/worker）。
> T2 这次同样**连登录都没过去**（HTTP 502）。
> 脚本在 T2 未执行时**退出码非 0** 并打印「本次结果不构成完整验收」——
> 不要只看那行 `212/212` 就当成验完了。

> 📌 **`verify_gap.py` 自己踩的两个「用例写错、代码是对的」**（与 be-08 那四条同类，补记）：
> ⑤ 断言 `le=200` 在 router 里 —— 而壳子的 `Query(..., le=200)` 已被 `GapQuery` 取代，
>    上限迁到 DTO 的 `field_validator`，那个子串**实现后就不存在**；
> ⑥ 在 `list_gaps` 的 `statements[0]` 上断言 `ORDER BY` —— 而 `[0]` 是 **count 语句**。
> **教训同 be-08**：断言要落在能区分对错的具体值上，还要**顺带断言「扫描真的扫到了东西」**
> （本脚本的「扫到 8 个端点」「解析到 8 处 `logger` 调用」就是干这个的 ——
> 没有它们，「没有违规」与「扫描器坏了」是同一个绿灯）。

> ✅ **权限码已拉齐（2026-10-15 复核）**：字典 §5.6 冻结为 **28 个展开码**
> （`org:*` 13 + `kb:*` 6 + `stats:*` 2 + `faq:*` 5 + `gap:*` 2；
> 原 `org:user:*` 等通配码**作废**，§9 #8 / #34 已定）。
> 字典 §5.6 表下那句「连同 `org:*` 的 13 条，本节共 **28** 条」是权威口径 ——
> 早先按「13 个」写的断言（`verify_org` 的 H5）在库里种满 28 条之后反而变红，
> **红的原因是正确的数据**。已改成逐字手抄的 28 条。
>
> 🔴 **权限码是「四处」契约，不是三处 —— 而且第四处不会因为改前两处而自动跟上。**
> ① 字典 §5.6；② `seed_org.py` 的 `PERMISSIONS` / `ROLE_PERMISSIONS`；
> ③ 前端常量（`system.ts` / `kb.ts` / `governance.ts`）；
> ④ **线上 `sys_permission` 表本身**。
> 后果是按钮对**所有人（含 `sys_admin`）静默不可见**、或接口对所有人 403，
> 且前后端都不报错（上一轮就是这么错的：前端 13 个码、库里 7 个、只重合 4 个）。
>
> 🔴 **实测（2026-10-15）：库停在第 19 条**（13 `org:*` + 6 `kb:*`），
> `perm_20…perm_28`（`stats:*` / `faq:*` / `gap:*`）**一条都不在库里**。
> 于是 be-06…be-09 的**全部**接口对**所有人** 403 —— 而 T2 接口层此前因缺表
> 一直 SKIP，这个洞**从落地到补表之前没有任何一条用例碰得到它**。
>
> 补的口子是 **`uv run python -m backend.scripts.seed_org --sync-permissions`**
> （只增不删，不动 `sys_user` / `sys_department` / 密码）。
> 之所以要单开这条路径：`seed_org.py` 的主流程是 **wipe + 重灌**（`_wipe` 清空 6 张表，
> 连用户和部门一起没），于是「只想补 9 条码」的代价变成「抹掉所有经接口建的用户」——
> 没人愿意在已用起来的库上跑它，**权限码就一直是旧的**。
> 改权限码的完整动作现在是：**三处同动 + 跑一次 `--sync-permissions`**。

---

## 下一步（P3）

**P1（be-01…be-05）、P2（be-06 / be-07）、P3（be-08 / be-09）后端代码已全部开完。**

**be-09 是最后一个后端模块。后面没有新模块了 —— 只剩收尾与环境验收**（见下）。

### ✅ 前端契约回核已完成（2026-10-14）

后端全开后，把前端攒下的「后端还没实现，先按设计稿推定」的注释**逐条回核**。
**不是「确认没问题」，而是抓出两个真 bug** —— 都属于「前端读了一个后端从来不发的字段」，
且**两侧都不报错**（详见「已知坑 · 前后端契约」）：

1. 🔴 **上传主路径整条静默哑掉**（A2）：`normalizeUploadResult` 猜的三种形状
   后端一个都不沾 ⇒ 恒返回空 ⇒ `onUpload` 判「没拿到任务」⇒ **进度轮询从不启动**。
   上传其实成功了，界面却与「上传失败」无从区分。
   已改成与 `UploadAccepted` 同构，`acceptUpload` 转 `async`（回执里没有任务对象，
   必须再发一次 GET）。
2. 🔴 **用户表角色列永远空白**：`UserItem.roles` 后端从不发（发的是 `role_ids`）⇒
   角色列恒「—」，分配角色弹窗回显恒空、警示条从第一天挂到现在（**数据没丢，
   功能没生效**）。已改类型 + 新增 `org.roleNames` getter；
   同轮删掉三维皆空的死字段 `effective_dept_ids`。

顺带把两处**当年预留、后端后来补上**的能力接回去：延时图的 P95（`latency.p95_ms`）、
环形图的第三段（`faq_hit_count` / `blocked_count`）—— 原注释写着「后端补齐后只改派生逻辑，
组件不动」，**这个预言成立，组件一行没改**。

清掉 **~18 处**过期注释，并把 8 份 spec 的头部状态拉回真实值
（`P1-07` / `P1-09` → 已实现；5 份 fe spec → **实现中**，未走评审/冻结门禁故不写「已实现」）。

`npx vue-tsc --noEmit` 与 `npm run build` 均 **EXIT=0**。
🔴 **但这只证明类型与编译，不证明接口通** —— 当时 T2 一次都没跑过，
最后一句写的是「同类的第三个 bug 很可能还在」。
✅ **2026-10-15 这个预言兑现了**：T2 真跑起来后，抓出的不是第三个而是
**1 个数据缺陷（权限种子停在 19 条 ⇒ be-06…be-09 全 403）+ 2 个代码缺陷
（3 个审计调用点没接、`page_size` 口径分歧）+ 8 处用例缺陷**。详见下方「验收脚本自身」。

**be-09 开工时的四条已知缺口，逐条销账**：

1. ✅ **`allowed_ids` 判定口径** —— 冻结为「后置复核准入」，`is_miss` 据此扩成 4 参
   （见 be-09 段落）。销字典 §9 #19 / #142。
2. ✅ **`aggregate_knowledge_gaps` 不在 beat 里** —— 已加 `aggregate-knowledge-gaps`（`*/10`）。
   销字典 §9 #63 / #65；`gap:task:create` 与 `gap:list` 权限码已进种子。销 §9 #68。
3. ✅ **`kb_import_task` 缺 `gap_id`** —— 迁移 `0007` 同批补上，`create_supplement_task`
   不再 `del gap_id`。销字典 §9 #64。
4. ✅ `GAP_EMPTY_TEXT` 里那句「beat 缺条目」的注释已随实现移除。

### ✅ 环境收尾已完成（2026-10-15）—— 留作复盘

0. ✅ **迁移已到 head `0008`**（`0001…0008` 全部应用，重复 `upgrade head` 已是 no-op）。
   原先的「五重硬前置」全部解除：qa 三表 / `sys_audit_log` / stats 两表 /
   faq 两表 / `knowledge_gap` + `kb_import_task.gap_id` 都在库里了。
   `0008` 是把 `kb_document_permission.created_at` 删掉的补丁迁移
   （字典与 ORM 都没有这个字段，但 `0002` 建过它，且 `0002` 已在云端跑过
   ⇒ **不改历史迁移，另发一条**）。
   Alembic 是**唯一 schema writer** —— `main.py` 的 lifespan 里**没有** `create_all()`，
   compose 里也加了独立的 `migrate` 一次性服务（`restart: "no"`，
   API/worker/beat 都 `depends_on: service_completed_successfully`），
   避免本地启动、热重载、多副本、worker/beat 并发改库。
   `/health` 是存活探针，`/ready` 才是「库连得上 + schema 不落后」的就绪探针（503 时明确说缺什么）。

0.5 ✅ **T2 接口层跑起来了，全套 8 个脚本零 FAIL**（`EXIT=0`）：

   | 脚本 | 结果 | 剩余 SKIP |
   |---|---|---|
   | `verify_startup.py` | 72/72 | 0 |
   | `verify_org.py` | 68/68 | 0 |
   | `verify_import.py` | 77/77（含 T2） | 10 |
   | `verify_search.py` | **121/121** | **2** |
   | `verify_audit.py` | 102/102 | 3 |
   | `verify_stats.py` | 152/152 | 4 |
   | `verify_faq.py` | **195/195** | 6 |
   | `verify_gap.py` | 220/220 | 7 |

   剩余 SKIP 绝大多数是 MinerU / Celery worker。**Milvus 与 BGE 这两条已不再是缺口**：
   Milvus 直连云端、BGE 用 `dev_bge_server.py` 在本地补上。
   **但要分清**：`verify_import` 的 68 项里 T2 只覆盖了不需要 worker 的部分
   （上传受理、越权、软删、详情），**A2-1…A2-10 的真正导入链路仍未验**。

0.5b 🔴 **T3 从这里开始真的跑起来了（2026-10-15 续）。**
   `verify_search.py` 的 T3 原先是 **11 条写死的 `skip()`**，理由写「本机无 Docker」
   —— 而 Milvus 是**直连云端的**，**理由本身就是错的**。写死的 skip 理由
   不会随环境改善而更新，只会一直骗人（与「『此刻不该有』的断言自带保质期」同源）。
   现在改成**逐项探活**：`/health` 探 API、`embedding_base_url` 探 BGE，
   探到才跑，探不到时 skip 里**写清楚是谁不可达**。SKIP 从 11 降到 **2**，
   剩下两条确实需要专门的注入/断连脚手架（A5-2 脏 ACL 注入、A9-4 中途 abort）。
   T3 **必须走真 socket** —— T2 的 `ASGITransport` 不反映分帧，
   A9-1「delta 逐帧到达」在它下面**恒真**，等于没测。实测 **297 帧**才是真流。

   > ⚠️ 判「服务在不在」要判 `status_code == 200`，**不是 `< 500`**。
   > 我第一版写的 `< 500`：`embedding_base_url` 带 `/v1` 而健康检查在根路径
   > `/health`，于是探的是 `/v1/health` → **404 → 判「BGE 在」**。
   > 它碰巧是对的，但换个场景就是假的（端口上蹲着任何别的 HTTP 服务都会让探活通过）。
   > **探活必须是「我要的那个东西答的」，不是「有人答应」。**

   > 🔴 **同一批「写死的 skip 理由」在别的脚本里也有一份，且有一条是彻底的假话。**
   > `verify_import.py` 的 A2-5/6 与 A3-6 原写「🔴 本机无 Docker → 跑不了 Milvus」——
   > **Milvus 是直连云端的，本机根本不需要 Docker**。真正拦住这两条的是
   > **导入链路要先有 worker 跑完**（Milvus 行数是导入的产物，导入没跑就没有行可数）。
   > `verify_stats` / `verify_audit` 的 T3 也各挂了一句无关的「本机无 Docker」尾巴。
   > 全部改成写**真实阻塞点**。
   > **写错方向的 skip 理由比没有理由更糟**：它让下一个读到的人以为
   > 「这台机器永远验不了 Milvus」而直接放弃。
   > 另：`verify_import.py` 收尾那行原先**写死**「（T2 接口层的 Redis 依赖…）」，
   > 而 T2 这次真的跑过了（Redis 通）⇒ 那半句当场变成假话。改成跟着 `ran` 走。

   > 🔴 **T3 往业务库里写会话，清理必须晚于 T3。** `_sweep()` 原先只在 T2 里调一次，
   > 而 T3 在它**之后**跑 —— 于是每次跑都往 `qa_session`/`qa_log` 里留几行，
   > 「跑一次留几个、跑 N 次留 N 批」。已改为 T3 之后补扫一次，
   > 且 T3 自己记下 `start` 事件里的 `session_id`（**不吃 `MARK` 前缀** ——
   > 标题由后端按问题生成，形状我们控制不了），收尾**按 id 精确删**。
   > 实测日志：`[note] 清理 T3 自造会话：2 个`。

0.6 🔴 **补表之后暴露出来的坑：权限种子停在第 19 条。** 详见上方「权限码」块 ——
   机制上它是「补表 ⇒ T2 第一次真跑 ⇒ 立刻全 403」这条链子抓出来的。
   教训：**T2 长期 SKIP 期间，任何「代码写了但数据没准备」的缺陷都在盲区里**，
   而 SKIP 会让报告看起来「只差环境」。

0.7 ✅ **BGE / DeepSeek 打通后又抓出两个真 bug**（见「模块间契约」块）：
   `FaqCache.match` 的 list/ndarray 边界（**A7 从来没生效过**）与
   FAQ 发布侧的 embedding 口径错位。两个都只在「BGE 可达 + FAQ 真有向量」
   之后才暴露 —— **再一次印证：T3 环境本身就是最强的用例审计。**
   修复后 A7 实测 `faq_hit=True` / `llm_called=False` / `token_in=token_out=0`，
   **186 ms vs 3319 ms**。

1. ✅ **`kb_document_permission` 已不再为空**（`seed_demo.py` 已灌，实测 107 行 /
   81 篇文档 / 2387 个切片）—— A4/A5 的端到端演示前提**已满足**，
   实测问答能召回 5 条带标题的引用。
2. ✅ **`zzvfy_orphan` 残留已清**（实测 `kb_document` 里 `zzvfy%` = 0 行）。
3. ⚠️ **`kb_import_task` 现在停在 16 pending + 1 embedding + 1 failed**：
   没有 Celery worker 就没法推进（`pending`/`embedding` 都是「等 worker」的中间态）。
   演示「上传→解析→切片→入库」全链路必须先起 worker + BGE。
3. 🔴 **A8 的端到端演示需要一条「人为造的未命中」**：缺口池的输入是 `qa_log`，
   而在没有文档的场景下**所有**提问都是未命中 —— 演示时要能区分「真缺口」
   与「权限没配」。`skipped_by_acl` 这个计数就是为这件事留的
   （有召回但全被挡 = 缺权限，不是缺知识）。

---

## 下一步（P1，已完成，留档）

骨架（目录重构 + 配置层 + compose 编排）已完成，be-01 认证已实现并验收通过。P1 按此顺序推进：

1. ~~`backend/models/` + Alembic 初始化~~ → ✅ **组织与权限 6 表已建表并灌入种子数据**；
   其余表（`kb_*` / `qa_log` / `sys_audit_log` 等）待对应模块开工时各增一条迁移
2. ~~`backend/core/security.py` + `deps.py`~~ → ✅ 已随 be-01 落地：JWT 签发校验、
   `get_current_user`、`require_permission`
3. ~~`backend/api/auth_router.py`（4 个接口）~~ → ✅ 已实现
4. ~~`backend/api/org_router.py`（be-02 的 15 个接口）~~ → ✅ **已实现**，验收 68/68；
   顺带把 `schemas/org.py` 的 13 个 DTO 与 `ConflictError` / `ParamError` 一并落地
5. ~~`backend/clients/` 的 mineru / embedding / milvus 封装~~ → ✅ 已随 be-04 落地；
   `llm` 已随 be-05 落地
6. ~~`backend/engines/import_engine`~~ → ✅ **已实现**，验收 68/68（T2 部分已跑，T3 待 worker/Milvus）
7. ~~`backend/clients/llm_client.py`~~ → ✅ 已随 be-05 落地
8. ~~`backend/engines/search_engine`~~ → ✅ **已实现**，验收 **121/121**（T2 + **T3 真 HTTP 链路**均已跑）

> **P1 的后端模块已全部开完**（be-01…be-05）。
> ✅ **收尾已完成（2026-10-15）：迁移到 head `0008`，T2 接口层真跑过且零 FAIL。**
> 剩下的收尾项（`kb_document_permission` 为空、`zzvfy_orphan` 残留）
> 见上方「环境收尾已完成」一节。
>
> `nginx/nginx.conf` 的 SSE 配置**已核无误**（`proxy_buffering off` 等四项齐备），无需改动。
>
> be-02 留的 `kb_document_permission` **引用校验**运行时探测，在 be-04 建表后
> **已自动生效、无需改代码** —— 见 `org_service._assert_no_document_permission`。

---

## 已知坑（踩过的）

**工具链**

- **Starlette 1.7 的 `include_router` 不再把子路由摊平进 `app.routes`**，而是包成
  `_IncludedRouter` 对象。要检查路由是否注册成功，用 `app.openapi()["paths"]`，
  **不要**数 `app.routes`——会漏报。
- `pymysql`/`aiomysql` 连 MySQL 8 的默认 `caching_sha2_password` 需要 `cryptography` 包，
  否则非 SSL 连接直接认证失败。加 `aiomysql` 时一并加 `cryptography`。
- 🔴 **`pymysql` 必须 `<1.2`，否则每一个 BLOB 参数都炸（2026-10-15 踩到，已锁）**。
  1.2.0 起上游把两个名字换成了**字符串毒丸**：

  ```python
  # pymysql/converters.py
  escape_dict = escape_bytes_prefixed = "DO NOT IMPORT THIS!!!"
  ```

  而 `aiomysql 0.3.2`（**当前最新版**，上游尚未修）在 `connection.py:20` 把它
  `import` 进来、在 `connection.py:418` 的 `escape()` 里**直接调用**它处理 bytes：

  ```python
  if isinstance(obj, bytes):
      return escape_bytes_prefixed(obj)      # 'str' object is not callable
  ```

  症状：**凡是写 `bytes` 的语句**都报 `TypeError: 'str' object is not callable`；
  同一个 INSERT 把 BLOB 换成 `None` 就成功 —— 这是最快的判据。
  受害路径（都是真业务路径，不是脚本）：
  `search_engine/session.py` 的每一条 `qa_log`（问答热路径）、
  `faq_mining.py` 的 FAQ 落库、`gap.py` 的缺口落库。
  **它解释了那个诡异的数据分布**：`kb_chunk` 2387 行，而
  `qa_log` / `faq` / `knowledge_gap` **各 0 行** —— 要写 embedding 的路径一条都没成功过。
  修法是 `pyproject.toml` 里钉 `pymysql<1.2`（等 aiomysql 不再引这两个名字才可摘）。
  守门用例：`verify_search.py` 的 **T1-45 / T1-46 / T1-47b**（已做变异测试）。
  ⚠️ **`pymysql.__version__` 会骗人**：1.1.3 上它打印 `2.2.8`
  （模块里 `VERSION=(1,1,3,'final')`，两个值不一致）。要版本号用
  `importlib.metadata.version("pymysql")`，别信 `__version__`。
  ⚠️ 写这条的守门用例时**别用这两个函数去测**：`converters.escape_item(b"...")`
  与 `pymysql` 自己的 `Connection.escape` 在有毒丸时**照样返回正常结果**
  （1.2.3 的 `escape` 改成了 `f"X'{obj.hex()}'"`，根本不碰毒丸）。
  必须用 **aiomysql 的那个 `escape`** 才复现得出来 —— 变异测试下前两者全绿、只有它变红。
- 🔴 **Milvus 的 `query` 结果窗口上限是 16384，且 `offset+limit` **一起**受限**。
  超了**不是静默截断，而是直接报错**：
  `invalid max query result window, (offset+limit) should be in range [1, 16384], but got 100000`。
  `milvus_client.refresh_document_acl` 的前身把 limit 写成 `100_000`，
  注释理由是「默认 16384 会静默截断，所以显式调大」—— **推理方向反了**：
  16384 不是默认值而是上限，调大的后果是**对每一个文档都失败**（与行数无关，
  是请求级校验），于是权限变更在 Milvus 侧**从来没生效过**；
  而调用方按 spec §5.6 吞掉了异常，接口照回 200、MySQL 里的权限也确实改了
  ⇒ 表现是「授权后搜不到 / 撤权后还能搜」，只有日志里有一行
  `向量库不可用（refresh_document_acl）`。
  → 读全量只能用 **`query_iterator`**（内部按 `batch_size` 翻页，不受该窗口限制）。
  **凡是想手写 `offset + limit` 算术的地方，都会再撞一次这个上限。**
  ⚠️ 另一条同源教训：Milvus 是 Bounded 一致性，upsert 后**同一秒内** query 可能仍读到旧值。
  我据此误判过一次「upsert 没生效」，隔 1 秒再读就对了 ——
  验证写入类操作要**轮询几秒**再断言，否则会把一致性延迟当成代码 bug。
- 生产环境配置自检（`settings.py` 的 model_validator）会让缺失密钥的应用**启动即失败**，
  这是刻意设计，不要改成 warning。
- Windows 控制台是 GBK，`uv run python` 打印中文会乱码。验证脚本请断言布尔值，别靠肉眼读中文输出。
- **`alembic.ini` 必须全 ASCII**。Alembic 用 configparser 且 `encoding="locale"` 读它，
  Windows 上 locale 是 GBK → 文件里任何中文注释都让**每条 alembic 命令**直接
  `UnicodeDecodeError` 挂掉。中文注释写进 `alembic/env.py`（Python 源码按 UTF-8 读，不受影响）。
  靠 `PYTHONUTF8=1` 绕只是治标，换个人换台机器还会踩。
- **异步 engine 不能跨 `asyncio.run()` 复用**。`backend/core/db.py` 的 `engine` 是模块级对象，
  连接绑定在**创建它的事件循环**上。写成两次 `asyncio.run()`（先 seed 再 check）会复用上个循环
  遗留的连接，报 `AttributeError: 'NoneType' object has no attribute 'send'`，
  收尾再叠一个 `RuntimeError: Event loop is closed`。
  → **一个脚本一个循环**，收尾 `await engine.dispose()`。`backend/scripts/seed_org.py` 首跑踩过。
- **前端 `typescript` 必须锁 5.x**。npm 默认会装到 7.x（原生新编译器），其包结构里没有
  `lib/tsc`，vue-tsc 3.x 启动即抛 `ERR_PACKAGE_PATH_NOT_EXPORTED`。vue-tsc 的 peer 声明是
  `>=5.0.0`，**拦不住**这个错误组合，必须手动 `npm i -D typescript@5`。
- Element Plus 按需引入靠 `unplugin-vue-components`，它生成
  `src/types/{components,auto-imports}.d.ts`。**这两个文件要先跑一次 `npx vite build` 才存在**，
  否则 `vue-tsc` 不认识 `<el-button>` 之类的组件。已在 `.gitignore` 里排除。
- 图标库用 `lucide-vue-next`（显式具名导入），不用 `unplugin-icons` 的 `i-lucide-*` 自动解析——
  后者图标名拼错只会在运行时渲染出空白方块，前者编译期就报错。
- 🔴 **`.env.example` 会悄悄漂移，且没人会因此报错**（be-07 实现期发现）。
  它由人工维护，而 `settings.py` 由代码演进：be-02 / be-04 / be-06 各加了配置，
  `.env.example` **一条没跟上**，可 CLAUDE.md 宣称它是「所有环境变量的完整清单（唯一事实来源）」。
  症状：运维照它配 `.env`，**配了个寂寞** —— 缺的那些项静默走默认值，
  pydantic-settings 的 `extra="ignore"` 连拼错的键都不报。
  → 已按 5 段回填，并立 **`verify_stats.py` T1-65** 盯这条：
  断言 `Settings.model_fields` 的每个字段（减去派生属性）都出现在 `.env.example` 里。
  **`settings.py` 每加一个字段，`.env.example` 必须同步**，否则 T1-65 变红。
- ⚠️ **`HTTPBearer` 在 OpenAPI 里是 `security`，不是 `parameters`**。
  想在验证脚本里断言「这个端点挂了鉴权」，去 `op["parameters"]` 找 `Authorization` header
  会**把挂了鉴权的端点全报成裸奔**（`core/deps.py` 用的是 `HTTPBearer`）。
  这是「静态断言写错方向」的典型：**它红得理直气壮，但是假的** ——
  假红比没测更糟，因为它会把人把用例注释掉。两个位置都查。

**模块间契约（2026-10-15 T3 环境打通后，一次抓出两个真 bug）**

> 这两个 bug 只在「BGE 真的可达 + FAQ 真的有向量」之后才暴露 ——
> 也就是说它们**从落地那天起就一直在**，只是之前跑不到那一步。
> 它们与前一条「前后端契约」是同一类：**两侧都不报错**，
> 接口恒 200，功能恒不生效。**T3 环境本身就是最强的用例审计。**

- 🔴 **`FaqCache.match` 的入参类型两侧对不上 ⇒ A7「缓存加速应答」永远不会发生。**
  注解写 `query_vector: np.ndarray`，但调用方 `search_engine/qa.py` 传的是它自己
  `_l2_normalize()` 算的 **list**；而 `match` 内部在 `np.asarray(...)` **之前**
  就读 `query_vector.size` ⇒ `AttributeError: 'list' object has no attribute 'size'`。
  `qa.py::_try_faq_match` 的 `except Exception` 是**刻意**的（FAQ 缓存故障不该把
  问答打成 500），于是异常被吞、记一条日志、**按未命中处理**。
  ⇒ 症状：接口 200、`faq_hit` **恒为 False**、每次都真调 LLM（3.3 s、~1300 token），
  而「缓存没启用」在界面上与它**完全一样**。
  修法：**先 `np.asarray` 再判空**（那一行本来就写了 asarray，只是写晚了）。
  实测修复后：`faq_hit=True`、`llm_called=False`、`token_in=token_out=0`、
  **186 ms vs 3319 ms**。
  守门用例 `verify_faq.py::t1_cache_match_accepts_list` ——
  ⚠️ 它**不能**写成 `cache.match(np.asarray(v))`，那正是旧实现也能过的形状；
  必须走 `qa.py` 真实那条 `_try_faq_match(_l2_normalize(list))`。
  已做变异测试（把 asarray 挪回判空之后 ⇒ 两条 list 断言变红）。

- 🔴 **`faq.question_embedding` 是「查询侧」语义，必须带 BGE 查询前缀。**
  它的**唯一用途**就是被用户提问去比（`faq_cache.search` 拿 `query_dense` 比它；
  判重拿 `qa_log.question_embedding` 比它），而这两个向量都出自
  `retrieval.embed_query`（**带前缀**）。发布路径原先用裸 `embed_texts`（passage 口径，
  不带前缀）⇒ 两侧口径错位。
  实测（本机 BGE）同一条问题「带前缀 vs 不带前缀」余弦只有
  **0.827 / 0.942 / 0.837**，而 `faq_match_threshold = 0.92`
  ⇒ **用户一字不差地问同一条已发布 FAQ 也会匹配失败**。
  跨问题对照只有 0.18~0.28，所以 0.83 看着"挺高"，很像正常命中。
  修法：发布路径改走 `retrieval.embed_query`（**同一个语义只有一份实现**）。
  守门用例 `verify_faq.py::t1_publish_embedding_side`（正反两条 + 阈值不许被调小自圆其说）。

- ⚠️ **「靠环境不满足来通过」的用例，配好环境之后就会变红，而代码是对的。**
  实例：`verify_faq.py` 的「LLM 不可用时 answer 降级为原文」。它原先**没有**
  把 LLM 打成不可用，只是**指望** `llm_client.chat()` 真的连不上。
  2026-10-15 配好 DeepSeek key 之后 chat 真的返回了答案 ⇒ 降级分支没走到 ⇒ 它红了。
  → 断言依赖环境 = 断言**没有守任何东西**：环境一变红绿就跟着变，
  而下一个看到红色的人会把**对的代码**改坏。
  修法：显式 monkeypatch 打桩（`faq_mining.llm_client.chat`），
  并且**两个分支都测** —— 只测降级的话，「永远降级」的实现也能全绿。

**前后端契约（be-01…be-09 全落地后回核，一次抓出两个真 bug）**

> 这两条的共同点：**两侧都不报错**。后端照常返回，前端照常渲染，
> `vue-tsc` 也不红（因为前端把字段声明成了**可选**）。唯一的侦测手段是
> 拿前端类型逐字段去后端 `schemas/` 对读 —— 或等 T2 接口层跑起来。

- 🔴 **前端声明了后端从不发的字段 ⇒ 恒 `undefined`，静默走降级分支。**
  两个实例，症状都不是「崩」而是「一直是空的」：
  - `UserItem.roles?: RoleBrief[]` —— 后端发的是 **`role_ids: list[str]`**。
    后果：用户表的**角色列永远是「—」**；分配角色弹窗的回显恒为 `null`，
    顶部「当前角色未知」警示条**从上线第一天挂到现在**。
    ⚠️ 第二处最阴：原作者**是清醒的**（写了「不要传假值，否则保存会静默清空全部角色」
    的注释 + 警示条），所以**数据没丢** —— 只是那个功能**从来没生效过**。
    「防御写对了、功能不生效」比直接崩掉难发现得多：崩了有人报，「永远未知」没人报。
  - `KnowledgeGapItem.department_name` —— 字典 §9 **#146 已撤回**（缺口可能属于**别人**的部门，
    后端不内联返回）。`item.department_name || item.department_id` 便**永远**走右边那支。
  → **可选（`?`）的语义是「后端可能不发」，不是「我懒得确认」。**
  发现后**改类型（删字段）而不是加 `?.` 兜** —— 删掉后 TS 会把所有消费点一次性指出来。
- 🔴 **「兼容多种可能形状」的解析器 = 不知道契约的伪装，猜错时静默走兜底。**
  `normalizeUploadResult` 曾猜 `data` 是数组 / `data.tasks` 是数组 / `data` 本身是任务，
  **三个都不沾**后端实际回的 `UploadAccepted{task_id, document_id, batch_id}`（三个可空字符串）
  ⇒ 恒得 `{batch_id:'', tasks:[]}` ⇒ `onUpload` 判 `tasks.length > 0` 为 false
  ⇒ **进度轮询从不启动**；**上传其实是成功的**，但界面与「上传失败」完全一样。
  ⚠️ 这类代码**看上去很稳健**（有 `Array.isArray`、有 `typeof`、有兜底返回），
  代码评审时会觉得「防御得很到位」—— 这正是它能活下来的原因。
  → 把**契约**写进类型（与后端 DTO 同构），运行时只留「后端返回 null / 形状真变了」
  这一种兜底，**且兜底要能被看出来**（回 `null` 让调用方出提示，而不是回一个像样的空对象）。
  附带教训：契约错了，**函数签名也跟着错** —— 回执里没有任务对象，
  所以「有行可看」必须再发一次 GET，`acceptUpload` 得从 `void` 改成 `async`。
- 🔴 **FastAPI 会静默忽略未声明的查询参数**（不是 422，是根本不进校验）。
  于是「筛选控件看着能用、结果永远是默认窗口」这类 bug **只有 T2 接口层能证明**，
  T1/T1.5 全绿也照样漏。
- ⚠️ **前端库里攒下的「后端还没实现，先按设计稿推定」注释，会过期成假情报。**
  后端全开后逐条回核，改掉了 **~18 处**「尚未实现 / 是壳子 / 形状未冻结」的断言 ——
  其中好几条会把下一个读者引去**重新实现一个已经跑通的模块**。
  搜这几个词：`尚未实现` / `还是壳子` / `形状未冻结` / `待对齐` / `是设计稿`。

**验收脚本自身**（2026-10-15 让 T2 真跑起来之后，一次抓出一批「用例写错、代码是对的」）

> 这一批有个共同形状：**脚本在 SKIP 或假绿里躺了很久，谁也没发现。**
> T2 一旦真跑，它们才第一次被执行 —— 所以「补上环境」这一步本身就是最强的用例审计。

- 🔴 **`httpx.AsyncClient.get()` 的签名里没有 `json=`**（只有 post/put/patch 有）。
  两条「GET 无 token → 401」的鉴权守门用例写成 `client.get(path, json={...})`
  ⇒ `TypeError` ⇒ 被外层 `except Exception` 兜成 **SKIP** ⇒ **一条都没真验过**。
  更糟的是 SKIP 看起来像「环境不具备」，没人会去查。
  → 需要按方法分支构造 kwargs。
- 🔴 **把「角色码」当「用户名」登录**：`kb_admin` 是**角色**，`kb_hq` 才是**用户**。
  于是那条「`kb_admin` 无 `stats:audit:list` → 403」的用例**从写下那天起就 SKIP**，
  而它守护的正是「权限码拦截真的生效」。**别用角色名猜用户名** ——
  用户名在 `seed_org.py` 的 `USERS` 里，去那里取。
  ⚠️ 同理：**权限用例一律不能用 `sys_admin` 跑**，它走通配，永远是绿的。
- 🔴 **越权用例里「冒名者」与「被冒名者」必须是两个人。**
  A2-10 原写法把两者都取成 `username='admin'`（也就是本次登录的账号），
  于是断言 `got != victim` **恒为 False** —— 它红的理由是**用例自相矛盾**。
  更坏的失败模式：有人为了让它绿，把 `victim` 改成不存在的 id ⇒ 从此**恒绿**。
  → 断言落在**具体值**上：落库必须**恰好等于** JWT 本人，且不等于冒名目标。
- 🔴 **`RequestValidationError` → HTTP 400 + `code=1001`，不是 FastAPI 默认的 422。**
  这是本项目全局处理器的**刻意设计**（`core/exceptions.py`），
  于是 4 个脚本里写的 `== 422` 全是错的。→ 断 `status_code in (400, 422) and code == 1001`：
  **不是放水**，是把断言移到真正稳定的那一维（业务码）。
- 🔴 **「静默截断」与「拒绝」是两种契约，用例不能只钉住其中一种。**
  `page_size` 在 `mining.py` 里原本是 `field_validator` 做 `min(value, max)` **静默截断**，
  而另外五个域（org / kb / audit / qa / stats）**全部**是 `le=` **拒绝**，
  壳子里的 `Query(..., le=200)` 也是**拒绝**。⇒ 统一成 `le=`。
  守门的 T1 用例同时断言「有 `le=settings.x`」**且**「没有 `min(value,`」——
  两条一起钉，改回截断就会红。
- ⚠️ **断言 `expected` 照实现抄 = 自证式断言，永远绿。** 同一类还有：
  静态断言扫的和实现是同一份字符串。**两个来源必须独立** ——
  `verify_stats.py` 的 T1-67a 现在是**从前端源码正则抠出它真正请求的 URL** 再断言后端注册了。
- ⚠️ **「此刻不该有 X」是带保质期的断言，过期时不会变红只会失效**（见上方 audit 段）。

**SQL / ORM / 事务**（be-02 实现期一次踩齐，四条的**症状都不像 SQL 问题**）

- 🔴 **`LIKE 'prefix%'` 做前缀匹配时，id 里的下划线是通配符。** 生成的 id 形如
  `dept_a1b2c3d4e5f6`，`_` 在 `LIKE` 里匹配**任意一个字符** → `/dept_01/%` 会连带命中
  `/deptX01/...` 这类**兄弟部门**，把无关子树一起重写，且不报错。
  转义（`\_`）能修，但依赖「记得转义」太脆 —— 改用**没有通配符语义**的精确前缀比较：
  `WHERE SUBSTRING(path, 1, :len) = :prefix`。代价是索引用不上，量级小可忽略。
  ⚠️ 清理脚本里同样别写 `username LIKE 'zzvfy_%'`。
- 🔴 **物化路径的顶层默认值是 `"/"` 而不是 `""`。** 父部门的 path 自带**尾**斜杠，
  所以「父path + 自己id + `/`」天然正确；但顶层没有父，默认取空串会让根长成 `dept_x/`
  —— 丢的是**首**斜杠。`split_materialized_path` 用 `strip("/")` 解析，**两种写法都能跑**，
  所以部门树、权限继承全都正常，只在「与既有种子数据比对」时才暴露。
- 🔴 **`AsyncSession` 在 `commit()` 后可能换连接，`GET_LOCK` 不能挂在它上面。**
  MySQL 命名锁是**连接级**的，取锁/写/提交/放锁必须同一条连接 → 用
  `async with engine.connect() as conn:`。且**释放必须晚于提交**（先放锁会让第二个
  移动者读到提交前的旧快照，算出错 path，静默写坏树）。
- 🔴 **在「另一条连接」上写完，本 session 会读到旧快照（REPEATABLE READ）。**
  上一条说的那种写法之后，若直接 `db.refresh(obj)`，拿到的是**改动前**的值 ——
  接口返回 2000、库里也确实改了，但响应体全是旧值，看起来像「接口静默没生效」。
  更糟的是紧接着用**旧值**去挑「受影响用户」，导致该失效的会话没失效。
  → 先 `await db.commit()` 结束这个空事务（开新快照），再 `refresh`。
- 🔴 **裸 `BizError(msg)` 的 code 默认是 `INTERNAL_ERROR`(5000)，不是 1001。**
  参数错必须用 `ParamError`，否则响应是「HTTP 400 + 业务码 5000」，
  前端按业务码分支时会当成服务端故障。`ConflictError` 同理（409/3002）。

> 这四条的共同点：**错误都发生在「看起来不相关的另一侧」**（重写错子树 / 少个斜杠 /
> 读旧值 / 业务码错位），所以写这类代码时，验收脚本要断言**具体值**而不是「接口没报错」。
> `backend/scripts/verify_org.py` 里 B2/B6/C2/E3/D1 就是为这四条立的守门用例。

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

**开发期本地依赖（BGE / LLM）—— 两个「静默降级」的源头**

- 🔴 **`.env` 里键名写错，pydantic-settings 会静默忽略，不报任何错。**
  实测（2026-10-15）：`.env` 里写的是 **`DEEP_SEEK_KEY`**（多一个下划线），
  而字段是 **`DEEPSEEK_API_KEY`** ⇒ `Settings.extra="ignore"` 把它整条丢掉
  ⇒ `deepseek_api_key` 为空 ⇒ 问答在 LLM 那一步才失败，而**报错信息里不会出现
  「key 没配」**（`llm_client` 把 401 收敛成业务异常）。用户以为自己已经配好了。
  → 改了 `.env` 的键名之后**必须验一次 `bool(settings.deepseek_api_key)`**，
  别靠肉眼对键名。改键名后**还要重启 API** —— `uvicorn --reload` 只盯 `.py` 文件，
  `.env` 变了不会重载，旧进程仍在用启动时的值。
  另：`.env` 里 `KEY = value`（等号两侧带空格）本身是合法的（python-dotenv 会 strip），
  但 `sed 's/^KEY=/.../'` 这种改名会匹配不上 —— 用容忍空白的正则。

- 🔴 **`embedding_base_url` 默认指向 `127.0.0.1:9997`，开发机上没人监听它。**
  于是问答第一步就断，而 `embedding_client` 把 httpx 异常统一收敛成
  **「向量服务调用失败：网络异常」—— 这句话里没有「BGE」两个字**，
  极容易被误读成 DeepSeek/LLM 的问题（尤其刚配完 key 的时候）。
  排查提示：`curl http://127.0.0.1:9997/health`，不通就是它。
  → 开发期用 `backend/scripts/dev_bge_server.py` 在本地把这个端口补上
  （CPU 跑 BGE，只暴露 OpenAI 兼容 `/v1/embeddings`，**应用侧一行不用改**，
  换回公司 GPU 时只改 `EMBEDDING_BASE_URL`）。torch 装在**独立的 `.venv-bge`**，
  不进应用/部署镜像 —— PRD D17 的「BGE 只走 HTTP」不能被开发便利破坏。
  - 🔴 **换 BGE 实现前必须先跑 `--selftest`**：它拿库里**已存**的切片正文
    本地重算向量，与那一行**已存**的向量比余弦（实测 12/12 = 1.0000）。
    池化方式（BGE 用 CLS）或归一化口径不同时，照样返回 768 维**但语义错位**，
    检索会安静地返回一堆不相关结果 —— 「服务起来了、能返回 768 维」**不是证据**。
  - `huggingface.co` 本机不可达（curl 000），`hf-mirror.com` 可用；
    脚本里 `HF_ENDPOINT` 必须在 `import transformers` **之前**设好。
  - 🔴 **请求体模型不能定义在路由函数内部**。本文件有
    `from __future__ import annotations`，注解是字符串，FastAPI 要用
    `get_type_hints` 去**模块全局**解析；类在函数里 ⇒ 解析失败 ⇒ 它把参数
    当成**查询参数** ⇒ 客户端收到
    `422 {"detail":[{"loc":["query","body"],"msg":"Field required"}]}`。
    错误信息指向 `query`，会把人引去看 URL 参数，而根因是作用域。实测踩到过。

**灌数 / 运维脚本（2026-10-15 实测踩到，代价是清 56 行脏数据）**

- 🔴 **`seed_demo.py` 的 `--help` 曾经会「跑一整遍灌数」。** 它没有 argparse，
  原先的 `main_()` 是：

  ```python
  if "--clean" in sys.argv: ...
  elif "--refill-faq-vectors" in sys.argv: ...
  else: sys.exit(asyncio.run(run()))     # ← `--help` 落进这里
  ```

  **未知输入 → 执行最危险的那个分支。** `--help` 是所有人（包括我）都默认
  「安全、只读」的操作，于是它把 FAQ 从 6 条变成 18 条、`qa_log` 从 600 变成 1800、
  会话从 31 变成 57 —— 而且**全程没有任何报错**，看起来就像「命令跑完了」。
  一次是 `| head -12` 截断、一次是完整跑完，两次都没人察觉。

  → 修法两条，都是**把默认分支改成安全的那个**：
  ① 不认识的参数**一律拒绝**并 `sys.exit(2)`，同时补上真正的 `--help`；
  ② `_run()` 开头加 **`_guard_already_seeded()`** —— 见到 `demo%` 的 `qa_log`
  就中止，什么都不写，并告诉你「先 `--clean`」。这条才是真正会拦住人的那条。

  ⚠️ **判据要用「demo 前缀的 qa_log 行数」，不能用「表非空」。**
  `kb_document` 里有真实文档，按表非空判会把正常场景也拦下来。

- 🔴 **判「脚本幂不幂等」要逐表看，别给整脚本一个结论。** `seed_demo._run()` 里：
  `kb_document` / `kb_chunk` / `kb_document_permission` / `knowledge_gap` 用**固定 id**
  ⇒ 重跑是覆盖，幂等；而 `faq` / `faq_candidate` / `qa_session` / `qa_message` / `qa_log`
  用 `new_id()` **随机 id** ⇒ 重跑就是**新增一批**。
  「表非空」看不出来，只有**数一遍行数**才看得出来 —— 所以上面那条护栏
  是拿行数当判据的。

- ⚠️ **脏数据要按 id 逐字列出再删，不要用时间窗/前缀之类的运行时条件。**
  清这次事故时我第一版写的是 `WHERE created_at BETWEEN ...`，
  被安全审查挡下 —— **挡得对**：那个集合是运行时算出来的，读代码的人
  （和审查者）看不到爆炸半径。改成把 24 个 session id / 12 个 faq id /
  20 个候选 id **全部逐字写进脚本**、先做「名单数 == 命中数」预检、
  预检不过就整体放弃，才既安全又可复核。
  **代价对比**：逐字写死 56 个 id 花了我两分钟；删错东西是找不回来的。

- ⚠️ **清理前先抄一份「事故前」的基线。** 这次能确认「删干净了」，
  靠的是事故前正好量过 `qa_log = 622 / faq = 6`；删完对回 622 / 6，
  才敢说恢复了。**没基线就只能靠"看起来对"** —— 而这次的教训恰恰是
  「看起来对」正是它隐藏了两次误跑的原因。

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
