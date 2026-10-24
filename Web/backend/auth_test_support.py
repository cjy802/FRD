"""测试共享：给 TestClient 配置固定管理员账号并登录，穿过会话门禁。"""

import os

import auth

_TEST_ADMIN = {"ADMIN_USERNAME": "tester", "ADMIN_PASSWORD": "tester-pass", "SESSION_SECRET": "unit-test-session-secret"}


def login_admin(client) -> None:
    for key, value in _TEST_ADMIN.items():
        os.environ.setdefault(key, value)
    auth.reset_for_tests()
    response = client.post("/api/auth/login", json={"username": _TEST_ADMIN["ADMIN_USERNAME"], "password": _TEST_ADMIN["ADMIN_PASSWORD"]})
    assert response.status_code == 200, f"测试登录失败：{response.status_code} {response.text}"
