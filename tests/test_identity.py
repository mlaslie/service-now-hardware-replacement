import pytest

from app import identity, servicenow


def test_bearer_ignores_service_jwts():
    assert identity.bearer({"authorization": "Bearer abc123"}) == "abc123"
    # x-serverless-authorization is the Discovery Engine agent, never a person.
    assert identity.bearer({"x-serverless-authorization": "Bearer a.b.c"}) is None
    assert identity.bearer({"authorization": "Bearer a.b.c"}) is None


def _jwt(payload):
    import base64
    import json
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(payload)}.sig"


def test_bearer_keeps_servicenow_jwts_but_not_googles():
    sn = _jwt({"iss": "https://example.service-now.com", "sub": "jane"})
    assert identity.bearer({"authorization": f"Bearer {sn}"}) == sn
    for iss in ("https://accounts.google.com", "accounts.google.com"):
        assert identity.bearer({"authorization": f"Bearer {_jwt({'iss': iss})}"}) is None


async def test_user_comes_from_servicenow(monkeypatch):
    async def fake_current_user(token):
        assert token == "tok"
        return {"sys_id": "u1", "email": "john.doe@example.com", "name": "John Doe", "user_name": "john.doe"}
    monkeypatch.setattr(servicenow, "current_user", fake_current_user)
    user = await identity.resolve_end_user("tok")
    assert user.verified and user.email == "john.doe@example.com" and user.name == "John Doe" and user.sys_id == "u1"


@pytest.mark.parametrize("exc,problem", [(servicenow.NotSignedIn(), "token_rejected"),
                                         (servicenow.Hibernating(), "servicenow_hibernating")])
async def test_rejected_token_is_anonymous(monkeypatch, exc, problem):
    async def fail(token):
        raise exc
    monkeypatch.setattr(servicenow, "current_user", fail)
    user = await identity.resolve_end_user("tok")
    assert not user.verified and user.problem == problem


async def test_no_token_is_anonymous():
    user = await identity.resolve_end_user(None)
    assert not user.verified and user.problem == "no_token"
