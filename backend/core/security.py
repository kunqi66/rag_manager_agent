"""JWT 签发/校验 与 密码哈希（be-03 安全依赖）。

🔴 本文件是**全项目唯一**允许 `import jwt` / `from pwdlib import PasswordHash`
的地方（be-03 §7.2）。别处出现这两个 import 即视为缺陷，用 grep 卡住：

    grep -rn "import jwt\\|from pwdlib" backend/ | grep -v core/security.py

纯基础设施，**零业务依赖** —— 只准 import `settings` 与 `exceptions`（be-03 §2）。
不要让它去读数据库或业务 schema，那会让「签个 token」耦合上整个应用。
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from backend.core.exceptions import UnauthorizedError
from backend.core.settings import settings

# Argon2id，库推荐的参数。**不自己拼 salt、不指定弱参数**（be-03 §4.1）。
_hasher = PasswordHash.recommended()

# ═══════════════════════════════════════════════════════════════
# 防用户名枚举用的固定假哈希（be-01 spec §10-82）
# ═══════════════════════════════════════════════════════════════
# 「用户不存在」时必须对**某个**哈希跑一次完整 Argon2，否则响应时间会明显快于
# 「用户存在但密码错」，攻击者靠时间差就能枚举账号。
#
# 🔴 它必须是**合法**的 Argon2id 串。2026-10-14 实测 pwdlib 0.3.1：
#     对 'not-a-hash' / '' / '$argon2id$bogus' 一律 **抛 UnknownHashError**，
#     并不像早先推测的那样返回 False。
#   两条退化路径都比原设想严重：
#     ① 不接异常 → 用户名不存在时登录接口 500 而非 401，攻击者靠 500/401 差异**更容易**枚举；
#     ② 接住返回 False → 非法串根本不做 Argon2 运算，瞬间返回，
#        真实用户却要走完整 Argon2，**时间差把枚举口子重新打开**。
#
# 因此在导入时对一段随机串现算一次，请求路径上**永不重算**。
# 随机串只存在于本进程内存里，谁也不知道它的原文，所以对任何输入都返回 False。
_FAKE_PASSWORD = secrets.token_urlsafe(32)
FAKE_PASSWORD_HASH: str = _hasher.hash(_FAKE_PASSWORD)
del _FAKE_PASSWORD


def hash_password(raw: str) -> str:
    """算出可入库的 Argon2 哈希串。"""
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    """校验密码。

    恒定返回 `bool`（be-03 §3.2 的契约）—— 哈希串损坏时返回 `False` 而**不是**抛库异常。
    不接 `UnknownHashError` 会让登录接口在遇到脏数据时 500，把内部状态泄漏出去。
    """
    try:
        return _hasher.verify(raw, hashed)
    except UnknownHashError:
        return False


def make_jti() -> str:
    """生成 refresh token 的唯一标识，用于实现主动吊销白名单。"""
    return uuid.uuid4().hex


def _encode(payload: dict[str, Any], lifetime: timedelta) -> tuple[str, datetime]:
    """统一签发：补 `iat` / `exp` 后编码，返回 `(token, 过期时刻)`。"""
    now = datetime.now(timezone.utc)
    expires_at = now + lifetime
    body = {**payload, "iat": now, "exp": expires_at}
    token = jwt.encode(body, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def create_access_token(
    *,
    user_id: str,
    username: str,
    depts: list[str],
    roles: list[str],
    perms: list[str],
) -> tuple[str, datetime]:
    """签发 access token。返回 `(token, exp)`。

    ⚠️ `depts` 必须传 **effective_depts（含全部祖先）**，不是「所属部门」。
    """
    return _encode(
        {
            "sub": user_id,
            "username": username,
            "depts": list(depts),
            "roles": list(roles),
            "perms": list(perms),
            # `typ` 不在 PRD §8.1 的两张载荷表里，但**必须有**：
            # refresh 的 `sub` 也是合法 user_id、签名也对，缺了类型声明
            # 就会让 refresh token 被当作 access 用（be-03 §10-38）。
            "typ": "access",
        },
        timedelta(minutes=settings.access_token_expire_minutes),
    )


def create_refresh_token(*, user_id: str, jti: str) -> tuple[str, datetime]:
    """签发 refresh token。返回 `(token, exp)`。"""
    return _encode(
        {"sub": user_id, "jti": jti, "typ": "refresh"},
        timedelta(days=settings.refresh_token_expire_days),
    )


def decode_token(token: str, *, expect: Literal["access", "refresh"]) -> dict[str, Any]:
    """解码并校验 token，返回载荷。

    校验签名 / 过期 / **类型**三者，任一不过抛 `UnauthorizedError`。
    第三方异常在此收敛，**不向上层泄漏 `jwt.*` 类型**（分层铁律）。
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        # 必须先于 InvalidTokenError 捕获 —— 它是后者的子类
        raise UnauthorizedError("登录已过期，请重新登录") from None
    except jwt.InvalidTokenError:
        raise UnauthorizedError("登录凭证无效，请重新登录") from None

    if payload.get("typ") != expect:
        # 拿到 refresh 当 access 用（或反之）在这里被挡住
        raise UnauthorizedError("登录凭证类型不符")

    return payload
