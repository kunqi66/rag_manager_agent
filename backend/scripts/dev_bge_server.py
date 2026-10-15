"""开发期本地 BGE 向量服务（OpenAI 兼容 `/v1/embeddings`）。

## 为什么需要它

PRD D17 的设计是「公司 GPU 服务器只跑 BGE」，`EMBEDDING_BASE_URL` 默认指向
`http://127.0.0.1:9997/v1`。开发机上没人监听那个端口时，**问答链路第一步就断**：

    问答失败（业务异常）：向量服务调用失败：网络异常

—— 注意这句话里**没有出现 BGE 三个字**，所以很容易被误读成 DeepSeek 的问题
（尤其是刚配完 key 的时候）。`embedding_client._wrap()` 把任何 httpx 异常都收敛成
「网络异常」，丢掉了对端地址，这是**排查时的真实痛点**：它不告诉你是谁连不上。

本脚本在开发机上把那个端口补上：CPU 跑 `BAAI/bge-base-zh-v1.5`，
对外只暴露 OpenAI 兼容的 embeddings 接口 —— 于是**应用侧一行代码都不用改**，
换回公司 GPU 时只要把 `EMBEDDING_BASE_URL` 指回去。

## 🔴 这里**绝对不加** BGE 的查询前缀

`BAAI/bge-*` 检索时要给 **query** 加 `为这个句子生成表示以用于检索相关文章：`，
**passage 不加**。加前缀的分工在应用侧：`embedding_client` 只处理文档片段（不加），
be-05 的检索侧加。本服务只做「你给什么文本，我编码什么文本」——
在这里「顺手统一」会让 query **被加两次**前缀，相似度整体偏移，
且表现为「检索结果都差不多」而不是报错。

## 用法

    # 1) 一致性自检（强烈建议先跑）：本地编码的向量 vs Milvus 里已存的向量
    .venv-bge/Scripts/python.exe backend/scripts/dev_bge_server.py --selftest

    # 2) 起服务（前台，Ctrl-C 结束）
    .venv-bge/Scripts/python.exe backend/scripts/dev_bge_server.py --serve

`HF_ENDPOINT` 默认设成 hf-mirror（本机 huggingface.co 不可达，实测 000）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# 🔴 Windows 控制台是 GBK，打 `✅` 这类字符会直接 `UnicodeEncodeError` 把脚本炸掉
#    （本项目其它脚本都踩过，见 CLAUDE.md 已知坑）。必须在**任何 print 之前**改。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# huggingface.co 在本机不可达（curl 返回 000），必须在 import transformers 之前
# 就把 endpoint 指到镜像 —— 晚一步设置就没用了，模型会去连不上的域名。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
# 本机 HF 缓存在 D:\hugging_face_model，不支持符号链接，会刷一大段警告。
# 它只是提示、不影响正确性 —— 关掉，免得把真正的输出淹掉。
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

MODEL_NAME = os.environ.get("BGE_MODEL_NAME", "BAAI/bge-base-zh-v1.5")

# 🔴 请求体模型**必须在模块级定义**，不能写在 `serve()` 里面。
#    本文件有 `from __future__ import annotations`，所以注解是**字符串**，
#    FastAPI 要用 `get_type_hints` 去模块全局里解析它。类定义在函数内部时，
#    那个名字不在全局里 ⇒ 解析失败 ⇒ FastAPI 把它当成**查询参数**
#    ⇒ 请求体根本不校验，客户端收到
#       `422 {"detail":[{"loc":["query","body"],"msg":"Field required"}]}`
#    —— 错误信息指向 `query`，会把排查的人引去看 URL 参数，而真正的原因是
#    一个作用域问题。实测踩到过（2026-10-15）。
from pydantic import BaseModel  # noqa: E402  —— 位置紧跟上面的说明


class EmbeddingsIn(BaseModel):
    """OpenAI `/v1/embeddings` 的请求体。`input` 允许单条字符串或数组。"""

    model: str | None = None
    input: list[str] | str


def _load_model():
    """把模型加载独立出来：自检与服务都要它，但只有服务需要常驻。"""
    from sentence_transformers import SentenceTransformer

    print(f"[bge] 加载 {MODEL_NAME}（HF_ENDPOINT={os.environ['HF_ENDPOINT']}）…", flush=True)
    t0 = time.time()
    model = SentenceTransformer(MODEL_NAME, device="cpu")
    # sentence-transformers 6.x 把 `get_sentence_embedding_dimension` 改了名；
    # 两代都认，免得换台机器装到旧版就 `AttributeError`。
    getter = getattr(model, "get_embedding_dimension", None) or (
        model.get_sentence_embedding_dimension
    )
    dim = getter()
    print(f"[bge] 就绪：dim={dim}，耗时 {time.time() - t0:.1f}s", flush=True)
    return model


def _encode(model, texts: list[str]) -> list[list[float]]:
    """编码成 **L2 归一化** 的向量。

    `normalize_embeddings=True` 不是可选项：本项目的 Milvus 索引用 **COSINE**。
    归一化后余弦退化成点积，与不归一化的结果**方向一致、数值不同** ——
    不归一化时余弦值会整体偏离，而 `kb_min_score` 是个绝对阈值（默认 0.4），
    于是「检索恒空」或「什么都算命中」，取决于偏到哪边。
    """
    vectors = model.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return [v.tolist() for v in vectors]


# ═══════════════════════════════════════════════════════════════
# 一致性自检
# ═══════════════════════════════════════════════════════════════


def selftest(sample: int) -> int:
    """本地编码 Milvus 里**已存**的切片正文，与那一行**已存**的向量比余弦。

    这是「本地 BGE 能不能替公司那台」的**唯一硬证据**。
    光看「服务起来了、能返回 768 维」没有任何意义 —— 池化方式不同
    （CLS vs mean）、归一化不同，都会照样返回 768 维**但语义完全错位**的向量，
    而检索会安静地返回一堆不相关结果。

    ⚠️ 必须先起 Milvus 可达（本机是直连云端）。
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from backend.clients import milvus_client
    from backend.core.settings import settings

    model = _load_model()

    from pymilvus import MilvusClient

    client = MilvusClient(uri=f"http://{settings.milvus_host}:{settings.milvus_port}")
    rows = client.query(
        collection_name=settings.milvus_collection,
        filter='id != ""',
        output_fields=["id", "content", "dense"],
        limit=sample,
    )
    if not rows:
        print("❌ Milvus 里没有可比的切片")
        return 3
    print(f"[bge] 取到 {len(rows)} 条已存切片，逐条比对…", flush=True)

    import math

    cosines: list[float] = []
    for row in rows:
        stored = row["dense"]
        # Milvus 存的是 float32，本地算出来是 float32→float；量级无关，只算方向
        fresh = _encode(model, [row["content"]])[0]
        dot = sum(a * b for a, b in zip(stored, fresh, strict=True))
        na = math.sqrt(sum(a * a for a in stored))
        nb = math.sqrt(sum(b * b for b in fresh))
        cosines.append(dot / (na * nb) if na and nb else 0.0)

    cosines.sort()
    lo, mid, hi = cosines[0], cosines[len(cosines) // 2], cosines[-1]
    print(f"[bge] 余弦：min={lo:.4f}  中位={mid:.4f}  max={hi:.4f}")

    # 阈值取 0.99：同一模型同一文本，只差 float32 舍入，实测会到 0.9999+。
    # 只要池化或归一化口径不同，立刻掉到 0.9 以下 —— 两者差得很开，不存在灰区。
    if lo >= 0.99:
        print("✅ 一致：本地 BGE 与库里向量的口径相同，可以拿来跑检索")
        return 0
    print(
        "❌ 不一致：**别**用这个服务跑检索。\n"
        f"   最低一条只有 {lo:.4f}。库里那批向量不是这个口径生成的，\n"
        "   常见原因：池化方式不同（BGE 用 CLS 池化）或没做 L2 归一化。\n"
        "   继续用它会得到「不报错但不相关」的检索结果。"
    )
    return 1


# ═══════════════════════════════════════════════════════════════
# OpenAI 兼容服务
# ═══════════════════════════════════════════════════════════════


def serve(host: str, port: int) -> None:
    import uvicorn
    from fastapi import FastAPI

    model = _load_model()
    app = FastAPI(title="dev-bge", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "model": MODEL_NAME}

    @app.post("/v1/embeddings")
    def embeddings(body: EmbeddingsIn) -> dict[str, object]:
        texts = [body.input] if isinstance(body.input, str) else list(body.input)
        t0 = time.time()
        vectors = _encode(model, texts)
        # 响应必须逐条带 `index`：应用侧 `embed_batch` 是按 `index` 回填顺序的
        # （它明确不信任数组顺序 —— OpenAI 兼容实现允许乱序）。
        data = [
            {"object": "embedding", "index": i, "embedding": v}
            for i, v in enumerate(vectors)
        ]
        print(f"[bge] {len(texts)} 条 → {time.time() - t0:.2f}s", flush=True)
        return {
            "object": "list",
            "data": data,
            "model": body.model or MODEL_NAME,
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    print(f"[bge] 监听 http://{host}:{port}/v1  （EMBEDDING_BASE_URL 指这里）", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> int:
    parser = argparse.ArgumentParser(description="开发期本地 BGE 向量服务")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--serve", action="store_true", help="起 HTTP 服务")
    group.add_argument(
        "--selftest",
        action="store_true",
        help="与 Milvus 已存向量比对，确认口径一致（跑检索前必须先过这一关）",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9997)
    parser.add_argument("--sample", type=int, default=8, help="自检抽样条数")
    args = parser.parse_args()

    if args.selftest:
        return selftest(args.sample)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
