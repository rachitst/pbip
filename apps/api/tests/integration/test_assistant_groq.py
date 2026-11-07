"""ADR-0037: LLM_PROVIDER=groq end to end through the real wiring (``get_chat_model``), with both
providers on fake HTTP transports. No request leaves the test process."""

import json
from collections.abc import Callable
from functools import partial
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.modules.assistant import dependencies
from pbip_api.modules.assistant.llm import OllamaChatModel
from pbip_api.modules.assistant.providers import Cooldown, GroqChatModel
from pbip_api.modules.compliance.models import AuditLog
from pbip_test_support import Actor
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]

_KEY = "gsk_" + "k" * 48  # generated test value
_UNCLEAR = "am I doing ok money-wise?"  # no clear intent: goes to the model


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="local",
        argon2_time_cost=1,
        argon2_memory_kib=8,
        argon2_parallelism=1,
        jwt_secret=SecretStr("t" * 48),
        field_encryption_key=SecretStr(Fernet.generate_key().decode()),
        assistant_llm_enabled=True,
        llm_provider="groq",
        groq_api_key=SecretStr(_KEY),
        groq_model="openai/gpt-oss-20b",
        rate_limit_groq_per_minute=2,
    )


def _groq_call(name: str, arguments: str = "{}") -> httpx.Response:
    call = {"id": "c1", "type": "function", "function": {"name": name, "arguments": arguments}}
    message = {"role": "assistant", "content": None, "tool_calls": [call]}
    return httpx.Response(200, json={"choices": [{"index": 0, "message": message}]})


def _ollama_reply(tool: str) -> httpx.Response:
    content = json.dumps({"tool": tool, "arguments": {}})
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


class Upstream:
    """Fake Groq and Ollama servers; each keeps the requests it received."""

    def __init__(self) -> None:
        self.groq: httpx.Response | Exception = _groq_call("get_account_summary")
        self.ollama: httpx.Response | Exception = _ollama_reply("get_spend")
        self.groq_requests: list[httpx.Request] = []
        self.ollama_requests: list[httpx.Request] = []

    def serve_groq(self, request: httpx.Request) -> httpx.Response:
        self.groq_requests.append(request)
        if isinstance(self.groq, Exception):
            raise self.groq
        return self.groq

    def serve_ollama(self, request: httpx.Request) -> httpx.Response:
        self.ollama_requests.append(request)
        if isinstance(self.ollama, Exception):
            raise self.ollama
        return self.ollama


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Upstream:
    fake = Upstream()
    monkeypatch.setattr(
        dependencies,
        "GroqChatModel",
        partial(GroqChatModel, transport=httpx.MockTransport(fake.serve_groq)),
    )
    monkeypatch.setattr(
        dependencies,
        "OllamaChatModel",
        partial(OllamaChatModel, transport=httpx.MockTransport(fake.serve_ollama)),
    )
    monkeypatch.setattr(dependencies, "_GROQ_COOLDOWN", Cooldown())  # no state between tests
    return fake


def _chat(client: TestClient, actor: Actor, message: str) -> Any:
    resp = client.post("/api/v1/assistant/chat", headers=actor.headers, json={"message": message})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _llm_audit(db: Session, actor: Actor) -> list[dict[str, Any]]:
    rows = db.execute(
        select(AuditLog)
        .where(AuditLog.actor_user_id == actor.user_id, AuditLog.action == "assistant.llm_call")
        .order_by(AuditLog.seq)
    ).scalars()
    return [dict(row.details or {}) for row in rows]


def _outcomes(db: Session, actor: Actor) -> list[tuple[str, str]]:
    return [(d["provider"], d["outcome"]) for d in _llm_audit(db, actor)]


