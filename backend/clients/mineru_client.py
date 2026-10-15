"""MinerU 官方 API 封装（PRD §6.4.1 / D17）。

链路：**申请上传地址 → PUT 文件 → 轮询任务 → 下载 full_zip_url → 安全解压取 `full.md`**。

🔴 MinerU 是**异步**的，且**只返回 zip 地址、不回正文**（CLAUDE.md 已记）。
   所以「拿 markdown」这件事天然是三段式，中间任何一段都可能失败 ——
   这也是 spec §5.2 第 F 步「解析一成功就回写 `source_markdown`」的由来：
   不落库，后面任何一段失败都会让重试无从下手（原始文件没有落盘）。

⚠️ **合规提示**：MinerU 走官方 API 意味着**文档会出境**（PRD R1 已登记为风险）。
   本模块只负责调用，风险处置不在代码层。

分层铁律：`httpx` / `zipfile` 的异常**一律不向上抛** ——
网络类转 `DependencyError`，内容类转 `UnsafePayloadError`（见 `core/exceptions.py`）。
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import httpx

from backend.core.exceptions import DependencyError, UnsafePayloadError
from backend.core.settings import settings

logger = logging.getLogger(__name__)

__all__ = [
    "parse_to_markdown",
    "submit_document",
    "fetch_markdown",
    "extract_full_md",
    "needs_mineru",
    "is_supported",
]

#: 需要走 MinerU 的扩展名；其余（md/txt）直接读原文（PRD §6.4.1）
_MINERU_EXTENSIONS = frozenset({"pdf", "png", "jpg", "jpeg", "docx", "pptx", "xlsx"})
#: 直接读原文的扩展名
_PLAIN_EXTENSIONS = frozenset({"md", "txt"})

#: MinerU 自己的任务态。**与 `kb_import_task.status` 是两套东西**，别混用：
#: 这套只决定「还要不要继续轮询」。
_STATE_DONE = "done"
_STATE_FAILED = "failed"
#: 终态以外的都在跑（pending / running / converting / waiting-file）
_TERMINAL_STATES = frozenset({_STATE_DONE, _STATE_FAILED})

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def needs_mineru(file_type: str) -> bool:
    """该扩展名是否必须走 MinerU。"""
    return file_type.lower().lstrip(".") in _MINERU_EXTENSIONS


def is_supported(file_type: str) -> bool:
    """扩展名是否在白名单内（api 层据此挡 1001）。"""
    ft = file_type.lower().lstrip(".")
    return ft in _MINERU_EXTENSIONS or ft in _PLAIN_EXTENSIONS


# ═══════════════════════════════════════════════════════════════
# zip 安全解压（红线 5）
# ═══════════════════════════════════════════════════════════════


def _assert_member_safe(info: zipfile.ZipInfo) -> None:
    """逐条校验一个 zip 条目，不安全即抛 `UnsafePayloadError`。

    四项检查缺一不可（spec §5.8）：
    - `..` 路径段 → 解压会写到目标目录**之外**（经典的 zip-slip）；
    - 绝对路径 / Windows 盘符 → 同上，且 `PurePosixPath.is_absolute()` 认不出 `C:\\`；
    - 符号链接 → 指向 `/etc/passwd` 这类外部文件，后续若按路径读会读到系统文件。

    ⚠️ 目录条目（以 `/` 结尾）本身无内容，直接放行 —— 但要**先**过 `..` 检查，
       否则 `../../` 这种纯目录条目会被当成无害目录放过。
    """
    name = info.filename or ""

    if _WINDOWS_DRIVE_RE.match(name):
        raise UnsafePayloadError(f"MinerU 压缩包含盘符路径，已拒绝：{name[:80]}")

    # 正反斜杠都当分隔符：zip 规范用 `/`，但恶意包可以塞 `..\\..\\`
    parts = [p for p in re.split(r"[\\/]+", name) if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafePayloadError(f"MinerU 压缩包含路径穿越条目，已拒绝：{name[:80]}")

    if name.startswith(("/", "\\")) or PurePosixPath(name).is_absolute():
        raise UnsafePayloadError(f"MinerU 压缩包含绝对路径条目，已拒绝：{name[:80]}")

    # zip 的 external_attr 高 16 位是 Unix st_mode；Windows 打的包这里是 0
    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        raise UnsafePayloadError(f"MinerU 压缩包含符号链接条目，已拒绝：{name[:80]}")


def _extract_full_md(zf: zipfile.ZipFile, workspace: Path) -> str:
    """解压并读出 `full.md`。

    🔴 **体积上限边解压边累加**（红线 5）。只看 `ZipInfo.file_size` 不够 ——
       那是压缩包头里**声明**的值，可以被伪造成 1 KB 而实际解出几十 GB。
       所以除了校验声明值，还要在 `read` 时按「剩余额度 + 1」读，
       一旦真的超了就中止。两道一起才叫上限。
    """
    budget = settings.zip_max_uncompressed_mb * 1024 * 1024
    consumed = 0
    target_entries: list[zipfile.ZipInfo] = []

    for info in zf.infolist():
        if info.is_dir():
            # 目录条目也要过安全检查（`../../` 目录同样危险），但不计体积
            _assert_member_safe(info)
            continue

        _assert_member_safe(info)

        if info.file_size > budget - consumed:
            raise UnsafePayloadError(
                f"MinerU 压缩包解压体积超过上限 {settings.zip_max_uncompressed_mb} MB，已中止"
            )
        consumed += info.file_size

        if Path(info.filename).name == "full.md":
            target_entries.append(info)

    if not target_entries:
        raise DependencyError("MinerU 返回的压缩包中没有 full.md")

    # 若有多个 full.md（嵌套目录），取路径最浅的
    target = min(target_entries, key=lambda i: len(PurePosixPath(i.filename).parts))

    # 真正读取时再卡一次实际字节数 —— 声明值可以是假的
    remaining = budget - (consumed - target.file_size)
    with zf.open(target) as src:
        raw = src.read(remaining + 1)
    if len(raw) > remaining:
        raise UnsafePayloadError("MinerU 返回的 full.md 实际体积超过上限，已中止")

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # MinerU 正常输出 UTF-8。这里不猜编码 —— 猜错会得到满篇乱码的正文，
        # 而且会一路写进 Milvus 变成永远检索不准的脏数据。
        raise DependencyError("MinerU 返回的 Markdown 不是 UTF-8 编码") from None


def extract_full_md(zip_bytes: bytes) -> str:
    """从 MinerU 的 zip 字节里安全取出 `full.md` 的正文。

    独立成公开函数是刻意的：它是**唯一可以脱离网络单测**的一环，
    而红线 5 的三类攻击（zip-slip / 盘符 / 符号链接）都发生在这里。
    """
    # 落到独立的临时目录，不落项目目录（spec §5.8 第 1 条）。
    # 本函数其实只在内存里读，不写盘 —— 留 tempdir 是为了将来要落盘时
    # 不必再改一遍所有调用点，且 `finally` 保证成功/失败都清干净。
    workspace = Path(tempfile.mkdtemp(prefix="mineru_"))
    try:
        # `io.BytesIO` 可 seek，满足 zipfile 的要求；全程只在内存里读，不落盘
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            return _extract_full_md(zf, workspace)
    except zipfile.BadZipFile as exc:
        # 也覆盖「声明大小被篡改导致 CRC 校验失败」—— 同样是可重试的传输问题
        raise DependencyError("MinerU 返回的不是合法压缩包") from exc
    finally:
        # 成功、失败、重试路径都要清（PRD §6.4.1）
        _rmtree_quiet(workspace)


def _rmtree_quiet(path: Path) -> None:
    """删临时目录，失败只告警不抛。

    清理失败不该把一个**已经成功**的导入任务变成失败 ——
    代价是磁盘上留一个空目录，运维能看到。
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        logger.warning("MinerU 临时目录清理失败：%s", path)


