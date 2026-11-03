from http.cookies import SimpleCookie
from uuid import uuid4

import pytest
from fastapi import Response
from pydantic import SecretStr

from app.auth.dependencies import set_session_cookie
from app.auth.schemas import UserView
from app.auth.service import COOKIE_NAME, new_session
from app.core.config import Settings


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("authenticated", [False, True], ids=["anonymous", "user"])
def test_session_cookie_security_and_lifetime(scheme: str, authenticated: bool) -> None:
    settings = Settings(
        public_origin=f"{scheme}://foxtube.example",
        db_password=SecretStr("disposable-unit-test-secret"),
    )
    user = (
        UserView(
            id=uuid4(),
            email="author@example.com",
            display_name="Автор",
            role="user",
            channel_handle="fox-author",
        )
        if authenticated
        else None
    )
    state = new_session(user)
    response = Response()
    set_session_cookie(response, state, settings)
    cookie: SimpleCookie[str] = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    assert COOKIE_NAME in cookie
    morsel = cookie[COOKIE_NAME]
    assert bool(morsel["secure"]) is (scheme == "https")
    assert bool(morsel["httponly"])
    assert morsel["samesite"].casefold() == "lax"
    assert morsel["path"] == "/"
    assert not morsel["domain"]
    assert int(morsel["max-age"]) == (7 * 86400 if authenticated else 1800)
    assert morsel["expires"]
