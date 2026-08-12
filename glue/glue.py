"""
Glue（胶水层）：仅做 any-auto-register 与 chatgpt2api 之间的接口适配。
两个原版项目代码均不修改，本服务只负责把 aar 注册成功的账号转发入库 c2a。

两种同步方式：
1. 被动接收：aar 开启 contribution_mode=custom 后，注册成功会自动 POST /api/upload 到这里。
   但 aar 原版 sync_account 要求账号必须有 refresh_token 才会推送；api 协议（无浏览器）
   模式注册的账号 refresh_token 常为空，因此 aar 不会调用本接口。
2. 主动同步（兜底）：后台定时从 aar 的 /api/accounts 拉取 status=registered 的账号，
   用其 access_token（token 字段）兜底 refresh_token，转发入库 c2a。无论 aar 注册模式
   如何，账号都会自动进入 c2a 账号池。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request

AAR_BASE_URL = os.getenv("AAR_BASE_URL", "http://any-auto-register:8000").rstrip("/")
C2A_BASE_URL = os.getenv("C2A_BASE_URL", "http://chatgpt2api:80").rstrip("/")
C2A_AUTH_KEY = os.getenv("CHATGPT2API_AUTH_KEY", "test_key_123")
GLUE_TOKEN = os.getenv("GLUE_TOKEN", "glue-shared-secret")
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "30"))  # 主动同步轮询间隔（秒）

app = FastAPI(title="c2a-glue")

# 已推送成功的 email 集合（去重，避免重复入库）
_synced: set[str] = set()
_synced_lock = threading.Lock()


def _post_c2a_accounts(payload_accounts: list[dict[str, Any]]) -> dict[str, Any]:
    """把一批账号转发给 chatgpt2api 的 POST /api/accounts。"""
    resp = httpx.post(
        f"{C2A_BASE_URL}/api/accounts",
        json={"accounts": payload_accounts, "sync_after_import": True},
        headers={"Authorization": f"Bearer {C2A_AUTH_KEY}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _push_one(email: str, access_token: str, refresh_token: str | None) -> bool:
    """推送单个账号到 c2a。refresh_token 为空时用 access_token 兜底。返回是否成功。"""
    rt = (refresh_token or "").strip() or (access_token or "").strip()
    at = (access_token or "").strip()
    if not at or not email:
        return False
    payload = [{
        "email": email,
        "access_token": at,
        "refresh_token": rt,
        "type": "chatgpt",
    }]
    data = _post_c2a_accounts(payload)
    added = data.get("added", 0)
    print(f"[glue] push {email}: added={added} resp={data}")
    return added >= 1


def _pull_aar_accounts() -> list[dict[str, Any]]:
    """从 aar 拉取已注册账号。"""
    resp = httpx.get(
        f"{AAR_BASE_URL}/api/accounts",
        params={"page": 1, "page_size": 200},
        timeout=30,
    )
    resp.raise_for_status()
    d = resp.json()
    return d.get("items") or d.get("accounts") or []


def sync_once() -> int:
    """主动同步一轮：拉 aar registered 账号 → 推 c2a（去重）。返回本次新增数。"""
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
        # extra_json 里可能也有 refresh_token
        extra = acc.get("extra_json") or {}
        if isinstance(extra, str):
            try:
                import json
                extra = json.loads(extra)
            except Exception:
                extra = {}
        refresh_token = (extra.get("refresh_token") or "").strip() if isinstance(extra, dict) else ""
        if _push_one(email, access_token, refresh_token):
            with _synced_lock:
                _synced.add(email)
            pushed += 1
    return pushed


def _background_sync():
    """后台定时同步线程。"""
    while True:
        try:
            n = sync_once()
            if n:
                print(f"[glue] background sync pushed {n} account(s)")
        except Exception as e:
            print(f"[glue] background sync error: {e}")
        time.sleep(SYNC_INTERVAL)


@app.on_event("startup")
def _start_sync_thread():
    t = threading.Thread(target=_background_sync, daemon=True)
    t.start()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/sync-now")
def sync_now():
    """手动触发一次主动同步。"""
    n = sync_once()
    return {"ok": True, "pushed": n}


@app.post("/api/upload")
async def api_upload(request: Request):
    """
    被动接收：aar contribution_mode=custom 钩子推送的账号。
    校验 Bearer GLUE_TOKEN，转换成 c2a 的 POST /api/accounts 格式并转发。
    """
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
    payload = [{
        "email": email,
        "access_token": (access_token or "").strip(),
        "refresh_token": rt,
        "type": "chatgpt",
    }]
    try:
        data = _post_c2a_accounts(payload)
        with _synced_lock:
            _synced.add(email)
        return {"ok": True, "added": data.get("added", 0), "detail": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 502
