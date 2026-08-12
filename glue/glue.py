"""
Glue（胶水层）：仅做 any-auto-register 与 chatgpt2api 之间的接口适配 + 自动注册调度。
两个原版项目代码均不修改，本服务只负责：
  1) 被动接收 aar contribution_mode=custom 钩子推送的账号 (/api/upload)
  2) 主动轮询同步 aar 已注册账号到 c2a（兜底 AT 模式 refresh_token 为空的情况）
  3) 自动注册调度：c2a 可用账号低于阈值时，自动调 aar 注册 n 个
  4) 流量感知：轮询 c2a 自带日志 API (/api/logs) 判断最近 m 分钟是否有调用

所有配置通过环境变量注入，不修改任一项目源码。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, Request

AAR_BASE_URL = os.getenv("AAR_BASE_URL", "http://any-auto-register:8000").rstrip("/")
C2A_BASE_URL = os.getenv("C2A_BASE_URL", "http://chatgpt2api:80").rstrip("/")
C2A_AUTH_KEY = os.getenv("CHATGPT2API_AUTH_KEY", "test_key_123")
GLUE_TOKEN = os.getenv("GLUE_TOKEN", "glue-shared-secret")

# 主动同步
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "30"))

# 自动注册调度
AUTO_REGISTER_ENABLED = os.getenv("AUTO_REGISTER_ENABLED", "false").lower() in ("1", "true", "yes", "on")
MIN_AVAILABLE = int(os.getenv("MIN_AVAILABLE", "3"))
REGISTER_BATCH = int(os.getenv("REGISTER_BATCH", "1"))
ONLY_ON_TRAFFIC = os.getenv("ONLY_ON_TRAFFIC", "false").lower() in ("1", "true", "yes", "on")
TRAFFIC_WINDOW_MIN = int(os.getenv("TRAFFIC_WINDOW_MIN", "10"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
AAR_REGISTER_CONCURRENCY = int(os.getenv("AAR_REGISTER_CONCURRENCY", "1"))
AAR_REGISTER_MODE = os.getenv("AAR_REGISTER_MODE", "").strip()  # 空=用 aar 全局配置

app = FastAPI(title="c2a-glue")

_synced: set[str] = set()
_synced_lock = threading.Lock()

# 流量探测（基于 c2a /api/logs 最近调用时间）
_last_traffic_ts: float = 0.0
_traffic_lock = threading.Lock()

# 自动注册防并发
_registering = False
_registering_lock = threading.Lock()

# aar 注册结果（供前端查看）
_last_register_info: dict[str, Any] = {}
_register_info_lock = threading.Lock()


# ---------------- c2a 转发 ----------------
def _post_c2a_accounts(payload_accounts: list[dict[str, Any]]) -> dict[str, Any]:
    resp = httpx.post(
        f"{C2A_BASE_URL}/api/accounts",
        json={"accounts": payload_accounts, "sync_after_import": True},
        headers={"Authorization": f"Bearer {C2A_AUTH_KEY}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _push_one(email: str, access_token: str, refresh_token: str | None) -> bool:
    rt = (refresh_token or "").strip() or (access_token or "").strip()
    at = (access_token or "").strip()
    if not at or not email:
        return False
    payload = [{"email": email, "access_token": at, "refresh_token": rt, "type": "chatgpt"}]
    data = _post_c2a_accounts(payload)
    print(f"[glue] push {email}: added={data.get('added', 0)}")
    return data.get("added", 0) >= 1


def _pull_aar_accounts() -> list[dict[str, Any]]:
    resp = httpx.get(f"{AAR_BASE_URL}/api/accounts", params={"page": 1, "page_size": 200}, timeout=30)
    resp.raise_for_status()
    d = resp.json()
    return d.get("items") or d.get("accounts") or []


def _c2a_available_count() -> int:
    """统计 c2a 中可用(backend_status=正常)账号数。"""
    try:
        resp = httpx.get(
            f"{C2A_BASE_URL}/api/accounts",
            params={"page": 1, "page_size": 500},
            headers={"Authorization": f"Bearer {C2A_AUTH_KEY}"},
            timeout=30,
        )
        resp.raise_for_status()
        d = resp.json()
        items = d.get("items") or d.get("accounts") or []
        n = 0
        for it in items:
            bs = str(it.get("backend_status") or "").strip()
            sc = str(it.get("status_category") or "").strip().lower()
            if bs == "正常" or sc == "normal" or sc == "active":
                n += 1
        return n
    except Exception as e:
        print(f"[glue] c2a available count failed: {e}")
        return 0


def _c2a_last_traffic_ts() -> float:
    """通过 c2a 自带日志 API 取最近一次调用时间（Unix 秒）。无日志返回 0。"""
    try:
        resp = httpx.get(
            f"{C2A_BASE_URL}/api/logs",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {C2A_AUTH_KEY}"},
            timeout=30,
        )
        resp.raise_for_status()
        d = resp.json()
        items = d.get("items") or []
        if not items:
            return 0.0
        t = items[0].get("time") or items[0].get("started_at") or ""
        if not t:
            return 0.0
        # 格式: 2026-08-12 14:12:36
        dt = datetime.strptime(t.strip(), "%Y-%m-%d %H:%M:%S")
        return dt.timestamp()
    except Exception as e:
        print(f"[glue] c2a last traffic failed: {e}")
        return 0.0


# ---------------- 同步 ----------------
def sync_once() -> int:
    try:
        accounts = _pull_aar_accounts()
    except Exception as e:
        print(f"[glue] pull aar failed: {e}")
        return 0
    pushed = 0
    with _synced_lock:
        known = set(_synced)
    for acc in accounts:
        email = (acc.get("email") or "").strip()
        status = (acc.get("status") or "").strip().lower()
        if not email or status != "registered":
            continue
        if email in known:
            continue
        access_token = (acc.get("token") or "").strip()
        extra = acc.get("extra_json") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        refresh_token = (extra.get("refresh_token") or "").strip() if isinstance(extra, dict) else ""
        if _push_one(email, access_token, refresh_token):
            with _synced_lock:
                _synced.add(email)
            pushed += 1
    return pushed


# ---------------- 自动注册 ----------------
def _trigger_aar_register(count: int) -> dict[str, Any]:
    """调 aar 原版注册 API 注册 count 个账号。返回结果摘要。"""
    extra: dict[str, Any] = {"chatgpt_registration_mode": "refresh_token"}
    if AAR_REGISTER_MODE:
        extra["chatgpt_registration_mode"] = AAR_REGISTER_MODE
    payload = {
        "platform": "chatgpt",
        "email": None,
        "password": None,
        "count": count,
        "concurrency": AAR_REGISTER_CONCURRENCY,
        "register_delay_seconds": 0,
        "proxy": None,
        "executor_type": "protocol",
        "captcha_solver": "local_solver",
        "extra": extra,
    }
    resp = httpx.post(f"{AAR_BASE_URL}/api/tasks/register", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _auto_register_check() -> dict[str, Any]:
    global _registering
    if not AUTO_REGISTER_ENABLED:
        return {"skipped": "disabled"}
    with _registering_lock:
        if _registering:
            return {"skipped": "already_running"}
        _registering = True
    try:
        available = _c2a_available_count()
        if available >= MIN_AVAILABLE:
            return {"action": "none", "available": available, "reason": f">= MIN_AVAILABLE({MIN_AVAILABLE})"}

        # 流量窗口判断（基于 c2a /api/logs 最近调用时间）
        if ONLY_ON_TRAFFIC:
            last = _c2a_last_traffic_ts()
            elapsed = (time.time() - last) / 60.0 if last else 999999.0
            if last == 0.0 or elapsed > TRAFFIC_WINDOW_MIN:
                return {
                    "action": "none",
                    "available": available,
                    "reason": f"no traffic in last {TRAFFIC_WINDOW_MIN}min (elapsed={elapsed:.1f})",
                }

        print(f"[glue] available={available} < MIN_AVAILABLE={MIN_AVAILABLE}, registering {REGISTER_BATCH}")
        try:
            res = _trigger_aar_register(REGISTER_BATCH)
            info = {"action": "register", "available": available, "batch": REGISTER_BATCH, "task": res}
        except Exception as e:
            info = {"action": "register_failed", "available": available, "error": str(e)}
        with _register_info_lock:
            _last_register_info.update(info)
            _last_register_info["ts"] = time.time()
        return info
    finally:
        with _registering_lock:
            _registering = False


# ---------------- 后台线程 ----------------
def _background_sync():
    while True:
        try:
            n = sync_once()
            if n:
                print(f"[glue] background sync pushed {n} account(s)")
        except Exception as e:
            print(f"[glue] background sync error: {e}")
        time.sleep(SYNC_INTERVAL)


def _background_auto_register():
    while True:
        try:
            _auto_register_check()
        except Exception as e:
            print(f"[glue] auto register error: {e}")
        time.sleep(CHECK_INTERVAL)


@app.on_event("startup")
def _start_threads():
    threading.Thread(target=_background_sync, daemon=True).start()
    if AUTO_REGISTER_ENABLED:
        threading.Thread(target=_background_auto_register, daemon=True).start()


# ---------------- 路由 ----------------
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/sync-now")
def sync_now():
    return {"ok": True, "pushed": sync_once()}


@app.get("/auto-register/status")
def auto_register_status():
    with _register_info_lock:
        info = dict(_last_register_info)
    return {
        "enabled": AUTO_REGISTER_ENABLED,
        "min_available": MIN_AVAILABLE,
        "register_batch": REGISTER_BATCH,
        "only_on_traffic": ONLY_ON_TRAFFIC,
        "traffic_window_min": TRAFFIC_WINDOW_MIN,
        "last_traffic_ts": _c2a_last_traffic_ts(),
        "available_now": _c2a_available_count(),
        "last_register": info,
    }


@app.post("/auto-register/trigger")
def auto_register_trigger():
    """手动触发一次自动注册检查。"""
    return _auto_register_check()


@app.post("/api/upload")
async def api_upload(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {GLUE_TOKEN}":
        return {"ok": False, "error": "unauthorized"}, 401
    body = await request.json()
    email = body.get("email")
    refresh_token = body.get("refresh_token") or ""
    access_token = body.get("access_token") or ""
    if not email or not (access_token or refresh_token):
        return {"ok": False, "error": "missing email/token"}, 400
    rt = (refresh_token or "").strip() or (access_token or "").strip()
    payload = [{"email": email, "access_token": (access_token or "").strip(), "refresh_token": rt, "type": "chatgpt"}]
    try:
        data = _post_c2a_accounts(payload)
        with _synced_lock:
            _synced.add(email)
        return {"ok": True, "added": data.get("added", 0), "detail": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 502
