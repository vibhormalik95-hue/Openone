"""Stripe-hosted Checkout + owner-only control plane. No MCP secrets in URLs.

Deploy on the same HTTPS origin as the Astro static pages. The separate billing
DB role has control-plane grants only and cannot read conversation memory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import stripe
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

router = APIRouter(prefix="/billing", tags=["billing"])
CLAIM_COOKIE = "__Host-hvm-claim"
ACCOUNT_COOKIE = "__Host-hvm-account"
STRIPE_VERSION = "2025-06-30.basil"
EVENT_TYPES = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.payment_action_required",
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def origin() -> str:
    value = os.environ["PUBLIC_ORIGIN"].rstrip("/")
    if not value.startswith("https://"):
        raise RuntimeError("PUBLIC_ORIGIN must be an HTTPS origin")
    return value


def same_origin(request: Request) -> None:
    # Cookie authorization must be combined with CSRF protection. CORS is not it.
    if request.headers.get("origin") != origin():
        raise HTTPException(403, "Same-origin request required")


def billing_pool(request: Request):
    pool = getattr(request.app.state, "billing_pool", None)
    if pool is None:
        raise HTTPException(503, "Billing is not configured")
    return pool


def private_response(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        data,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name, value, max_age=max_age, secure=True, httponly=True, samesite="lax", path="/"
    )


async def one(conn, query: str | sql.Composable, params=()) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(query, params)
        return await cursor.fetchone()


async def rows(conn, query: str, params=()) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(query, params)
        return await cursor.fetchall()


async def stripe_api(
    method: str, path: str, params: dict | None = None, idempotency_key: str | None = None
) -> dict:
    headers = {
        "Authorization": f"Bearer {os.environ['STRIPE_SECRET_KEY']}",
        "Stripe-Version": STRIPE_VERSION,
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    # Never log HTTP headers, response bodies, Checkout URLs or API exceptions.
    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            response = await client.request(
                method,
                f"https://api.stripe.com/v1/{path.lstrip('/')}",
                headers=headers,
                params=params if method == "GET" else None,
                data=params if method != "GET" else None,
            )
        except httpx.HTTPError as error:
            raise HTTPException(503, "Billing provider temporarily unavailable") from error
    if response.status_code >= 400:
        raise HTTPException(503, "Billing provider rejected the request")
    return response.json()


def price_id(plan: str) -> str:
    if plan == "starter":
        return os.environ["STRIPE_PRICE_STARTER"]
    if plan not in {"team", "pro"}:
        raise ValueError("Unknown plan")
    current, legacy = os.getenv("STRIPE_PRICE_TEAM"), os.getenv("STRIPE_PRICE_PRO")
    if current and legacy and current != legacy:
        raise RuntimeError("STRIPE_PRICE_TEAM and STRIPE_PRICE_PRO disagree")
    if not current and not legacy:
        raise RuntimeError("STRIPE_PRICE_TEAM is required")
    return current or legacy


def validate_price(price: dict, plan: str, *, require_active: bool = False) -> None:
    """Fail closed if an operator wires the wrong amount, currency or billing period."""
    if not isinstance(price, dict):
        raise HTTPException(503, "Expanded subscription price is required")
    recurring = price.get("recurring") or {}
    expected = 1500 if plan == "starter" else 4900
    if (
        price.get("id") != price_id(plan)
        or price.get("currency") != "usd"
        or type(price.get("unit_amount")) is not int
        or price.get("unit_amount") != expected
        or price.get("type") != "recurring"
        or price.get("billing_scheme") != "per_unit"
        or recurring.get("interval") != "month"
        or recurring.get("interval_count") != 1
        or recurring.get("usage_type") != "licensed"
        or (require_active and price.get("active") is not True)
    ):
        raise HTTPException(503, "Configured price must match the published monthly USD plan")


def plan_from_subscription(subscription: dict) -> str:
    items = subscription.get("items", {}).get("data", [])
    if len(items) != 1 or items[0].get("quantity") != 1:
        raise HTTPException(503, "Unsupported subscription configuration")
    actual = object_id(items[0].get("price"))
    for plan in ("starter", "pro"):
        if actual == price_id(plan):
            validate_price(items[0]["price"], plan)
            return plan
    raise HTTPException(503, "Unknown subscription price")


def object_id(value: Any) -> str | None:
    return value.get("id") if isinstance(value, dict) else value


def subscription_id(event: dict) -> str | None:
    obj = event["data"]["object"]
    if event["type"].startswith("customer.subscription."):
        return obj["id"]
    if event["type"].startswith("checkout.session."):
        return object_id(obj.get("subscription"))
    return object_id(obj.get("subscription")) or object_id(
        ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
    )


def entitlement(subscription: dict) -> str:
    status = subscription["status"]
    if status == "active":
        invoice = subscription.get("latest_invoice")
        return (
            "active"
            if isinstance(invoice, dict) and invoice.get("status") == "paid"
            else "past_due"
        )
    # No free trial is sold. A Dashboard-created trial cannot silently grant access.
    if status == "trialing":
        return "incomplete"
    valid = {"past_due", "unpaid", "canceled", "incomplete", "paused", "incomplete_expired"}
    return status if status in valid else "incomplete"


async def checkout_admission(request: Request) -> None:
    """Shared DB admission, not per-process counters; Origin alone is not anti-abuse."""
    peer = request.client.host if request.client else "unknown"
    peer_hash = hmac.new(
        os.environ["STRIPE_WEBHOOK_SECRET"].encode(), peer.encode(), hashlib.sha256
    ).hexdigest()
    buckets = [
        ("checkout:global:minute", 60, 60),
        ("checkout:global:day", 86400, 500),
        (f"checkout:peer:minute:{peer_hash}", 60, 3),
        (f"checkout:peer:day:{peer_hash}", 86400, 20),
    ]
    async with billing_pool(request).connection() as conn, conn.transaction():
        for key, seconds, cap in buckets:
            result = await one(
                conn,
                """
                INSERT INTO billing_admission(bucket,window_start,attempts)
                VALUES(%s,date_bin(make_interval(secs=>%s),now(),'2000-01-01'::timestamptz),1)
                ON CONFLICT(bucket) DO UPDATE SET
                  attempts=CASE WHEN billing_admission.window_start=excluded.window_start
                                THEN billing_admission.attempts+1 ELSE 1 END,
                  window_start=excluded.window_start
                RETURNING attempts
                """,
                (key, seconds),
            )
            if result["attempts"] > cap:
                raise HTTPException(
                    429,
                    "Checkout request limit reached; try later",
                    headers={"Retry-After": str(seconds)},
                )
        await conn.execute(
            "DELETE FROM billing_admission WHERE window_start<now()-interval '2 days'"
        )
        await conn.execute(
            "DELETE FROM billing_browser_sessions WHERE expires_at<now()-interval '7 days'"
        )
        # Keep old unclaimed checkouts long enough for Stripe's delayed/manual retries.
        await conn.execute("""DELETE FROM checkout_claims c WHERE c.tenant_id IS NULL
                           AND c.created_at<now()-interval '45 days'
                           AND NOT EXISTS(SELECT 1 FROM billing_browser_sessions s WHERE s.claim_id=c.id)""")


class CheckoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["starter", "team", "pro"]

    @field_validator("plan")
    @classmethod
    def normalize_team(cls, value: str) -> str:
        # Preserve the existing SQL plan enum and populated installations.
        return "pro" if value == "team" else value


@router.post("/checkout")
async def checkout(body: CheckoutInput, request: Request):
    same_origin(request)
    await checkout_admission(request)
    if request.cookies.get(ACCOUNT_COOKIE):
        async with billing_pool(request).connection() as conn:
            try:
                existing_owner = await account(conn, request)
            except HTTPException as error:
                if error.status_code != 401:
                    raise
            else:
                if existing_owner:
                    raise HTTPException(
                        409, "An account already exists; manage billing from your account"
                    )
    claim_cookie = request.cookies.get(CLAIM_COOKIE)
    if claim_cookie:
        async with billing_pool(request).connection() as conn:
            previous = await one(
                conn,
                """
                SELECT id, plan, stripe_session_id, claimed_at FROM checkout_claims
                WHERE nonce_hash=%s AND expires_at>now()
                """,
                (digest(claim_cookie),),
            )
        if previous and previous["claimed_at"]:
            raise HTTPException(409, "An account already exists; manage it from the account page")
        if previous and previous["stripe_session_id"]:
            existing = await stripe_api("GET", f"checkout/sessions/{previous['stripe_session_id']}")
            if existing["status"] == "complete":
                return private_response({"url": f"{origin()}/onboard/"})
            if existing["status"] == "open":
                if previous["plan"] != body.plan:
                    raise HTTPException(
                        409, "Finish or expire your existing Checkout before choosing another tier"
                    )
                return private_response({"url": existing["url"]})
    price = await stripe_api("GET", f"prices/{price_id(body.plan)}")
    validate_price(price, body.plan, require_active=True)
    # Commit claim BEFORE Stripe creation. A fast webhook can safely find it.
    claim_id, claim_secret = uuid4(), secrets.token_urlsafe(32)
    session_secret = secrets.token_urlsafe(32)
    async with billing_pool(request).connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO checkout_claims(id,nonce_hash,plan) VALUES(%s,%s,%s)",
            (claim_id, digest(claim_secret), body.plan),
        )
        await conn.execute(
            "INSERT INTO billing_browser_sessions(session_hash,claim_id) VALUES(%s,%s)",
            (digest(session_secret), claim_id),
        )
    session = await stripe_api(
        "POST",
        "checkout/sessions",
        {
            "mode": "subscription",
            "line_items[0][price]": price_id(body.plan),
            "line_items[0][quantity]": "1",
            "payment_method_types[0]": "card",
            "success_url": f"{origin()}/onboard/",
            "cancel_url": f"{origin()}/",
            "client_reference_id": str(claim_id),
            "metadata[claim_id]": str(claim_id),
            "subscription_data[metadata][claim_id]": str(claim_id),
            "billing_address_collection": "required",
        },
        idempotency_key=f"checkout:{claim_id}",
    )
    async with billing_pool(request).connection() as conn, conn.transaction():
        await conn.execute(
            """UPDATE checkout_claims SET stripe_session_id=%s
                           WHERE id=%s AND (stripe_session_id IS NULL OR stripe_session_id=%s)""",
            (session["id"], claim_id, session["id"]),
        )
    response = private_response({"url": session["url"]})
    set_cookie(response, CLAIM_COOKIE, claim_secret, 86400)
    # Set before Checkout: even if the later key-delivery response is lost,
    # this pre-existing owner cookie can rotate credentials on the account page.
    set_cookie(response, ACCOUNT_COOKIE, session_secret, 30 * 86400)
    return response


async def reconcile_subscription(conn, sub_id: str) -> None:
    # Serialize BEFORE retrieving current state. Sorting stale event timestamps
    # alone cannot prevent a late invoice event from undoing cancellation.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (sub_id,))
    sub = await stripe_api("GET", f"subscriptions/{sub_id}", {"expand[]": "latest_invoice"})
    claim_text = sub.get("metadata", {}).get("claim_id")
    if not claim_text:
        return  # Another product in the same Stripe account.
    try:
        claim_id = UUID(claim_text)
    except ValueError:
        return
    claim = await one(conn, "SELECT * FROM checkout_claims WHERE id=%s FOR UPDATE", (claim_id,))
    if claim is None:
        return  # Do not provision from arbitrary Stripe metadata.
    plan, status = plan_from_subscription(sub), entitlement(sub)
    customer_id = object_id(sub["customer"])
    if claim["tenant_id"]:
        await conn.execute(
            """UPDATE tenants SET plan=%s,billing_status=%s
                           WHERE id=%s AND stripe_subscription_id=%s AND stripe_customer_id=%s""",
            (plan, status, claim["tenant_id"], sub_id, customer_id),
        )
        return
    if status != "active":
        return
    if claim["stripe_session_id"]:
        session = await stripe_api("GET", f"checkout/sessions/{claim['stripe_session_id']}")
    else:
        # Covers webhook winning the race against Checkout POST's final DB update.
        sessions = await stripe_api(
            "GET", "checkout/sessions", {"subscription": sub_id, "limit": "100"}
        )
        session = next(
            (s for s in sessions["data"] if s.get("client_reference_id") == str(claim_id)), None
        )
    if (
        not session
        or session.get("payment_status") != "paid"
        or session.get("status") != "complete"
        or session.get("client_reference_id") != str(claim_id)
        or object_id(session.get("subscription")) != sub_id
        or object_id(session.get("customer")) != customer_id
    ):
        raise HTTPException(503, "Paid Checkout has not reconciled; retry delivery")
    tenant_id = uuid4()
    await conn.execute(
        """INSERT INTO tenants(id,name,plan,billing_status,stripe_customer_id,stripe_subscription_id)
                       VALUES(%s,%s,%s,'active',%s,%s)""",
        (tenant_id, "My Hivemind", plan, customer_id, sub_id),
    )
    await conn.execute(
        "INSERT INTO projects(tenant_id,name,slug) VALUES(%s,'Default','default')", (tenant_id,)
    )
    await conn.execute(
        "INSERT INTO tenant_memberships(tenant_id,subject,role) VALUES(%s,%s,'owner')",
        (tenant_id, f"stripe:{customer_id}"),
    )
    await conn.execute(
        "UPDATE checkout_claims SET tenant_id=%s,stripe_session_id=%s WHERE id=%s",
        (tenant_id, session["id"], claim_id),
    )


class StripeEventData(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    object: dict[str, Any]


class StripeEvent(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    id: str = Field(pattern=r"^evt_[A-Za-z0-9_]+$", max_length=255)
    object: Literal["event"]
    type: str = Field(min_length=3, max_length=255, pattern=r"^[a-z_]+(?:\.[a-z_]+)+$")
    livemode: bool
    data: StripeEventData


@router.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    if len(raw) > 1_048_576:
        raise HTTPException(413, "Payload too large")
    try:
        stripe.Webhook.construct_event(
            raw,
            request.headers.get("stripe-signature", ""),
            os.environ["STRIPE_WEBHOOK_SECRET"],
            tolerance=300,
        )
        event = StripeEvent.model_validate(json.loads(raw)).model_dump()
    except (ValueError, TypeError, KeyError, ValidationError, stripe.SignatureVerificationError) as error:
        raise HTTPException(400, "Invalid webhook signature") from error
    if bool(event.get("livemode")) != (os.getenv("STRIPE_LIVEMODE", "false").lower() == "true"):
        raise HTTPException(400, "Wrong Stripe mode")
    if event["type"] not in EVENT_TYPES:
        return {"received": True}
    async with billing_pool(request).connection() as conn, conn.transaction():
        inserted = await one(
            conn,
            """INSERT INTO stripe_webhook_events(event_id,event_type)
                             VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING event_id""",
            (event["id"], event["type"]),
        )
        if inserted:
            sub_id = subscription_id(event)
            if sub_id:
                await reconcile_subscription(conn, sub_id)
    # Only acknowledge after COMMIT. Any provider/DB failure rolls back event ID
    # and produces non-2xx, so Stripe retries instead of losing fulfillment.
    return {"received": True}


async def account(conn, request: Request, require_paid: bool = False) -> dict:
    secret = request.cookies.get(ACCOUNT_COOKIE, "")
    if not secret:
        raise HTTPException(401, "Sign in with your recovery code")
    # The only SQL suffix is a fixed lock clause selected by an internal boolean.
    result = await one(
        conn,
        sql.SQL("""SELECT t.* FROM billing_browser_sessions s
                        LEFT JOIN checkout_claims c ON c.id=s.claim_id
                        JOIN tenants t ON t.id=coalesce(s.tenant_id,c.tenant_id)
                        WHERE s.session_hash=%s AND s.expires_at>now() AND s.revoked_at IS NULL""")
        + sql.SQL(" FOR UPDATE OF t" if require_paid else ""),
        (digest(secret),),
    )
    if result is None:
        raise HTTPException(401, "Account session expired or payment still pending")
    if require_paid and result["billing_status"] != "active":
        raise HTTPException(402, "Active paid subscription required")
    return result


async def issue_key(
    conn, tenant_id, label: str, project_ids: list[UUID], can_write: bool = True
) -> dict:
    if not project_ids:
        raise HTTPException(422, "Choose at least one project")
    permitted = await rows(
        conn,
        "SELECT id FROM projects WHERE tenant_id=%s AND id=ANY(%s::uuid[])",
        (tenant_id, project_ids),
    )
    if {r["id"] for r in permitted} != set(project_ids):
        raise HTTPException(403, "Project outside this organization")
    key_id, raw_key = uuid4(), f"hvm_{secrets.token_urlsafe(32)}"
    scopes = ["memory:read", "memory:write"] if can_write else ["memory:read"]
    await conn.execute(
        """INSERT INTO api_keys(id,tenant_id,key_hash,prefix,label,all_projects,scopes)
                       VALUES(%s,%s,%s,%s,%s,false,%s)""",
        (key_id, tenant_id, digest(raw_key), raw_key[:12], label, scopes),
    )
    for project in set(project_ids):
        await conn.execute(
            """INSERT INTO api_key_projects(tenant_id,api_key_id,project_id,can_write)
                           VALUES(%s,%s,%s,%s)""",
            (tenant_id, key_id, project, can_write),
        )
    return {"key_id": str(key_id), "token": raw_key, "label": label}


async def rotate_recovery(conn, tenant_id) -> str:
    raw = f"hvm_recovery_{secrets.token_urlsafe(32)}"
    await conn.execute(
        """INSERT INTO billing_recovery_codes(tenant_id,code_hash) VALUES(%s,%s)
                       ON CONFLICT(tenant_id) DO UPDATE SET code_hash=excluded.code_hash,created_at=now()""",
        (tenant_id, digest(raw)),
    )
    return raw


@router.post("/claim")
async def claim_key(request: Request):
    same_origin(request)
    secret = request.cookies.get(CLAIM_COOKIE, "")
    if not secret:
        raise HTTPException(401, "Use the browser where you started Checkout")
    async with billing_pool(request).connection() as conn, conn.transaction():
        claim = await one(
            conn,
            """SELECT c.*,t.billing_status FROM checkout_claims c
                          LEFT JOIN tenants t ON t.id=c.tenant_id WHERE c.nonce_hash=%s
                          AND c.expires_at>now() FOR UPDATE OF c""",
            (digest(secret),),
        )
        if claim is None:
            raise HTTPException(401, "Checkout claim expired")
        if claim["claimed_at"]:
            raise HTTPException(409, "Key already delivered; use account key rotation if needed")
        if not claim["tenant_id"] or claim["billing_status"] != "active":
            return private_response({"status": "pending"}, 202)
        # Cookie provisioned BEFORE Checkout must belong to this exact tenant.
        owner = await account(conn, request, require_paid=True)
        if owner["id"] != claim["tenant_id"]:
            raise HTTPException(403, "Wrong account session")
        project = await one(
            conn,
            "SELECT id FROM projects WHERE tenant_id=%s AND slug='default'",
            (claim["tenant_id"],),
        )
        key = await issue_key(conn, claim["tenant_id"], "owner/default", [project["id"]])
        recovery = await rotate_recovery(conn, claim["tenant_id"])
        await conn.execute(
            "UPDATE checkout_claims SET claimed_at=now() WHERE id=%s", (claim["id"],)
        )
    response = private_response(
        {
            "status": "ready",
            **key,
            "recovery_code": recovery,
            "project_id": str(project["id"]),
            "mcp_url": f"{origin()}/mcp/v1",
        }
    )
    response.delete_cookie(CLAIM_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.get("/account")
async def get_account(request: Request):
    async with billing_pool(request).connection() as conn:
        owner = await account(conn, request)
        projects = await rows(
            conn,
            "SELECT id,name,slug FROM projects WHERE tenant_id=%s ORDER BY created_at",
            (owner["id"],),
        )
        keys = await rows(
            conn,
            """SELECT id,prefix,label,revoked_at FROM api_keys
                          WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 100""",
            (owner["id"],),
        )
    return private_response(
        {
            "plan": "team" if owner["plan"] == "pro" else owner["plan"],
            "billing_status": owner["billing_status"],
            "oauth_enabled": bool(
                getattr(getattr(request.app.state, "settings", None), "auth_mode", None) == "oauth"
                and os.getenv("AUTH0_LINK_CLIENT_ID")
            ),
            "projects": [{**p, "id": str(p["id"])} for p in projects],
            "keys": [
                {
                    **k,
                    "id": str(k["id"]),
                    "revoked_at": str(k["revoked_at"]) if k["revoked_at"] else None,
                }
                for k in keys
            ],
        }
    )


class KeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9@._ /-]+$")
    project_ids: list[UUID] = Field(min_length=1, max_length=100)
    can_write: bool = True


@router.post("/keys")
async def create_key(body: KeyInput, request: Request):
    same_origin(request)
    async with billing_pool(request).connection() as conn, conn.transaction():
        owner = await account(conn, request, require_paid=True)
        # Serializes concurrent issuances; operational cap is separate from project count.
        await conn.execute("SELECT id FROM tenants WHERE id=%s FOR UPDATE", (owner["id"],))
        count = await one(
            conn,
            "SELECT count(*) AS n FROM api_keys WHERE tenant_id=%s AND revoked_at IS NULL",
            (owner["id"],),
        )
        cap = 10 if owner["plan"] == "starter" else 100
        if count["n"] >= cap:
            raise HTTPException(409, f"Revoke an old key; maximum {cap} active keys")
        result = await issue_key(conn, owner["id"], body.label, body.project_ids, body.can_write)
    return private_response(result, 201)


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: UUID, request: Request):
    same_origin(request)
    async with billing_pool(request).connection() as conn, conn.transaction():
        owner = await account(conn, request)
        result = await one(
            conn,
            """UPDATE api_keys SET revoked_at=coalesce(revoked_at,now())
                           WHERE id=%s AND tenant_id=%s RETURNING id""",
            (key_id, owner["id"]),
        )
        if result is None:
            raise HTTPException(404, "Key not found")
    return private_response({"revoked": True})


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@router.post("/projects")
async def create_project(body: ProjectInput, request: Request):
    same_origin(request)
    async with billing_pool(request).connection() as conn, conn.transaction():
        owner = await account(conn, request, require_paid=True)
        await conn.execute("SELECT id FROM tenants WHERE id=%s FOR UPDATE", (owner["id"],))
        count = await one(
            conn, "SELECT count(*) AS n FROM projects WHERE tenant_id=%s", (owner["id"],)
        )
        if owner["plan"] == "starter" and count["n"] >= 5:
            raise HTTPException(409, "Starter supports five projects")
        if await one(
            conn, "SELECT id FROM projects WHERE tenant_id=%s AND slug=%s", (owner["id"], body.slug)
        ):
            raise HTTPException(409, "Project slug already exists")
        result = await one(
            conn,
            "INSERT INTO projects(tenant_id,name,slug) VALUES(%s,%s,%s) RETURNING id",
            (owner["id"], body.name, body.slug),
        )
    return private_response({"project_id": str(result["id"])}, 201)


@router.post("/portal")
async def portal(request: Request):
    same_origin(request)
    async with billing_pool(request).connection() as conn:
        owner = await account(conn, request)
    data = {"customer": owner["stripe_customer_id"], "return_url": f"{origin()}/account/"}
    if os.getenv("STRIPE_PORTAL_CONFIGURATION"):
        data["configuration"] = os.environ["STRIPE_PORTAL_CONFIGURATION"]
    result = await stripe_api("POST", "billing_portal/sessions", data)
    return private_response({"url": result["url"]})


class RecoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_code: str = Field(min_length=50, max_length=100)


@router.post("/recover")
async def recover(body: RecoveryInput, request: Request):
    same_origin(request)
    secret = secrets.token_urlsafe(32)
    async with billing_pool(request).connection() as conn, conn.transaction():
        recovery = await one(
            conn,
            "SELECT tenant_id FROM billing_recovery_codes WHERE code_hash=%s FOR UPDATE",
            (digest(body.recovery_code),),
        )
        if not recovery:
            raise HTTPException(401, "Invalid recovery code")
        next_code = await rotate_recovery(conn, recovery["tenant_id"])
        # Recovery invalidates old owner sessions, but never silently changes memory keys.
        await conn.execute(
            """UPDATE billing_browser_sessions s SET revoked_at=now()
                           WHERE s.tenant_id=%s OR s.claim_id IN
                           (SELECT id FROM checkout_claims WHERE tenant_id=%s)""",
            (recovery["tenant_id"], recovery["tenant_id"]),
        )
        await conn.execute(
            "INSERT INTO billing_browser_sessions(session_hash,tenant_id) VALUES(%s,%s)",
            (digest(secret), recovery["tenant_id"]),
        )
    response = private_response({"recovery_code": next_code})
    set_cookie(response, ACCOUNT_COOKIE, secret, 30 * 86400)
    return response


@router.post("/recovery-code")
async def new_recovery_code(request: Request):
    same_origin(request)
    async with billing_pool(request).connection() as conn, conn.transaction():
        owner = await account(conn, request)
        code = await rotate_recovery(conn, owner["id"])
    return private_response({"recovery_code": code})


@router.post("/logout")
async def logout(request: Request):
    same_origin(request)
    async with billing_pool(request).connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE billing_browser_sessions SET revoked_at=now() WHERE session_hash=%s",
            (digest(request.cookies.get(ACCOUNT_COOKIE, "")),),
        )
    response = private_response({"signed_out": True})
    response.delete_cookie(ACCOUNT_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response
