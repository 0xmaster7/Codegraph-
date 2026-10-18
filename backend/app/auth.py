"""Single-owner hosted access. Local development remains disabled by default."""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings

COOKIE = "__Host-codegraph-session"
SESSION_SECONDS = 86_400


def password_digest(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def issue_session(key: str, *, now: int | None = None) -> str:
    payload = (
        f"{(int(time.time()) if now is None else now) + SESSION_SECONDS}.{secrets.token_hex(16)}"
    )
    signature = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def valid_session(value: str, key: str, *, now: int | None = None) -> bool:
    if len(value) > 256:
        return False
    try:
        expires, nonce, signature = value.split(".")
        deadline = int(expires)
        current = int(time.time()) if now is None else now
        if not current < deadline <= current + SESSION_SECONDS or len(nonce) != 32:
            return False
        expected = hmac.new(key.encode(), f"{expires}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def install_auth(app: FastAPI, settings: Settings) -> None:
    enabled = settings.hosted_auth_enabled
    key = settings.hosted_session_secret.get_secret_value() if enabled else ""
    failures: dict[str, deque[float]] = defaultdict(deque)

    def authenticated(request: Request) -> bool:
        return not enabled or valid_session(request.cookies.get(COOKIE, ""), key)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if enabled:
            if request.method not in {"GET", "HEAD", "OPTIONS"} and (
                request.headers.get("origin") != settings.public_app_origin
            ):
                return JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)
            if request.url.path not in {"/health", "/api/auth/session"} and not authenticated(
                request
            ):
                return JSONResponse({"detail": "Sign in to continue."}, status_code=401)
        response = await call_next(request)
        if enabled:
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/auth/session")
    def session(request: Request) -> dict[str, bool]:
        return {"enabled": enabled, "authenticated": authenticated(request)}

    @app.post("/api/auth/session")
    async def login(request: Request) -> JSONResponse:
        if not enabled:
            return JSONResponse({"enabled": False, "authenticated": True})
        # One worker owns this bounded window. Authentication is never cached by the CDN.
        current = time.monotonic()
        for address in list(failures):
            while failures[address] and failures[address][0] < current - 300:
                failures[address].popleft()
            if not failures[address]:
                del failures[address]
        client = request.client.host if request.client else "unknown"
        if len(failures[client]) >= 10 or sum(map(len, failures.values())) >= 100:
            return JSONResponse(
                {"detail": "Too many attempts. Try again in five minutes."},
                status_code=429,
                headers={"Retry-After": "300"},
            )
        raw = await request.body()
        if len(raw) > 4096:
            return JSONResponse({"detail": "Request is too large."}, status_code=413)
        try:
            import json

            password = json.loads(raw).get("password", "")
            if not isinstance(password, str) or not 1 <= len(password) <= 256:
                raise ValueError("Invalid password")
            salt, expected = settings.hosted_password_hash.get_secret_value().split(":")
            accepted = hmac.compare_digest(password_digest(password, salt), expected)
        except (ValueError, AttributeError, TypeError):
            accepted = False
        if not accepted:
            failures[client].append(current)
            return JSONResponse({"detail": "Incorrect password."}, status_code=401)
        failures.pop(client, None)
        response = JSONResponse({"enabled": True, "authenticated": True})
        response.set_cookie(
            COOKIE,
            issue_session(key),
            max_age=SESSION_SECONDS,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.delete("/api/auth/session")
    def logout() -> JSONResponse:
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return response
