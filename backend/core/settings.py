"""全局配置。

所有可调参数集中在此。业务代码**禁止**直接调用 os.getenv，
一律通过 `from backend.core.settings import settings` 读取。

连接串（DATABASE_URL / REDIS_URL / CELERY_*）由各分段字段派生，
不在 .env 里重复配置 —— 避免分段值与整串值不一致这种经典事故。
"""

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 应用 ----------
    app_env: Literal["dev", "production"] = "dev"
    app_name: str = "RAG 知识库管理平台"
    api_port: int = 8000
    log_level: str = "INFO"
    # 前端跨域来源，逗号分隔。生产环境由 Nginx 同源反代，通常留空即可。
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---------- MySQL ----------
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_db: str = "rag_kb"
    mysql_user: str = "rag"
    mysql_password: str = ""

    # ---------- Milvus ----------
    milvus_host: str = "127.0.0.1"
    milvus_port: int = 9091
    milvus_collection: str = "kb_chunk"

    # ---------- Redis ----------
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db_cache: int = 0
    redis_db_broker: int = 1
    redis_db_result: int = 2

    # ---------- 认证 ----------
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    login_max_failures: int = 5
    login_lock_minutes: int = 15

    # ---------- 外部服务 ----------
    mineru_api_base: str = "https://mineru.net/api/v4"
    mineru_api_token: str = ""
    mineru_poll_timeout_seconds: int = 900
    # MinerU 轮询退避（PRD §6.4.1）：首次 3 s，之后翻倍，上限 30 s。
    # ⚠️ 这与 Celery 的任务重试退避是**两回事**，别混。
    mineru_poll_interval_initial_seconds: int = 3
    mineru_poll_interval_max_seconds: int = 30
    # 开发期指向本机 4060 上的 BGE 服务；生产期指向公司 GPU 服务器
    embedding_base_url: str = "http://127.0.0.1:9997/v1"
    embedding_model: str = "BAAI/bge-base-zh-v1.5"
    embedding_dim: int = 768
    embedding_batch_size: int = 32
    embedding_timeout_seconds: int = 120
    # 🔴 `BAAI/bge-*` 系列在**检索**时要求给 query 加这个指令前缀，**passage 不加**。
    #    加错边（两边都加 / 都不加）不会报错，只是相似度整体偏移 ——
    #    表现为「检索结果全都差不多」。
    #    职责划分见 `clients/embedding_client.py` 头部：那边只做文档片段（不加），
    #    前缀由检索侧（be-05）加。**不要把它挪进 embedding_client「顺手统一」**。
    embedding_query_prefix: str = "为这个句子生成表示以用于检索相关文章："
    # 🔴 HuggingFace 主站在国内**不可达**（实测），但 hf-mirror.com 可达。
    # 切片要按 BGE 的 token 口径计数（字典 §9 #12），tokenizer 得从 HF 拉，
    # 所以这个镜像地址必须可配 —— 否则换台机器或进 Docker 就是「导入全挂」。
    # 消费方：engines/import_engine/tokenizer.py（在 import huggingface 之前设进环境变量）。
    hf_endpoint: str = "https://hf-mirror.com"
    deepseek_api_base: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"

    # ---------- 知识导入（be-04）----------
    # 单文件上限与批量数量上限（PRD §6.4.1）。
    upload_max_file_mb: int = 200
    upload_batch_max_files: int = 50
    # 🔴 解 MinerU 返回的 zip 时的**累计**解压体积上限（CLAUDE.md 安全红线 5）。
    # 不给上限的话，一个几十 KB 的 zip 可以解出几百 GB（zip bomb）。
    zip_max_uncompressed_mb: int = 2048
    # 任务重试：countdown = min(cap, base * factor ** retries)
    # 🔴 factor=5 是为了得到 60 / 300 / 900，**正好等于 PRD §6.4.1 的「退避 60/300/900 s」**。
    #    原先写的 factor=2 只会给 60/120/240（字典 §9 #95 已定案改 5）。
    #    改这里等于改重试节奏，PRD 是验收依据，不要随手调。
    import_retry_max: int = 3
    import_retry_backoff_base: int = 60
    import_retry_backoff_factor: int = 5
    import_retry_backoff_cap: int = 900
    # Milvus 建 collection 用（be-04 负责建，be-05 只消费）
    milvus_metric_type: str = "COSINE"
    milvus_index_type: str = "AUTOINDEX"
    # 台账与任务列表的分页每页上限（与 be-02 的 org_user_page_size_max 同构）
    kb_page_size_max: int = 100

    # ---------- 切片与检索 ----------
    chunk_max_tokens: int = 512
    chunk_min_tokens: int = 32
    chunk_overlap: int = 64
    retrieval_top_k: int = 5
    # 融合前每个子检索的召回倍数，避免 RRF 融合后候选过少
    retrieval_prefetch_ratio: int = 3
    kb_min_score: float = 0.4

    # ---------- 检索问答（be-05）----------
    # 多轮追问时注入 Prompt 的历史轮数（1 轮 = user + assistant 两条消息）。
    # 🔴 用**拼接**而非问题改写：改写要多一次 LLM 调用，P1 不值得（be-05 §10-22）。
    #    取值过大会让历史挤掉知识正文，3 轮是不喧宾夺主的经验值。
    qa_history_rounds: int = 3
    # 会话标题取首问的前 N 字（字典 §2.3 `qa_session.title` 写「前 20 字」）
    qa_session_title_chars: int = 20
    # 受限探测结果的缓存秒数（PRD §6.2.4）。**探测每问多一次 Milvus 查询**，
    # 多轮追问同一问题时缓存能直接省掉它。60 s 是 PRD 给的量级。
    qa_probe_cache_seconds: int = 60
    # 会话消息分页每页上限（与 kb_page_size_max 同构）
    qa_page_size_max: int = 100

    # ---------- DeepSeek（be-05）----------
    deepseek_timeout_seconds: int = 120
    # 单次生成上限。**必须设**：不设时模型可能一直生成到上下文用尽，
    # 表现为「回答停不下来」且 token 费用失控。
    deepseek_max_tokens: int = 2048
    # 知识问答要「照着资料说」，不要发挥。低温比高温更贴资料。
    deepseek_temperature: float = 0.3

    # ---------- 沉淀挖掘（be-08）----------
    faq_cluster_similarity: float = 0.85
    faq_min_frequency: int = 5
    faq_match_threshold: float = 0.92
    mining_lookback_days: int = 7
    # 置信度公式 `avg_sim × min(1, frequency / 本值)` 的频次参考点（PRD §6.3.1 只说
    # 「平均相似度 × 频次归一化」，没给公式）。20 的读法：某问题被问满 20 次，
    # 频次项就吃满 1.0，此后置信度只由相似度决定 —— 不设上限的话，
    # 一个被反复问的寒暄句会靠频次盖过真正该沉淀的问题。
    faq_confidence_freq_ref: int = 20
    # 进程内 FAQ 索引的兜底全量重载周期（秒）。主路径是 Redis 订阅的增量刷新，
    # 这个定时器只兜「订阅掉了 / 消息丢了」—— 没有它，一次订阅断线会让
    # 新发布的 FAQ 直到下次重启都命中不了，且没有任何报错。
    faq_cache_reload_interval_seconds: int = 300
    # 候选 / FAQ 列表分页上限（与 org / kb / audit / qa 四域的既有写法一致）。
    faq_page_size_max: int = 100
    # 🔴 手动挖掘（`POST /faq/mine`）的自限耗时，**必须小于前端全局超时**
    #    （`frontend/src/api/http.ts` 的 `timeout: 15_000`）。超过它前端必然断开，
    #    用户看到超时而后端还在跑 —— 所以宁可在后端主动截短窗口并回带 `partial`。
    #    改这个值时必须同时看 http.ts，验收脚本里有一条断言在看这对关系。
    faq_mine_sync_max_seconds: int = 10

    # ---------- 知识缺口（be-09）----------
    # 每轮聚合的回溯窗口（小时）。beat 每 10 分钟跑一轮、窗口 3 小时 ⇒ 窗口**必然重叠**，
    # 重叠部分靠缺口行自己的 `last_seen_at` 水位线去重（见 `engines/.../gap.py`）。
    # 窗口比间隔宽是刻意的：worker 偶发堆积时，晚到的 `qa_log` 还能被下一轮捞到。
    gap_aggregate_lookback_hours: int = 3
    # 是否自动把 `tasked` 缺口置 `closed`。可关是因为**关闭不可逆**：
    # 台账一旦显示「已解决」，再自动回退会让它反复横跳、无法据以汇报。
    gap_auto_close_enabled: bool = True
    # 缺口清单分页上限（与其它域的既有写法一致）。
    gap_page_size_max: int = 200
    # 🔴 分类推荐的**独立**超时（秒），刻意远小于 `deepseek_timeout_seconds`：
    #    推荐分类跑在聚合任务里，而它是**可选装饰** —— 一次 DeepSeek 卡顿
    #    不该占满整轮聚合的预算，缺口照常入池、分类留空即可。
    gap_llm_suggest_timeout_seconds: int = 5

    # ---------- 组织域（be-02）----------
    # 部门树移动时的 MySQL 命名锁（GET_LOCK）等待上限。
    # 拿不到锁即认为有并发移动在进行，直接报 409 冲突而不是无限等。
    org_dept_lock_timeout_seconds: int = 5
    # 用户列表分页每页上限。
    org_user_page_size_max: int = 100

    # ---------- 审计（be-06）----------
    # 保留期。字典 §9 #45：表无限增长会拖垮「按时间范围翻页」这条主路径
    # （审计页默认就查最近 7 天，老数据对查询无用，只对合规有用）。
    # 180 天是等保「网络日志留存不少于六个月」的下限。
    audit_retention_days: int = 180
    # 清理**必须分批**：一次 DELETE 几十万行会锁表并撑爆 binlog，
    # 表现为「清理任务跑的那个整点，全站写入变慢」——与审计本身毫无关系，
    # 极难反查。5000 是单批的安全量级。
    audit_purge_batch_size: int = 5000
    # 「读取正文」类审计的去重窗口（字典 §9 #44）。
    # 知识管理员翻一份长文档会触发几十次读取，逐条落库会让审计表被同一个人
    # 同一份文档刷满，真正的异常行为反而被淹没。窗口内同 (user, doc) 只记一条。
    audit_read_dedup_seconds: int = 300
    # 审计列表默认时间跨度（不传 start_time/end_time 时）。
    # 必须有默认值：不设上限时「不传参」等于全表扫，而这个端点面向管理员的
    # 随手点开，不能指望调用方每次都带时间范围。
    audit_default_range_days: int = 7
    audit_page_size_max: int = 100
    # 清理任务失败后的重试退避。**必须显式配置并使用**：Celery 的
    # `retry_backoff` / `retry_backoff_max` 对手动的 `self.retry()` 静默无效，
    # 不给 `countdown` 会退回写死的 180 秒（CLAUDE.md 已知坑）。
    # ⚠️ 这两个系数与 `import_retry_backoff_*` 同构但**独立** —— 清理是低频
    # 运维任务，退避节奏跟导入重试没有任何关系，共用会让调其中一个时误改另一个。
    audit_purge_retry_backoff_base: int = 60
    audit_purge_retry_backoff_cap: int = 600
    # 🔴 是否信任 `X-Real-IP`。**默认 True 是因为部署形态就是 nginx 反代**
    #   （nginx.conf 用 `proxy_set_header X-Real-IP $remote_addr` **覆盖**写入，
    #   客户端伪造不了）。直连暴露 API 端口时必须置 False，否则任何人
    #   手填一个 `X-Real-IP` 就能把审计 IP 写成任意值 —— 审计的 IP 一旦可伪造，
    #   它作为「谁干的」的证据就全废了。
    audit_trust_proxy_ip: bool = True

    # ---------- 运营看板（be-07）----------
    # 每轮聚合回溯的小时数。**不是 0 也不是 1**：`qa_log` 的落库与提问时刻
    # 之间有事务提交 + 网络往返 + 连接池排队的延迟，会跨桶迟到。
    # 回溯窗口就是用来兜这些迟到行的（be-07 §4.2）。
    stats_lookback_hours: int = 3
    # 看板查询的默认时间窗（`start_time`/`end_time` 都不传时）。
    # **必须有默认值**：不设时「不传参」等于全表扫，而这个端点面向管理员随手点开。
    stats_default_range_days: int = 7
    # 每桶 JSON 快照里存的 TOP 条数（查询时再取前 K）。
    # 也是 `limit` 参数的**上限**：不设上限时 `limit=100000` 会把整桶 JSON 拉出来。
    stats_top_n: int = 50
    # 延时直方图的桶边界（毫秒），逗号分隔，**最后一个为开区间**。
    # 🔴 用字符串而不是 `list[int]`：pydantic-settings 对 list 型环境变量要求
    #    JSON 字面量（`[0,200]`），写进 `.env` 极易漏引号，而漏了会静默按默认值走。
    #    逗号串更好输入，切分在 `latency_edges` 里做。
    stats_latency_edges_ms: str = "0,200,500,1000,2000,5000"
    # `stats_hourly` 保留期。日表长期保留，小时表只服务「近 48h」这类细粒度查询，
    # 留 90 天足够回溯排障（同 be-06 的 audit_retention_days 思路）。
    stats_hourly_retention_days: int = 90
    # `stats_daily` 保留期。**0 = 永久保留**，此时清理任务不碰日表。
    # ⚠️ 这个 0 必须显式判断，不能写成 `WHERE bucket_date < now - INTERVAL 0 DAY`
    #    —— 那会把当天之前的全部日数据删光（be-07 §7）。
    stats_daily_retention_days: int = 0
    # 清理分批大小（同 audit_purge_batch_size 的理由：一次 DELETE 几十万行会持锁）。
    stats_purge_batch_size: int = 5000
    # 清理任务失败的重试退避系数（同 be-06：Celery 的 retry_backoff 对手动
    # `self.retry()` 静默无效，不给 countdown 会退回写死的 180 秒）。
    stats_purge_retry_backoff_base: int = 60
    stats_purge_retry_backoff_cap: int = 600
    # `granularity=hour` 允许的最大跨度。小时表保留期有限，且「按小时看一年」
    # 会有 8760 个点，前端也画不动。
    stats_hour_range_max_days: int = 7
    # 任意查询的最大跨度（防一次拉十年）。
    stats_query_max_days: int = 365
    # `overview` 选表的跨度阈值：≤ 此值查 `stats_hourly`（更细），否则查 `stats_daily`。
    # 48h 是「两天以内用小时表更细、超过两天用日表更省」的经验阈值。
    stats_hour_table_max_hours: int = 48
    # `top-knowledge` 的角色硬闸（红线 4）。**这不是权限码的替代**，而是第二道闸：
    # 该端点返回文档标题，若哪天有人把 `stats:dashboard:view` 授给 staff，
    # 它就变成静默越权泄漏点。显式在校验角色能把「未来的授权失误」
    # 变成响亮的 403，而不是一条安静的泄漏（`P2-13-be-stats` §4.4）。
    stats_admin_role_codes: str = "sys_admin,kb_admin"

    # ---------- 安全开关 ----------
    # 开启后权限缺失提示会附带受限文档标题。
    # 这会泄漏「存在某份敏感文档」这一事实，仅在演示场景临时打开。
    acl_reveal_blocked_titles: bool = False

    # ---------- 派生连接串 ----------
    @property
    def database_url(self) -> str:
        """SQLAlchemy 异步连接串。"""
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"
        )

    def _redis_base(self, db: int) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{db}"

    @property
    def redis_url(self) -> str:
        return self._redis_base(self.redis_db_cache)

    @property
    def celery_broker_url(self) -> str:
        return self._redis_base(self.redis_db_broker)

    @property
    def celery_result_backend(self) -> str:
        return self._redis_base(self.redis_db_result)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def stats_latency_edges(self) -> list[int]:
        """延时直方图的桶边界（升序 int 列表）。

        🔴 **必须至少 2 个边界**，否则一个桶都构不成（`edges.length - 1 == 0`），
           直方图恒为空而 `counts` 也不报错 —— 症状是「延时图永远没数据」。
           配置写错时在这里直接抛，让应用**启动即失败**（同 jwt_secret 的自检思路：
           坏配置宁可起不来，也不要带着它跑出错误的看板）。
        """
        parts = [p.strip() for p in self.stats_latency_edges_ms.split(",") if p.strip()]
        edges = [int(p) for p in parts]
        if len(edges) < 2:
            raise ValueError("STATS_LATENCY_EDGES_MS 至少需要 2 个边界（否则构不成任何桶）")
        if edges != sorted(edges):
            raise ValueError("STATS_LATENCY_EDGES_MS 必须升序，否则桶区间重叠")
        return edges

    @property
    def stats_admin_role_list(self) -> list[str]:
        """`top-knowledge` 角色硬闸允许的角色码（红线 4）。"""
        return [r.strip() for r in self.stats_admin_role_codes.split(",") if r.strip()]

    # ---------- 生产环境自检 ----------
    @model_validator(mode="after")
    def _assert_production_secrets(self) -> "Settings":
        """生产环境缺密钥就拒绝启动。

        宁可启动失败，也不要带着默认 JWT 密钥上线 ——
        那意味着任何人都能伪造管理员 token。
        """
        if self.app_env != "production":
            return self

        missing: list[str] = []
        if not self.mysql_password:
            missing.append("MYSQL_PASSWORD")
        if self.jwt_secret.startswith("change-me") or len(self.jwt_secret) < 32:
            missing.append("JWT_SECRET（须为 32 位以上随机串）")
        if not self.mineru_api_token:
            missing.append("MINERU_API_TOKEN")
        if not self.deepseek_api_key:
            missing.append("DEEPSEEK_API_KEY")

        if missing:
            raise ValueError(
                "生产环境缺少必要配置：" + "、".join(missing) + "。请检查 .env。"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """带缓存的配置单例，避免重复解析 .env。"""
    return Settings()


settings = get_settings()
