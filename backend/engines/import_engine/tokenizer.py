"""切片用的 token 计数（字典 §9 #12 定案：**用 BGE 的 tokenizer**）。

为什么值得为此引一个依赖：`chunk_max_tokens=512` / `min=32` / `overlap=64`
这三个阈值只有在**与向量模型同一套 token 口径**下才有意义。用字符数近似的话，
中文约 1 字 1 token 还算接近，英文按字符算会**高估一半以上** ——
结果是英文文档的切片实际只有限额的一半，且这个偏差随文档语言而变，
表现为「同样的 512 阈值，中英文切出来的粒度完全不同」。

🔴 计数**含 `[CLS]` / `[SEP]`**（`encode()` 的默认行为），这是刻意的：
   远端 BGE 服务的 512 上限是**含特殊 token 的序列长度**。若只数正文 token，
   正文正好 512 的切片加上两个特殊 token 就超限，会被模型静默截断 ——
   截断掉的那几个 token 永远检索不到，且不报错。
"""

from __future__ import annotations

import logging
import os
import threading

from backend.core.exceptions import DependencyError
from backend.core.settings import settings

logger = logging.getLogger(__name__)

_tokenizer = None
# Celery worker 可能是多线程池（Windows 上 `-P threads`），加锁避免并发重复加载
_lock = threading.Lock()

__all__ = ["count_tokens", "get_tokenizer"]


def _load():
    """加载 BGE tokenizer。

    🔴 这里**必须**用函数内 import，不能放文件顶部。
       `huggingface_hub` 在 **import 那一刻**读取 `HF_ENDPOINT` 并缓存下来，
       而国内访问 huggingface.co 是不可达的（实测），必须先把镜像地址设进环境变量。
       改成顶部 `from tokenizers import ...` 会让镜像设置晚一步生效，
       症状是「首次加载卡住几分钟后超时」，看起来像网络问题而不是配置问题。

    ⚠️ 写 `os.environ` 与「配置读取走 settings」的纪律不冲突：
       值来自 `settings.hf_endpoint`，这里只是把它转交给第三方库约定的环境变量名。
    """
    os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
    # noqa: PLC0415 —— 见上面的理由，不能提到模块顶部
    from tokenizers import Tokenizer  # noqa: PLC0415

    return Tokenizer.from_pretrained(settings.embedding_model)


def get_tokenizer():
    """进程内单例。首次调用会从 HF 镜像拉取 tokenizer.json（约 700 KB）。

    ⚠️ 拉取失败一律抛 `DependencyError`，**不做字符近似降级**。
       静默降级会让同一份文档在不同机器上切出不同的片，且没有任何痕迹 ——
       而切片是「一次写死、长期复用」的，事后无法判断某份文档用的是哪套口径。
       生产部署应在镜像构建期预下载（见 `backend/Dockerfile`），使其离线可用。
    """
    global _tokenizer
    if _tokenizer is None:
        with _lock:
            if _tokenizer is None:
                try:
                    _tokenizer = _load()
                except Exception as exc:  # noqa: BLE001 —— 第三方异常一律收敛
                    logger.error("BGE tokenizer 加载失败（model=%s）", settings.embedding_model)
                    raise DependencyError(
                        f"分词器不可用：{settings.embedding_model}"
                    ) from exc
    return _tokenizer


def count_tokens(text: str) -> int:
    """按 BGE 口径数 token，含 `[CLS]` / `[SEP]`。

    这是**切片模块唯一允许的计数入口** —— 别处再写一个 `len(text) // 2` 之类的
    近似，就等于把上面那套口径的意义抹掉了。
    """
    if not text:
        return 0
    return len(get_tokenizer().encode(text).ids)
