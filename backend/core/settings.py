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
    milvus_port: int = 19530
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
    # 开发期指向本机 4060 上的 BGE 服务；生产期指向公司 GPU 服务器
    embedding_base_url: str = "http://127.0.0.1:9997/v1"
    embedding_model: str = "BAAI/bge-base-zh-v1.5"
    embedding_dim: int = 768
    embedding_batch_size: int = 32
    deepseek_api_base: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"

    # ---------- 切片与检索 ----------
    chunk_max_tokens: int = 512
    chunk_min_tokens: int = 32
    chunk_overlap: int = 64
    retrieval_top_k: int = 5
    # 融合前每个子检索的召回倍数，避免 RRF 融合后候选过少
    retrieval_prefetch_ratio: int = 3
    kb_min_score: float = 0.4

    # ---------- 沉淀挖掘 ----------
    faq_cluster_similarity: float = 0.85
    faq_min_frequency: int = 5
    faq_match_threshold: float = 0.92
    mining_lookback_days: int = 7

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