def test_groq_routes_the_unclear_question_and_nothing_identifying_leaves(
    client: TestClient, make_customer: Callable[..., Actor], upstream: Upstream, db: Session
) -> None:
    alice = make_customer(funds=5_000_00)
    message = (
        f"I'm Test Customer ({alice.email}), ref {alice.customer_id}, acct "
        f"{alice.account_number} - {_UNCLEAR}"
    )
    body = _chat(client, alice, message)
    assert body["engine"] == "llm" and body["tool"] == "get_account_summary"
    assert body["outcome"] == "answered" and upstream.ollama_requests == []
    [request] = upstream.groq_requests
    sent = request.content.decode()
    for secret in (
        alice.email,
        str(alice.customer_id),
        str(alice.user_id),
        alice.account_number,
        "Test Customer",
        _KEY,
    ):
        assert secret not in sent, secret
    assert _UNCLEAR in json.loads(sent)["messages"][1]["content"]
    [audit] = _llm_audit(db, alice)
    assert audit["provider"] == "groq" and audit["outcome"] == "ok"
    assert isinstance(audit["latency_ms"], int) and set(audit) == {
        "provider",
        "outcome",
        "latency_ms",
    }


@pytest.mark.parametrize(
    ("groq", "outcome"),
    [
        (httpx.Response(429, headers={"retry-after": "3"}), "rate_limited"),
        (httpx.Response(500), "error"),
        (httpx.Response(503), "error"),
        (httpx.Response(200, content=b"{not json"), "error"),
        (_groq_call("get_recent_transactions", "{broken"), "invalid"),
        (_groq_call("transfer_money", '{"amount": 50000}'), "invalid"),  # injected tool
        (_groq_call("get_account_summary", '{"customer_id": "x"}'), "invalid"),  # identity arg
    ],
)
def test_any_groq_failure_but_a_timeout_falls_back_to_ollama_and_is_audited(
    client: TestClient,
    make_customer: Callable[..., Actor],
    upstream: Upstream,
    db: Session,
    groq: httpx.Response | Exception,
    outcome: str,
) -> None:
    alice = make_customer(funds=5_000_00)
    upstream.groq = groq
    body = _chat(client, alice, _UNCLEAR)
    assert body["engine"] == "llm" and body["tool"] == "get_spend"  # Ollama's choice
    assert len(upstream.groq_requests) == 1 and len(upstream.ollama_requests) == 1  # no retries
    assert _outcomes(db, alice) == [("groq", outcome), ("ollama", "ok")]


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")])
def test_a_groq_timeout_goes_straight_to_the_rules_and_is_audited_without_payloads(
    client: TestClient,
    make_customer: Callable[..., Actor],
    upstream: Upstream,
    db: Session,
    error: Exception,
) -> None:
    """ADR-0037 addendum (2026-11-08): no Ollama wait after the routing deadline, no error."""
    alice = make_customer(funds=5_000_00)
    upstream.groq = error
    body = _chat(client, alice, "recent transactions and my spending")  # two intents: unclear
    assert body["engine"] == "rules" and body["outcome"] == "answered"
    assert body["tool"] == "get_recent_transactions"  # the keyword rules' precedence
    assert len(upstream.groq_requests) == 1 and upstream.ollama_requests == []
    [audit] = _llm_audit(db, alice)
    assert set(audit) == {"provider", "outcome", "latency_ms"}  # never the message or prompt
    assert (audit["provider"], audit["outcome"]) == ("groq", "timeout")
    [call] = [
        dict(row.details or {})
        for row in db.execute(
            select(AuditLog).where(
                AuditLog.actor_user_id == alice.user_id,
                AuditLog.action == "assistant.tool_call",
            )
        ).scalars()
    ]
    assert call == {"tool": "get_recent_transactions", "outcome": "ok", "engine": "rules"}


def test_a_groq_timeout_on_the_stream_ends_with_an_answer_not_an_error(
    client: TestClient, make_customer: Callable[..., Actor], upstream: Upstream
) -> None:
    alice = make_customer(funds=5_000_00)
    upstream.groq = httpx.ReadTimeout("slow")
    resp = client.post(
        "/api/v1/assistant/chat/stream", headers=alice.headers, json={"message": _UNCLEAR}
    )
    assert resp.status_code == 200
    events = [line.removeprefix("event: ") for line in resp.text.splitlines()
              if line.startswith("event: ")]  # fmt: skip
    assert "error" not in events and events[-1] == "done"
    done = json.loads(resp.text.rsplit("data: ", 1)[1].splitlines()[0])
    assert done["engine"] == "rules" and done["outcome"] == "help"  # nothing fits: help text
    assert upstream.ollama_requests == []