# ═══════════════════════════════════════════════════════════════
# 网络链路
# ═══════════════════════════════════════════════════════════════


def _headers() -> dict[str, str]:
    if not settings.mineru_api_token:
        raise DependencyError("未配置 MINERU_API_TOKEN")
    return {
        "Authorization": f"Bearer {settings.mineru_api_token}",
        "Content-Type": "application/json",
    }


def _wrap_http(exc: Exception, op: str) -> DependencyError:
    """httpx 异常 → `DependencyError`。**第三方异常类型绝不外泄**。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return DependencyError(f"MinerU {op} 失败：HTTP {exc.response.status_code}")
    if isinstance(exc, httpx.TimeoutException):
        return DependencyError(f"MinerU {op} 超时")
    return DependencyError(f"MinerU {op} 失败：网络异常")


async def _submit(client: httpx.AsyncClient, file_name: str, content: bytes) -> tuple[str, str]:
    """申请上传地址并 PUT 文件，返回 `(batch_id, 上传后的文件名)`。"""
    url = f"{settings.mineru_api_base}/file-urls/batch"
    payload = {
        "enable_formula": True,
        "language": "ch",
        "files": [{"name": file_name, "is_ocr": True}],
    }
    try:
        resp = await client.post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 —— 统一收敛
        raise _wrap_http(exc, "申请上传地址") from exc

    data = body.get("data") or {}
    batch_id = data.get("batch_id")
    urls = data.get("file_urls") or []
    if body.get("code") not in (0, None) or not batch_id or not urls:
        raise DependencyError(f"MinerU 申请上传地址失败：{body.get('msg') or '响应缺少 batch_id'}")

    # 上传地址是**预签名 URL**，不带 Authorization 头 ——
    # 带上反而可能因签名串不一致被拒
    try:
        put = await client.put(urls[0], content=content)
        put.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise _wrap_http(exc, "上传文件") from exc

    return batch_id, file_name


async def submit_document(file_name: str, content: bytes) -> str:
    """**只提交**，返回 MinerU 的 `batch_id`（存进 `kb_import_task.mineru_task_id`）。

    🔴 为什么提交放在**同步的受理阶段**、而不是 worker 里：
       原始文件在本项目里**没有任何持久化位置** —— `kb_document` 没有文件字段、
       MinIO 不在本模块设计内、api 与 worker 在 compose 里也没有共享卷。
       若把「提交」也交给 worker，worker 就需要文件字节，而字节只能经
       broker 传（200 MB 的 base64 会把 Redis 打爆）或者根本不传（任务必然失败）。
       ⇒ 受理时提交，把 MinerU 侧的 job_id 存库，worker 只需凭 id 取回结果。

    这个设计顺带解决了 #96 的大部分：worker 失败后重试可以**从已存库的 job_id
       继续轮询**，不必重新上传；而「提交失败」发生在同步阶段，会直接以 HTTP 错误
       返回给用户，**不会**留下一个必然失败的任务行。
    """
    timeout = httpx.Timeout(60.0, read=300.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        batch_id, uploaded_name = await _submit(client, file_name, content)
    logger.info("MinerU 已受理：file=%s batch_id=%s", file_name, batch_id)
    # 回传上传后的文件名（MinerU 可能改名），轮询时要拿它匹配结果条目
    return f"{batch_id}|{uploaded_name}"


async def fetch_markdown(job_id: str, file_name: str) -> str:
    """凭 `job_id`（`submit_document` 的返回值）轮询并取回 Markdown 正文。

    这是 worker 侧的入口，**不需要原始文件字节**。
    """
    batch_id, _, uploaded_name = job_id.partition("|")
    if not batch_id:
        raise DependencyError("导入任务的 MinerU job_id 非法")

    timeout = httpx.Timeout(60.0, read=300.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        zip_url = await _poll(client, batch_id, uploaded_name or file_name)
        zip_bytes = await _download(client, zip_url)
    return extract_full_md(zip_bytes)


async def _poll(client: httpx.AsyncClient, batch_id: str, file_name: str) -> str:
    """轮询到 `done`，返回 `full_zip_url`。

    退避：首次 3 s、翻倍、上限 30 s；总时长受 `mineru_poll_timeout_seconds` 约束
    （PRD §6.4.1：15 min）。**轮询间隔与 Celery 重试退避是两回事**，别混。
    """
    url = f"{settings.mineru_api_base}/extract-results/batch/{batch_id}"
    interval = settings.mineru_poll_interval_initial_seconds
    waited = 0.0

    while waited < settings.mineru_poll_timeout_seconds:
        await asyncio.sleep(interval)
        waited += interval
        interval = min(interval * 2, settings.mineru_poll_interval_max_seconds)

        try:
            resp = await client.get(url, headers=_headers())
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise _wrap_http(exc, "轮询解析结果") from exc

        results = (body.get("data") or {}).get("extract_result") or []
        item = next(
            (r for r in results if r.get("file_name") == file_name),
            results[0] if results else None,
        )
        if item is None:
            continue

        state = item.get("state")
        if state == _STATE_DONE:
            zip_url = item.get("full_zip_url")
            if not zip_url:
                raise DependencyError("MinerU 任务已完成但未返回 full_zip_url")
            return zip_url
        if state == _STATE_FAILED:
            # 解析失败是**内容问题**（格式损坏等），重试无用
            raise UnsafePayloadError(
                f"MinerU 解析失败：{item.get('err_msg') or '未知原因'}"
            )

    raise DependencyError(f"MinerU 解析超时（{settings.mineru_poll_timeout_seconds} 秒）")


async def _download(client: httpx.AsyncClient, zip_url: str) -> bytes:
    try:
        resp = await client.get(zip_url)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise _wrap_http(exc, "下载解析结果") from exc
    return resp.content


async def parse_to_markdown(file_name: str, content: bytes) -> str:
    """提交并等待结果，一步到位。

    ⚠️ **生产链路不走这个函数** —— 它只是为了脚本 / 冒烟测试方便。
       真实链路是「受理时 `submit_document` → worker 里 `fetch_markdown`」，
       中间隔着一次落库，这样 worker 才不需要原始字节（见 `submit_document`）。
    """
    job_id = await submit_document(file_name, content)
    return await fetch_markdown(job_id, file_name)
