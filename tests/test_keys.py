"""Users' own tool keys: stored encrypted, never mixed up between users, checked against the tool before saving."""
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.services.lead_finder import keys, runner


class FakeQuery:
    def __init__(self, rows):
        self.rows, self.filters, self.op, self.payload = rows, [], "select", None

    def select(self, *_):
        self.op = "select"
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def upsert(self, row, on_conflict=None):
        self.op, self.payload = "upsert", (row, on_conflict)
        return self

    def delete(self):
        self.op = "delete"
        return self

    def execute(self):
        match = lambda r: all(r.get(k) == v for k, v in self.filters)  # noqa: E731
        if self.op == "select":
            return SimpleNamespace(data=[dict(r) for r in self.rows if match(r)])
        if self.op == "upsert":
            row, conflict = self.payload
            cols = conflict.split(",")
            self.rows[:] = [r for r in self.rows if not all(r[c] == row[c] for c in cols)] + [dict(row)]
            return SimpleNamespace(data=[row])
        self.rows[:] = [r for r in self.rows if not match(r)]
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self):
        self.rows: list[dict] = []

    def table(self, _name):
        return FakeQuery(self.rows)


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(keys, "_db", lambda: fake)
    monkeypatch.setattr(settings, "KEYS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return fake


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------- Encryption and storage ----------

def test_key_is_encrypted_and_round_trips(db):
    secret = "sk-live-1234567890abcdef"
    keys.save("user-a", "hunter", secret)
    stored = db.rows[0]
    assert secret not in stored["key_encrypted"]
    assert stored["key_last4"] == "cdef"
    assert keys.load("user-a")["hunter"]["key"] == secret


def test_key_encrypted_with_another_secret_is_unreadable(db, monkeypatch):
    keys.save("user-a", "hunter", "sk-live-1234567890abcdef")
    monkeypatch.setattr(settings, "KEYS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert keys.load("user-a") == {}


def test_each_user_only_gets_their_own_keys(db):
    keys.save("user-a", "hunter", "AAAA-hunter-key-1111")
    keys.save("user-b", "hunter", "BBBB-hunter-key-2222")
    keys.save("user-b", "reoon", "BBBB-reoon-key-3333")
    assert keys.load("user-a")["hunter"]["key"] == "AAAA-hunter-key-1111"
    assert keys.load("user-b")["hunter"]["key"] == "BBBB-hunter-key-2222"
    assert "reoon" not in keys.load("user-a")
    assert keys.status("user-a")["reoon"] == {"connected": False, "last4": None}
    assert keys.status("user-b")["reoon"] == {"connected": True, "last4": "3333"}


def test_saving_again_replaces_and_removing_only_touches_that_user(db):
    keys.save("user-a", "hunter", "first-key-aaaaaaaa")
    keys.save("user-a", "hunter", "second-key-bbbbbbbb")
    keys.save("user-b", "hunter", "other-key-cccccccc")
    assert len([r for r in db.rows if r["user_id"] == "user-a"]) == 1
    assert keys.load("user-a")["hunter"]["key"] == "second-key-bbbbbbbb"
    keys.remove("user-a", "hunter")
    assert keys.load("user-a") == {}
    assert keys.load("user-b")["hunter"]["key"] == "other-key-cccccccc"


def test_missing_secret_stops_saving_with_a_plain_message(db, monkeypatch):
    monkeypatch.setattr(settings, "KEYS_ENCRYPTION_KEY", "")
    with pytest.raises(keys.KeysUnavailable, match="KEYS_ENCRYPTION_KEY"):
        keys.save("user-a", "hunter", "sk-live-1234567890abcdef")
    assert keys.load("user-a") == {}


# ---------- Which tools a user gets ----------

def test_tools_are_connected_only_by_the_users_own_keys():
    nothing = runner.tools_status({}, {})
    assert not any(t["connected"] for t in nothing.values())
    only_serper = runner.tools_status({}, {"serper": {"key": "s" * 10}})
    assert only_serper["serper"]["connected"] and only_serper["places"]["connected"]
    assert not only_serper["hunter"]["connected"] and not only_serper["verifier"]["connected"]
    switched_off = runner.tools_status({"tools": {"hunter": False}}, {"hunter": {"key": "h" * 10}})
    assert switched_off["hunter"] == {"connected": True, "on": False}


def test_a_company_is_looked_into_with_its_own_users_keys_only():
    a = {"hunter": {"key": "HUNTER-A"}, "reoon": {"key": "REOON-A"}}
    b = {"hunter": {"key": "HUNTER-B"}}
    with httpx.Client() as c:
        tools_a = runner.build_tools({}, a, c)
        tools_b = runner.build_tools({}, b, c)
    assert tools_a.hunter.headers["X-API-KEY"] == "HUNTER-A"
    assert tools_b.hunter.headers["X-API-KEY"] == "HUNTER-B"
    assert tools_a.verifier is not None and tools_b.verifier is None
    assert tools_a.serper is None and tools_b.writer is None


# ---------- Checking a key ----------

def test_hunter_key_check_reports_credits_left():
    body = {"data": {"plan_name": "Free", "requests": {"credits": {"used": 15.0, "available": 50.0}}}}
    result = keys.check_key("hunter", "k" * 12, client_for(lambda req: httpx.Response(200, json=body)))
    assert result.ok and "35 credits left" in result.message and "Free" in result.message


@pytest.mark.parametrize("provider", ["hunter", "serper", "reoon", "ai"])
def test_wrong_key_is_refused_without_repeating_it(provider):
    secret = "wrong-key-0123456789"
    result = keys.check_key(provider, secret, client_for(lambda req: httpx.Response(401, json={"reason": "Invalid API key."})))
    assert not result.ok and secret not in result.message


def test_serper_reoon_and_ai_keys_that_work():
    serper = keys.check_key("serper", "k" * 12, client_for(lambda req: httpx.Response(200, json={"balance": 2451, "rateLimit": 5})))
    assert serper.ok and "2,451" in serper.message
    reoon_body = {"api_status": "active", "remaining_daily_credits": 13, "remaining_instant_credits": 100, "status": "success"}
    reoon = keys.check_key("reoon", "k" * 12, client_for(lambda req: httpx.Response(200, json=reoon_body)))
    assert reoon.ok and "13 daily" in reoon.message and "100 instant" in reoon.message
    ai = keys.check_key("ai", "k" * 12, client_for(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})))
    assert ai.ok


def test_network_trouble_is_a_plain_message_not_a_crash():
    def boom(req):
        raise httpx.ConnectError("nope", request=req)

    result = keys.check_key("reoon", "k" * 12, client_for(boom))
    assert not result.ok and "Couldn't reach Reoon" in result.message