def test_when_both_models_fail_the_rules_still_answer(
    client: TestClient, make_customer: Callable[..., Actor], upstream: Upstream, db: Session
) -> None:
    alice = make_customer(funds=5_000_00)
    upstream.groq = httpx.Response(502)
    upstream.ollama = httpx.ConnectError("refused")
    body = _chat(client, alice, "recent transactions and my spending")  # two intents: unclear
    assert body["engine"] == "rules" and body["outcome"] == "answered"
    assert body["tool"] == "get_recent_transactions"
    assert _outcomes(db, alice) == [("groq", "error"), ("ollama", "error")]


def test_a_429_stops_groq_calls_from_this_process_for_a_while(
    client: TestClient, make_customer: Callable[..., Actor], upstream: Upstream, db: Session
) -> None:
    alice, bob = make_customer(funds=1_00), make_customer(funds=1_00)
    upstream.groq = httpx.Response(429, headers={"retry-after": "30"})
    _chat(client, alice, _UNCLEAR)
    _chat(client, bob, _UNCLEAR)
    assert len(upstream.groq_requests) == 1  # bob's question did not hit the limited org again
    assert _outcomes(db, bob) == [("groq", "cooldown"), ("ollama", "ok")]


def test_the_per_customer_groq_budget_sends_the_rest_to_ollama(
    client: TestClient, make_customer: Callable[..., Actor], upstream: Upstream, db: Session
) -> None:
    alice, bob = make_customer(funds=1_00), make_customer(funds=1_00)
    for _ in range(3):  # rate_limit_groq_per_minute=2
        assert _chat(client, alice, _UNCLEAR)["engine"] == "llm"
    _chat(client, bob, _UNCLEAR)  # another customer has their own budget
    assert len(upstream.groq_requests) == 3
    assert _outcomes(db, alice) == [
        ("groq", "ok"),
        ("groq", "ok"),
        ("groq", "budget"),
        ("ollama", "ok"),
    ]
    assert _outcomes(db, bob) == [("groq", "ok")]


def test_clear_questions_and_refusals_never_reach_any_model(
    client: TestClient, make_customer: Callable[..., Actor], upstream: Upstream, db: Session
) -> None:
    alice = make_customer(funds=1_00)
    assert _chat(client, alice, "what's my balance")["engine"] == "rules"
    assert _chat(client, alice, "send 5000 to Bob now")["outcome"] == "refused"
    assert upstream.groq_requests == [] and upstream.ollama_requests == []
    assert _llm_audit(db, alice) == []


def test_the_default_provider_never_calls_groq(
    client: TestClient,
    make_customer: Callable[..., Actor],
    upstream: Upstream,
    settings: Settings,
    db: Session,
) -> None:
    settings.llm_provider = "ollama"
    alice = make_customer(funds=1_00)
    body = _chat(client, alice, _UNCLEAR)
    assert body["engine"] == "llm" and upstream.groq_requests == []
    assert _outcomes(db, alice) == [("ollama", "ok")]


def test_stream_uses_the_same_chain(
    client: TestClient, make_customer: Callable[..., Actor], upstream: Upstream
) -> None:
    alice = make_customer(funds=1_00)
    upstream.groq = httpx.Response(500)
    with client.stream(
        "POST", "/api/v1/assistant/chat/stream", headers=alice.headers, json={"message": _UNCLEAR}
    ) as resp:
        text = "".join(resp.iter_text())
    assert '"tool":"get_spend"' in text.replace(" ", "") and "event: done" in text
    assert len(upstream.groq_requests) == 1 and len(upstream.ollama_requests) == 1
