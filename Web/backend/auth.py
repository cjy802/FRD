"""账号登录与会话。

双角色固定账号（管理员=主控工作台，使用者=现场处理端），凭证来自
backend/.env；签名 Cookie 会话，无第三方依赖。全部端点仅限本机部署，
跨站与跨域由 main.py 的回环/Origin 校验挡在会话层之前。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

BACKEND_ROOT = Path(__file__).resolve().parent
USER_DATA = BACKEND_ROOT / "user_data"

SESSION_COOKIE = "lowalt_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600

ROLES = ("admin", "field")
_ACCOUNT_PLACEHOLDERS = {"", "change-me", "changeme"}


def _configured_value(key: str) -> str | None:
    if value := os.environ.get(key):
        return value
    value = dotenv_values(BACKEND_ROOT / ".env").get(key)
    return value.strip() if isinstance(value, str) else None


def _secret_key() -> bytes:
    configured = _configured_value("SESSION_SECRET")
    if configured:
        return configured.encode("utf-8")
    # 未配置签名密钥时自动生成并落盘（user_data 不入库），重启不掉线。
    key_file = USER_DATA / "session_secret.key"
    try:
        if key_file.exists():
            stored = key_file.read_text(encoding="utf-8").strip()
            if stored:
                return stored.encode("utf-8")
        USER_DATA.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_hex(32)
        key_file.write_text(generated, encoding="utf-8")
        return generated.encode("utf-8")
    except OSError:
        # user_data 不可写时退回进程内密钥：重启后需重新登录。
        return secrets.token_hex(32).encode("utf-8")


def _bootstrap_accounts() -> dict[str, dict[str, str]]:
    """读固定账号；.env 未配置密码时首次生成随机密码并落盘，避免弱口令默认值。"""
    accounts: dict[str, dict[str, str]] = {}
    bootstrap_file = USER_DATA / "auth_credentials.json"
    generated: dict[str, str] = {}
    try:
        if bootstrap_file.exists():
            generated = json.loads(bootstrap_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        generated = {}
    for role, username_key, password_key in (("admin", "ADMIN_USERNAME", "ADMIN_PASSWORD"), ("field", "FIELD_USERNAME", "FIELD_PASSWORD")):
        username = _configured_value(username_key)
        password = _configured_value(password_key)
        if not username or not password or password in _ACCOUNT_PLACEHOLDERS:
            if f"{role}_password" not in generated:
                generated[f"{role}_password"] = secrets.token_urlsafe(6)
                print(f"[auth] {username_key}/{password_key} 未配置，已生成 {role} 账号随机密码：{generated[f'{role}_password']}")
        accounts[role] = {
            "username": username or role,
            "password": password if password and password not in _ACCOUNT_PLACEHOLDERS else generated[f"{role}_password"],
        }
    if any(f"{role}_password" in generated and not bootstrap_file.exists() for role in ROLES) or generated:
        try:
            USER_DATA.mkdir(parents=True, exist_ok=True)
            bootstrap_file.write_text(json.dumps(generated, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return accounts


_accounts: dict[str, dict[str, str]] | None = None
_secret: bytes | None = None


def _accounts_table() -> dict[str, dict[str, str]]:
    global _accounts
    if _accounts is None:
        _accounts = _bootstrap_accounts()
    return _accounts


def _signing_key() -> bytes:
    global _secret
    if _secret is None:
        _secret = _secret_key()
    return _secret


def verify_credentials(username: str, password: str) -> str | None:
    """返回账号角色；用户名或密码不匹配返回 None。"""
    for role, account in _accounts_table().items():
        if hmac.compare_digest(account["username"], username) and secrets.compare_digest(account["password"], password):
            return role
    return None


def issue_token(username: str, role: str, *, max_age_seconds: int = SESSION_MAX_AGE_SECONDS) -> str:
    payload = json.dumps({"u": username, "r": role, "exp": int(time.time()) + max_age_seconds}, separators=(",", ":"), ensure_ascii=False)
    body = payload.encode("utf-8")
    digest = hmac.new(_signing_key(), body, hashlib.sha256).hexdigest()
    import base64
    return base64.urlsafe_b64encode(body).decode("ascii") + "." + digest


def parse_token(token: str | None) -> dict[str, Any] | None:
    """校验签名与有效期；返回 {"username": …, "role": …} 或 None。"""
    if not token or token.count(".") != 1:
        return None
    import base64
    body_part, digest = token.split(".", 1)
    try:
        body = base64.urlsafe_b64decode(body_part.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return None
    expected = hmac.new(_signing_key(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("r") not in ROLES:
        return None
    if not isinstance(payload.get("exp"), int) or payload["exp"] < time.time():
        return None
    return {"username": str(payload.get("u", "")), "role": payload["r"]}


def session_from_cookie(cookie_value: str | None) -> dict[str, Any] | None:
    return parse_token(cookie_value)


def reset_for_tests() -> None:
    """测试里改环境变量后重置缓存。"""
    global _accounts, _secret
    _accounts = None
    _secret = None
