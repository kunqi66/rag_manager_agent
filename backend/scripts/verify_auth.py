"""be-01 认证的验收脚本（spec `P1-07-be-auth` §8 的可执行版本）。

跑法（项目根目录）：

    uv run python -m backend.scripts.verify_auth

覆盖 A1（登录链路 / 防爆破 / 权限差异）、A4（`effective_depts` 含全部祖先）、
A5（登录态 / 停用账号 / Redis 不可用）与安全专项。

⚠️ 两点说明：

1. 会**真的**连 `.env` 里的 MySQL 与 Redis（开发期直连 ECS），
   用的是 `seed_org.py` 灌的种子账号（密码 `Rag@12345`）。
2. 防爆破用例会短暂锁定 `chenjing` / `nosuchuser_zzz`，
   **脚本收尾会主动清掉**这些键，不会把种子账号留在锁定状态。

Windows 控制台是 GBK —— 输出一律 ASCII，断言布尔值，不靠肉眼读中文。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

import main
from backend.clients import redis_client
from backend.core import security
from backend.core.db import engine
from backend.core.settings import settings
from backend.service.session_store import (
    fail_key,
    lock_key,
    session_key,
)

PASSWORD = "Rag@12345"
ACCESS_TTL = settings.access_token_expire_minutes * 60

# 防爆破用例会碰的账号；收尾要清干净，否则下次跑会误判
TOUCHED = ("admin", "chenjing", "cost01", "plat04", "nosuchuser_zzz")

# 🔴 Windows 控制台默认是 GBK —— 本脚本的提示里有 ⚠️ / ✅ 这类字符，
#    不重配的话 `print` 直接抛 UnicodeEncodeError，而它抛在**报 SKIP 的那一行**，
#    于是症状是「环境不可用时脚本崩了」，看起来像代码坏了。
#    `errors="replace"` 兜底：终端不支持 UTF-8 时显示成问号，不会崩。
#    （与 `verify_search.py` / `verify_import.py` / `verify_audit.py` 同一约定。）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 —— 重配失败不该让脚本起不来
        pass

_results: list[bool] = []


def check(passed: bool, label: str) -> None:
    _results.append(bool(passed))
    print(f"  [{'OK  ' if passed else 'FAIL'}] {label}")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: AsyncClient, username: str, password: str) -> tuple[int, dict]:
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    return resp.status_code, resp.json()


async def _env_ready() -> str | None:
    """环境自检：返回**不可用的原因**，全部可用时返回 None。

    ⚠️ 这个函数存在的唯一理由是「别让脚本崩在半路」。
       没有它的话，云端 MySQL/Redis 不可达时本脚本会甩出一大段 aiomysql
       连接栈然后退出 —— 从输出上**完全分不出**「环境没起来」和
       「代码被改坏了」，而这正是验收脚本最坏的失败模式
       （`verify_search.py` / `verify_audit.py` 的第一条纪律：
       `绝不把「环境不具备」伪装成「通过」`，反过来也一样：
       **绝不把「环境不具备」伪装成「失败」**）。
    """
    try:
        from sqlalchemy import text as _text

        async with engine.connect() as conn:
            await conn.execute(_text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        _banner(f"MySQL 不可达（{type(exc).__name__}）")
        return f"MySQL: {type(exc).__name__}"
    try:
        await redis_client.get_client().ping()
    except Exception as exc:  # noqa: BLE001
        _banner(f"Redis 不可达（{type(exc).__name__}）")
        return f"Redis: {type(exc).__name__}"
    return None


def _banner(reason: str) -> None:
    print("\n" + "=" * 56)
    print(f"SKIP：{reason}")
    print("本脚本的全部用例都需要 MySQL + Redis（开发期直连云端 ECS）。")
    print("⚠️ **本次结果不构成验收**，依赖恢复后重跑。")
    print("=" * 56)


async def run() -> bool | None:
    """返回 `(全部通过)`；**返回 `None` 表示环境不具备、一条都没跑**。

    ⚠️ 三态而不是二态：二态无法表达「没跑」，于是一次连接失败会被
       `_main` 报成「验收失败」，与真正的断言失败混在一起。
    """
    if await _env_ready() is not None:
        return None

    transport = ASGITransport(app=main.app)
    redis = redis_client.get_client()

    # 清掉上次可能的残留
    for username in TOUCHED:
        await redis.delete(fail_key(username), lock_key(username))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # ── 路由注册（Starlette 1.7 必须查 openapi，不能数 app.routes）──
        print("\n[routes]")
        paths = main.app.openapi()["paths"]
        check(
            all(
                p in paths
                for p in (
                    "/api/auth/login",
                    "/api/auth/refresh",
                    "/api/auth/logout",
                    "/api/auth/me",
                )
            ),
            "4 auth routes present in app.openapi()['paths']",
        )

        # ── A1：登录链路 login -> me -> refresh -> logout ──
        print("\n[A1 login chain]")
        status, body = await _login(client, "admin", PASSWORD)
        check(status == 200 and body["code"] == 0, "login(admin) -> 200/code 0")
        tokens = body["data"]
        access, refresh = tokens["access_token"], tokens["refresh_token"]
        check(
            tokens["token_type"] == "bearer" and tokens["expires_in"] == ACCESS_TTL,
            "token_type=bearer & expires_in matches settings",
        )

        resp = await client.get("/api/auth/me", headers=auth(access))
        body = resp.json()
        check(resp.status_code == 200 and body["code"] == 0, "me -> 200/code 0")
        me_admin = body["data"]
        check("password_hash" not in json.dumps(me_admin), "me response carries no password_hash")
        check(
            me_admin["department"]["id"] == "dept_01"
            and bool(me_admin["department"]["name"]),
            "me.department has id + name (display use)",
        )

        resp = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
        check(resp.json()["code"] == 0, "refresh -> code 0")
        access2 = resp.json()["data"]["access_token"]

        resp = await client.post(
            "/api/auth/logout",
            json={"refresh_token": refresh},
            headers=auth(access2),
        )
        check(resp.json()["code"] == 0, "logout -> code 0")

        resp = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
        check(
            resp.status_code == 401 and resp.json()["code"] == 2001,
            "revoked refresh rejected -> 2001/401",
        )

        # ── typ 断言：refresh 不能当 access 用 ──
        print("\n[typ assertion]")
        _, body = await _login(client, "admin", PASSWORD)
        refresh_only = body["data"]["refresh_token"]
        resp = await client.get("/api/auth/me", headers=auth(refresh_only))
        check(
            resp.status_code == 401 and resp.json()["code"] == 2001,
            "refresh token used as access -> 2001/401",
        )
        resp = await client.get(
            "/api/auth/me", headers=auth(body["data"]["access_token"] + "x")
        )
        check(
            resp.status_code == 401 and resp.json()["code"] == 2001,
            "tampered signature -> 2001/401",
        )

        # ── A1：不同账号的 perms 有差异 ──
        print("\n[A1 perms differ]")
        _, body = await _login(client, "cost01", PASSWORD)
        me_cost = (
            await client.get("/api/auth/me", headers=auth(body["data"]["access_token"]))
        ).json()["data"]
        check(
            len(me_admin["perms"]) > 0 and me_cost["perms"] == [],
            f"admin perms={len(me_admin['perms'])} vs staff perms=0",
        )

        # ── A4：effective_depts 含自身 + 全部祖先 ──
        print("\n[A4 effective_depts]")
        check(
            sorted(me_cost["depts"]) == ["dept_01", "dept_02", "dept_04"],
            f"cost01 depts has 3 levels (self+ancestors): {sorted(me_cost['depts'])}",
        )
        check(
            me_cost["department"]["id"] == "dept_04"
            and sorted(me_cost["depts"]) != [me_cost["department"]["id"]],
            "department (self only) != depts (with ancestors)",
        )

        # ── A4：dept_names 与 depts 同序同长（be-01 spec §10-86）──
        # 侧栏副标题「财务部 · 成本组」与检索范围 chip 都取 dept_names 的末两段。
        # 标签一律 ASCII —— Windows 控制台是 GBK，打中文会乱码（CLAUDE.md 已知坑），
        # 断言本身是布尔比较，Python 源码按 UTF-8 读，不受控制台编码影响。
        print("\n[A4 dept_names]")
        check(
            me_cost["dept_names"] == ["集团总部", "财务部", "成本组"],
            "cost01 dept_names == 3 names, root-first / self-last",
        )
        check(
            me_cost["dept_names"][-2:] == ["财务部", "成本组"],
            "cost01 dept_names[-2:] == parent + self (sidebar & chip source)",
        )
        # 🔴 对齐不变式，两条。守的是「名字与 id 错位」这个不报错、
        # 只静默显示错部门名的失效模式 —— 它没有异常、没有 500，只能靠断言抓。
        check(
            len(me_cost["dept_names"]) == len(me_cost["depts"]),
            f"len(dept_names) == len(depts) (both {len(me_cost['dept_names'])})",
        )
        check(
            me_cost["dept_names"][-1] == me_cost["department"]["name"],
            "dept_names[-1] == department.name (alignment invariant)",
        )
        # 单级边界：admin 只挂在根部门 dept_01 下，取末两段时不能越界、也不能多取。
        check(
            me_admin["dept_names"] == ["集团总部"] and len(me_admin["depts"]) == 1,
            f"admin single-level dept_names ok (len={len(me_admin['dept_names'])})",
        )

        # ── A5：无 token / 停用账号 / Redis 不可用 ──
        print("\n[A5 login state]")
        resp = await client.get("/api/auth/me")
        check(
            resp.status_code == 401 and resp.json()["code"] == 2001,
            "me without Authorization -> 2001/401",
        )
        check(
            resp.json()["code"] != 5000,
            "me without Authorization is NOT 5000 (HTTPBearer auto_error regression)",
        )

        status, body = await _login(client, "plat04", PASSWORD)
        check(status == 401 and body["code"] == 2001, "disabled account -> 2001/401")
        check(
            not await redis.exists(fail_key("plat04")),
            "disabled account does NOT increment fail counter",
        )

        # Redis 挂掉时必须 fail-closed（4001/502），不得降级放行
        dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=1)
        with patch("backend.core.deps.get_client", return_value=dead):
            status, body = await _login(client, "admin", PASSWORD)
        await dead.aclose()
        check(
            status == 502 and body["code"] == 4001,
            f"redis down -> 4001/502 fail-closed (got {status}/{body['code']})",
        )

        # ── 防枚举：用户不存在 vs 密码错误，状态码与文案完全一致 ──
        print("\n[anti-enumeration]")
        s1, b1 = await _login(client, "nosuchuser_zzz", "whatever")
        s2, b2 = await _login(client, "chenjing", "definitely-wrong")
        check(
            s1 == s2 == 401 and b1["message"] == b2["message"],
            "nonexistent user and wrong password are indistinguishable",
        )

        # ── A1：防爆破（第 6 次即使密码正确也拒）──
        print("\n[A1 brute-force lockout]")
        for _ in range(settings.login_max_failures):
            await _login(client, "chenjing", "wrong-password")
        status, body = await _login(client, "chenjing", PASSWORD)
        check(
            status == 401 and body["code"] == 2001,
            f"attempt #{settings.login_max_failures + 1} with CORRECT password -> 2001/401",
        )
        check(
            bool(await redis.exists(lock_key("chenjing"))),
            "lock key rag:auth:lock:chenjing present",
        )

        # 锁定检查必须先于查库：不存在的用户名被锁定后，也必须报「锁定」而非「密码错」
        for _ in range(settings.login_max_failures - 1):
            await _login(client, "nosuchuser_zzz", "x")
        check(
            bool(await redis.exists(lock_key("nosuchuser_zzz"))),
            "nonexistent username also reaches the lock threshold",
        )
        _, body = await _login(client, "nosuchuser_zzz", "x")
        check(
            body["message"] == "尝试次数过多，请稍后重试",
            "locked nonexistent user reports LOCK msg, not bad-credentials (lock check precedes DB)",
        )

        # 成功登录清计数
        status, _ = await _login(client, "admin", PASSWORD)
        check(
            status == 200
            and not await redis.exists(fail_key("admin"))
            and not await redis.exists(lock_key("admin")),
            "fail counter + lock cleared after successful login",
        )

        # ── 会话缓存未命中必须「重算」，不是「回退 JWT 载荷」──
        print("\n[session cache eviction]")
        _, body = await _login(client, "admin", PASSWORD)
        token = body["data"]["access_token"]
        await redis.delete(session_key("u_1001"))
        resp = await client.get("/api/auth/me", headers=auth(token))
        check(resp.json()["code"] == 0, "me still works after session cache evicted")
        ttl = await redis.ttl(session_key("u_1001"))
        check(
            0 < ttl <= ACCESS_TTL,
            f"backfilled session TTL is remaining token life, not a fresh {ACCESS_TTL}s (ttl={ttl})",
        )

        # ── 安全专项：假哈希与密码校验 ──
        print("\n[security: password hashing]")
        start = time.perf_counter()
        result = security.verify_password("anything", security.FAKE_PASSWORD_HASH)
        elapsed = time.perf_counter() - start
        check(
            result is False,
            "FAKE_PASSWORD_HASH verifies False (does NOT raise UnknownHashError)",
        )
        check(
            elapsed > 0.001,
            f"FAKE_PASSWORD_HASH path costs real Argon2 time (dt={elapsed:.4f}s)",
        )
        check(
            security.verify_password("x", "not-a-hash") is False,
            "malformed hash returns False (library exception does not leak)",
        )
        check(
            security.verify_password(PASSWORD, security.hash_password(PASSWORD)) is True
            and security.hash_password(PASSWORD) != security.hash_password(PASSWORD),
            "hash round-trips and is salted (two hashes of same input differ)",
        )

    # ── 收尾：清掉本脚本制造的锁，别把种子账号留在锁定状态 ──
    for username in TOUCHED:
        await redis.delete(fail_key(username), lock_key(username))

    return all(_results)


async def _main() -> int:
    try:
        passed = await run()
    finally:
        # 单事件循环内释放，避免连接交给 GC 时循环已关闭
        await redis_client.close_client()
        await engine.dispose()

    if passed is None:
        # 环境不具备 —— 退出码非 0（不能让人把「没跑」当成功），
        # 但上面的 SKIP 横幅已经把原因说清楚了。
        return 1
    total = len(_results)
    ok_count = sum(_results)
    print(f"\n{ok_count}/{total} checks passed")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
