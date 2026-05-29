"""Tests for the auth flows: signup, login, logout, password reset."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------


class TestSignup:
    def test_signup_with_valid_data(self, client):
        resp = client.post(
            "/api/auth/signup",
            json={"email": "new@example.com", "password": "SuperSecret1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["email"] == "new@example.com"
        assert body["subscription_tier"] == "free"

    def test_signup_duplicate_email_400(self, client):
        client.post(
            "/api/auth/signup",
            json={"email": "dup@example.com", "password": "SuperSecret1"},
        )
        resp = client.post(
            "/api/auth/signup",
            json={"email": "dup@example.com", "password": "SuperSecret1"},
        )
        assert resp.status_code == 400

    def test_signup_weak_password_400(self, client):
        resp = client.post(
            "/api/auth/signup",
            json={"email": "weakpw@example.com", "password": "short"},
        )
        assert resp.status_code == 400

    def test_signup_invalid_email_400(self, client):
        resp = client.post(
            "/api/auth/signup",
            json={"email": "notanemail", "password": "SuperSecret1"},
        )
        assert resp.status_code == 400

    def test_signup_normalizes_email(self, client):
        resp = client.post(
            "/api/auth/signup",
            json={"email": "MixedCase@Example.COM", "password": "SuperSecret1"},
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "mixedcase@example.com"


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestLogin:
    def test_login_with_correct_credentials(self, client, user_factory):
        user_factory(email="login@example.com", password="SuperSecret1")
        resp = client.post(
            "/api/auth/login",
            json={"email": "login@example.com", "password": "SuperSecret1"},
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "login@example.com"

    def test_login_sets_session(self, client, user_factory):
        user_factory(email="sess@example.com", password="SuperSecret1")
        client.post(
            "/api/auth/login",
            json={"email": "sess@example.com", "password": "SuperSecret1"},
        )
        # /api/auth/me requires session
        me = client.get("/api/auth/me")
        assert me.status_code == 200

    def test_login_wrong_password_401(self, client, user_factory):
        user_factory(email="badpw@example.com", password="SuperSecret1")
        resp = client.post(
            "/api/auth/login",
            json={"email": "badpw@example.com", "password": "wrong-one"},
        )
        assert resp.status_code == 401

    def test_login_nonexistent_email_401(self, client):
        resp = client.post(
            "/api/auth/login",
            json={"email": "noone@example.com", "password": "SuperSecret1"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    def test_logout_clears_session(self, authenticated_client):
        client, _ = authenticated_client
        # Confirm logged in first
        assert client.get("/api/auth/me").status_code == 200
        # Log out — empty JSON body so CSRF middleware treats it as same-origin JSON
        resp = client.post("/api/auth/logout", json={})
        assert resp.status_code == 200
        # Now /api/auth/me should be 401
        assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# Auth-required routes
# ---------------------------------------------------------------------------


class TestRouteProtection:
    def test_me_requires_auth(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_transactions_requires_auth(self, client):
        resp = client.get("/api/transactions")
        assert resp.status_code == 401

    def test_settings_requires_auth(self, client):
        resp = client.get("/api/settings")
        assert resp.status_code == 401

    def test_dashboard_html_redirects_to_login(self, client):
        resp = client.get("/dashboard", follow_redirects=False)
        # Should redirect to /login
        assert resp.status_code in (302, 303, 307)
        assert "/login" in resp.headers.get("location", "")


# ---------------------------------------------------------------------------
# Password reset — token issuance, redemption, expiration
# ---------------------------------------------------------------------------


class TestPasswordReset:
    def test_forgot_for_existing_email_creates_token(self, client, user_factory, db_session):
        from backend.database import PasswordResetToken
        user_factory(email="forgot@example.com", password="SuperSecret1")
        resp = client.post(
            "/api/auth/forgot", json={"email": "forgot@example.com"},
        )
        assert resp.status_code == 200
        tokens = db_session.query(PasswordResetToken).all()
        assert len(tokens) == 1

    def test_forgot_for_unknown_email_returns_ok_no_token(self, client, db_session):
        from backend.database import PasswordResetToken
        # Should NOT leak account existence — returns ok with no token created
        resp = client.post(
            "/api/auth/forgot", json={"email": "unknown@example.com"},
        )
        assert resp.status_code == 200
        tokens = db_session.query(PasswordResetToken).all()
        assert len(tokens) == 0

    def test_reset_with_valid_token_updates_password(self, client, user_factory, db_session):
        from backend.database import PasswordResetToken
        user = user_factory(email="resetok@example.com", password="OldPassword1")
        # Issue token
        client.post("/api/auth/forgot", json={"email": "resetok@example.com"})
        tok = db_session.query(PasswordResetToken).first()
        # Redeem it
        resp = client.post(
            "/api/auth/reset",
            json={"token": tok.token, "new_password": "NewPassword2"},
        )
        assert resp.status_code == 200
        # Old password should NOT log in
        bad = client.post(
            "/api/auth/login",
            json={"email": "resetok@example.com", "password": "OldPassword1"},
        )
        assert bad.status_code == 401
        # New password DOES work
        good = client.post(
            "/api/auth/login",
            json={"email": "resetok@example.com", "password": "NewPassword2"},
        )
        assert good.status_code == 200

    def test_reset_with_invalid_token_400(self, client):
        resp = client.post(
            "/api/auth/reset",
            json={"token": "bogus-token", "new_password": "NewPassword1"},
        )
        assert resp.status_code == 400

    def test_reset_with_used_token_400(self, client, user_factory, db_session):
        from backend.database import PasswordResetToken
        user_factory(email="used@example.com", password="OldPassword1")
        client.post("/api/auth/forgot", json={"email": "used@example.com"})
        tok = db_session.query(PasswordResetToken).first()
        # First redemption
        client.post(
            "/api/auth/reset",
            json={"token": tok.token, "new_password": "NewPassword2"},
        )
        # Second redemption should fail
        resp = client.post(
            "/api/auth/reset",
            json={"token": tok.token, "new_password": "Another1234"},
        )
        assert resp.status_code == 400

    def test_reset_with_expired_token_400(self, client, user_factory, db_session):
        from backend.database import PasswordResetToken
        user_factory(email="exp@example.com", password="OldPassword1")
        client.post("/api/auth/forgot", json={"email": "exp@example.com"})
        tok = db_session.query(PasswordResetToken).first()
        # Force expiration
        tok.expires_at = datetime.utcnow() - timedelta(hours=2)
        db_session.commit()
        resp = client.post(
            "/api/auth/reset",
            json={"token": tok.token, "new_password": "NewPassword2"},
        )
        assert resp.status_code == 400

    def test_reset_weak_password_400(self, client, user_factory, db_session):
        from backend.database import PasswordResetToken
        user_factory(email="weak@example.com", password="OldPassword1")
        client.post("/api/auth/forgot", json={"email": "weak@example.com"})
        tok = db_session.query(PasswordResetToken).first()
        resp = client.post(
            "/api/auth/reset",
            json={"token": tok.token, "new_password": "short"},
        )
        assert resp.status_code == 400
