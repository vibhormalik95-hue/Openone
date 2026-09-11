"""Payment authenticity, entitlement, replay and one-time key claim tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI, HTTPException

from hivemind import billing

SECRET = "whsec_disposable_unit_test_secret"
ORIGIN = "https://hivemind.test"


def signed(event: dict, stamp: int | None = None):
    raw = json.dumps(event, separators=(",", ":")).encode()
    stamp = int(time.time()) if stamp is None else stamp
    mac = hmac.new(SECRET.encode(), str(stamp).encode() + b"." + raw, hashlib.sha256).hexdigest()
    return raw, f"t={stamp},v1={mac}"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("STRIPE_LIVEMODE", "false")
    monkeypatch.setenv("PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("STRIPE_PRICE_STARTER", "price_starter_test")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_team_test")
    result = FastAPI()
    result.include_router(billing.router)
    return result


def event(event_type="customer.created"):
    return {
        "id": f"evt_{uuid4().hex}",
        "object": "event",
        "type": event_type,
        "livemode": False,
        "data": {"object": {"id": "cus_test"}},
    }


@pytest.mark.parametrize("tamper,expired", [(True, False), (False, True)])
async def test_webhook_rejects_modified_or_expired_signature(app, tamper, expired):
    raw, signature = signed(event(), int(time.time()) - 601 if expired else None)
    if tamper:
        raw += b" "
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post(
            "/billing/webhook", content=raw, headers={"Stripe-Signature": signature}
        )
    assert response.status_code == 400


async def test_webhook_ignores_authentic_unrelated_event_and_checks_mode(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        raw, signature = signed(event())
        response = await client.post(
            "/billing/webhook", content=raw, headers={"Stripe-Signature": signature}
        )
        assert response.status_code == 200
        live = event()
        live["livemode"] = True
        raw, signature = signed(live)
        response = await client.post(
            "/billing/webhook", content=raw, headers={"Stripe-Signature": signature}
        )
        assert response.status_code == 400


async def test_control_plane_rejects_cross_origin_checkout_before_db(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        for headers in ({}, {"Origin": "https://attacker.test"}):
            response = await client.post(
                "/billing/checkout", json={"plan": "starter"}, headers=headers
            )
            assert response.status_code == 403


@pytest.mark.parametrize(
    "status,invoice,expected",
    [
        ("active", "paid", "active"),
        ("active", "open", "past_due"),
        ("canceled", "paid", "canceled"),
        ("trialing", "paid", "incomplete"),
        ("unpaid", "paid", "unpaid"),
        ("incomplete_expired", "open", "incomplete_expired"),
    ],
)
def test_entitlement_never_resurrects_canceled_or_unpaid_subscription(status, invoice, expected):
    assert (
        billing.entitlement({"status": status, "latest_invoice": {"status": invoice}}) == expected
    )


def test_subscription_reference_handles_basil_and_legacy_invoice():
    for invoice in (
        {"subscription": "sub_legacy"},
        {"parent": {"subscription_details": {"subscription": "sub_basil"}}},
    ):
        assert billing.subscription_id(
            {"type": "invoice.paid", "data": {"object": invoice}}
        ).startswith("sub_")
    assert (
        billing.subscription_id({"type": "invoice.paid", "data": {"object": {"parent": None}}})
        is None
    )


def test_price_cannot_be_selected_by_untrusted_plan_metadata(app):
    with pytest.raises(HTTPException):
        billing.plan_from_subscription(
            {
                "metadata": {"plan": "pro"},
                "items": {"data": [{"quantity": 1, "price": {"id": "price_unknown"}}]},
            }
        )


class SingleConnectionPool:
    """Wrap one rollback-only integration connection without changing production code."""

    def __init__(self, connection):
        self.connection_value = connection

    @asynccontextmanager
    async def connection(self):
        yield self.connection_value


@pytest.mark.integration
async def test_paid_webhook_replay_claim_once_revocation_and_stale_invoice(app, monkeypatch):
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL must name migrated disposable PostgreSQL")
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.transaction(force_rollback=True):
            await conn.execute("SET LOCAL ROLE hivemind_billing")
            app.state.billing_pool = SingleConnectionPool(conn)
            claim_id = uuid4()
            nonce, browser = f"nonce-{uuid4()}", f"browser-{uuid4()}"
            sub_id, customer_id = f"sub_{uuid4().hex}", f"cus_{uuid4().hex}"
            session_id = f"cs_{uuid4().hex}"
            await conn.execute(
                "INSERT INTO checkout_claims(id,nonce_hash,plan,stripe_session_id) VALUES(%s,%s,'starter',%s)",
                (claim_id, billing.digest(nonce), session_id),
            )
            await conn.execute(
                "INSERT INTO billing_browser_sessions(session_hash,claim_id) VALUES(%s,%s)",
                (billing.digest(browser), claim_id),
            )
            live_subscription = {
                "id": sub_id,
                "customer": customer_id,
                "status": "active",
                "latest_invoice": {"status": "paid"},
                "metadata": {"claim_id": str(claim_id)},
                "items": {"data": [{"quantity": 1, "price": monthly_price("starter")}]},
            }

            async def fake_stripe(method, path, params=None, idempotency_key=None):
                if path == f"subscriptions/{sub_id}":
                    return live_subscription
                if path == f"checkout/sessions/{session_id}":
                    return {
                        "id": session_id,
                        "customer": customer_id,
                        "subscription": sub_id,
                        "status": "complete",
                        "payment_status": "paid",
                        "client_reference_id": str(claim_id),
                    }
                raise AssertionError(path)

            monkeypatch.setattr(billing, "stripe_api", fake_stripe)
            payment_event = event("invoice.paid")
            payment_event["data"]["object"] = {
                "id": "in_paid",
                "parent": {"subscription_details": {"subscription": sub_id}},
            }
            raw, signature = signed(payment_event)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=ORIGIN,
                cookies={billing.CLAIM_COOKIE: nonce, billing.ACCOUNT_COOKIE: browser},
                headers={"Origin": ORIGIN},
            ) as client:
                for _ in range(2):
                    response = await client.post(
                        "/billing/webhook", content=raw, headers={"Stripe-Signature": signature}
                    )
                    assert response.status_code == 200, response.text
                count = await billing.one(
                    conn,
                    "SELECT count(*) AS n FROM tenants WHERE stripe_subscription_id=%s",
                    (sub_id,),
                )
                assert count["n"] == 1
                claim = await client.post("/billing/claim")
                assert claim.status_code == 200, claim.text
                credentials = claim.json()
                assert credentials["token"].startswith("hvm_")
                assert claim.headers["cache-control"] == "no-store, private"
                key = await billing.one(
                    conn, "SELECT key_hash FROM api_keys WHERE id=%s", (credentials["key_id"],)
                )
                assert key["key_hash"] == billing.digest(credentials["token"])
                # Restore old cookie to simulate a replay; the DB, not cookie deletion, enforces once.
                second = await client.post(
                    "/billing/claim",
                    headers={
                        "Cookie": f"{billing.CLAIM_COOKIE}={nonce}; {billing.ACCOUNT_COOKIE}={browser}"
                    },
                )
                assert second.status_code == 409
                revoked = await client.delete(f"/billing/keys/{credentials['key_id']}")
                assert revoked.status_code == 200
                key = await billing.one(
                    conn, "SELECT revoked_at FROM api_keys WHERE id=%s", (credentials["key_id"],)
                )
                assert key["revoked_at"] is not None
                # Deliver a distinct old invoice event after current subscription cancellation.
                live_subscription["status"] = "canceled"
                payment_event["id"] = f"evt_{uuid4().hex}"
                raw, signature = signed(payment_event)
                response = await client.post(
                    "/billing/webhook", content=raw, headers={"Stripe-Signature": signature}
                )
                assert response.status_code == 200
                tenant = await billing.one(
                    conn,
                    "SELECT billing_status FROM tenants WHERE stripe_subscription_id=%s",
                    (sub_id,),
                )
                assert tenant["billing_status"] == "canceled"


def monthly_price(plan="starter"):
    return {
        "id": "price_starter_test" if plan == "starter" else "price_team_test",
        "currency": "usd", "unit_amount": 1500 if plan == "starter" else 4900,
        "type": "recurring", "billing_scheme": "per_unit", "active": True,
        "recurring": {"interval": "month", "interval_count": 1, "usage_type": "licensed"},
    }


def test_team_public_contract_retains_existing_storage_enum(app, monkeypatch):
    assert billing.CheckoutInput(plan="team").plan == "pro"
    monkeypatch.setenv("STRIPE_PRICE_TEAM", "price_team_test")
    assert billing.price_id("team") == "price_team_test"
    assert billing.plan_from_subscription(
        {"items": {"data": [{"quantity": 1, "price": monthly_price("team")}]}}
    ) == "pro"
    monkeypatch.setenv("STRIPE_PRICE_TEAM", "price_wrong")
    with pytest.raises(RuntimeError, match="disagree"):
        billing.price_id("team")


@pytest.mark.parametrize("changed", [
    {"unit_amount": 49}, {"unit_amount": 1500.0}, {"currency": "cad"},
    {"type": "one_time"}, {"billing_scheme": "tiered"},
    {"recurring": {"interval": "year", "interval_count": 1, "usage_type": "licensed"}},
    {"active": False},
])
def test_rejects_misconfigured_published_price(app, changed):
    price = {**monthly_price(), **changed}
    with pytest.raises(HTTPException) as error:
        billing.validate_price(price, "starter", require_active=True)
    assert error.value.status_code == 503


@pytest.mark.parametrize("change", [
    {"livemode": "false"}, {"id": "unscoped"}, {"data": {"object": []}},
    {"type": "x"}, {"object": "account"},
])
async def test_malformed_signed_event_is_rejected_before_db(app, change):
    raw, signature = signed({**event(), **change})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post(
            "/billing/webhook", content=raw, headers={"Stripe-Signature": signature}
        )
    assert response.status_code == 400
