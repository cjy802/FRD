"""登录会话与角色门禁测试。"""

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import auth
import main


class AuthTestBase(unittest.TestCase):
    def setUp(self):
        self._env = {
            "ADMIN_USERNAME": "boss",
            "ADMIN_PASSWORD": "admin-pass-1",
            "FIELD_USERNAME": "field",
            "FIELD_PASSWORD": "field-pass-2",
            "SESSION_SECRET": "unit-test-secret",
        }
        self._patcher = mock.patch.dict(os.environ, self._env)
        self._patcher.start()
        auth.reset_for_tests()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(auth.reset_for_tests)
        self.client = TestClient(main.app, base_url="http://127.0.0.1:8011", client=("127.0.0.1", 50012))


class TestLogin(AuthTestBase):
    def test_admin_login_returns_role_and_cookie(self):
        response = self.client.post("/api/auth/login", json={"username": "boss", "password": "admin-pass-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"username": "boss", "role": "admin"})
        set_cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("samesite=lax", set_cookie.lower())
        self.assertIn(auth.SESSION_COOKIE, set_cookie)

    def test_field_login_returns_field_role(self):
        response = self.client.post("/api/auth/login", json={"username": "field", "password": "field-pass-2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "field")

    def test_wrong_password_is_rejected(self):
        response = self.client.post("/api/auth/login", json={"username": "boss", "password": "nope"})
        self.assertEqual(response.status_code, 401)

    def test_unknown_user_is_rejected(self):
        response = self.client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
        self.assertEqual(response.status_code, 401)

    def test_me_reports_session(self):
        self.client.post("/api/auth/login", json={"username": "field", "password": "field-pass-2"})
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"username": "field", "role": "field"})

    def test_logout_invalidates_session(self):
        self.client.post("/api/auth/login", json={"username": "boss", "password": "admin-pass-1"})
        self.client.post("/api/auth/logout")
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 401)


class TestApiGuard(AuthTestBase):
    def test_unauthenticated_api_call_is_401(self):
        response = self.client.get("/api/tasks")
        self.assertEqual(response.status_code, 401)

    def test_health_stays_public(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)

    def test_field_role_can_read_task_list(self):
        self.client.post("/api/auth/login", json={"username": "field", "password": "field-pass-2"})
        response = self.client.get("/api/tasks")
        self.assertEqual(response.status_code, 200)

    def test_field_role_cannot_mutate_tasks(self):
        self.client.post("/api/auth/login", json={"username": "field", "password": "field-pass-2"})
        response = self.client.patch("/api/tasks/20260101000000-00000000", json={})
        self.assertIn(response.status_code, (403, 404))

    def test_field_role_cannot_call_admin_weather(self):
        self.client.post("/api/auth/login", json={"username": "field", "password": "field-pass-2"})
        response = self.client.get("/api/weather/status")
        self.assertEqual(response.status_code, 403)

    def test_admin_role_passes_guard(self):
        self.client.post("/api/auth/login", json={"username": "boss", "password": "admin-pass-1"})
        response = self.client.get("/api/weather/status")
        self.assertIn(response.status_code, (200, 503))


class TestToken(unittest.TestCase):
    def test_token_roundtrip_and_expiry(self):
        with mock.patch.dict(os.environ, {"SESSION_SECRET": "s1"}):
            auth.reset_for_tests()
            self.addCleanup(auth.reset_for_tests)
            token = auth.issue_token("u1", "admin")
            parsed = auth.parse_token(token)
            self.assertEqual(parsed, {"username": "u1", "role": "admin"})
            expired = auth.issue_token("u1", "admin", max_age_seconds=-1)
            self.assertIsNone(auth.parse_token(expired))

    def test_tampered_token_is_rejected(self):
        with mock.patch.dict(os.environ, {"SESSION_SECRET": "s1"}):
            auth.reset_for_tests()
            self.addCleanup(auth.reset_for_tests)
            token = auth.issue_token("u1", "admin")
            self.assertIsNone(auth.parse_token(token[:-4] + "0000"))
            self.assertIsNone(auth.parse_token(None))


if __name__ == "__main__":
    unittest.main()
