#!/usr/bin/env python3
"""Create/reuse Stripe test/live products, immutable prices, portal and webhook.

Run manually with an operator's Stripe key; never run inside app startup or CI.
Non-secret IDs print to stdout. New webhook signing secret writes mode 0600 file.
"""

import argparse
import asyncio
import os
from pathlib import Path

import stripe

from hivemind.billing import EVENT_TYPES, STRIPE_VERSION, stripe_api


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Required for a live Stripe secret key")
    parser.add_argument("--webhook-secret-file", default=".stripe-webhook-secret")
    args = parser.parse_args()
    key = os.environ["STRIPE_SECRET_KEY"]
    live = key.startswith("sk_live_") or key.startswith("rk_live_")
    if live and not args.live:
        parser.error("Live setup requires --live; use a test-mode key for rehearsal")
    public_origin = os.environ["PUBLIC_ORIGIN"].rstrip("/")
    if not public_origin.startswith("https://"):
        parser.error("PUBLIC_ORIGIN must start with https://")
    products = []
    for tier, dollars, display in (
        ("starter", 15, "Hivemind Scale Starter"),
        ("pro", 49, "Hivemind Scale Team"),
    ):
        lookup = f"hivemind_{tier}_usd_monthly_v1"
        matches = await stripe_api("GET", "prices", {"lookup_keys[]": lookup, "active": "true"})
        if matches["data"]:
            price = matches["data"][0]
            if (
                price["unit_amount"] != dollars * 100
                or price["currency"] != "usd"
                or price["recurring"]["interval"] != "month"
            ):
                raise RuntimeError(f"Existing {lookup} has unexpected amount/currency/interval")
        else:
            # Retrieve by stable ID first: Stripe idempotency retention is finite.
            try:
                product = await asyncio.to_thread(
                    stripe.Product.retrieve,
                    f"hivemind_{tier}_v1",
                    api_key=key,
                    stripe_version=STRIPE_VERSION,
                )
            except stripe.InvalidRequestError as error:
                if error.http_status != 404:
                    raise RuntimeError("Stripe product lookup failed") from None
                product = await stripe_api(
                    "POST",
                    "products",
                    {
                        "id": f"hivemind_{tier}_v1",
                        "name": display,
                        "metadata[service]": "hivemind-scale",
                    },
                    idempotency_key=f"hivemind-product-{tier}-v1",
                )
            if product.get("metadata", {}).get("service") != "hivemind-scale":
                raise RuntimeError("Stable product ID belongs to an unrecognized product")
            price = await stripe_api(
                "POST",
                "prices",
                {
                    "product": product["id"],
                    "unit_amount": str(dollars * 100),
                    "currency": "usd",
                    "recurring[interval]": "month",
                    "lookup_key": lookup,
                },
                idempotency_key=f"hivemind-price-{tier}-v1",
            )
        products.append((price["product"], price["id"]))
        print(f"STRIPE_PRICE_{'STARTER' if tier == 'starter' else 'PRO'}={price['id']}")
    configs = await stripe_api(
        "GET", "billing_portal/configurations", {"active": "true", "limit": "100"}
    )
    config = next(
        (c for c in configs["data"] if c.get("metadata", {}).get("service") == "hivemind-scale-v1"),
        None,
    )
    if not config:
        data = {
            "business_profile[headline]": "Manage your Hivemind Scale subscription",
            "features[customer_update][enabled]": "true",
            "features[customer_update][allowed_updates][0]": "email",
            "features[customer_update][allowed_updates][1]": "address",
            "features[invoice_history][enabled]": "true",
            "features[payment_method_update][enabled]": "true",
            "features[subscription_cancel][enabled]": "true",
            "features[subscription_cancel][mode]": "at_period_end",
            # Tier changes deliberately operator-controlled in the seven-day beta.
            # A downgrade below current project count needs a customer selection flow.
            "features[subscription_update][enabled]": "false",
            "metadata[service]": "hivemind-scale-v1",
        }
        config = await stripe_api(
            "POST", "billing_portal/configurations", data, idempotency_key="hivemind-portal-v1"
        )
    print(f"STRIPE_PORTAL_CONFIGURATION={config['id']}")
    endpoint_url = f"{public_origin}/billing/webhook"
    endpoints = await stripe_api("GET", "webhook_endpoints", {"limit": "100"})
    endpoint = next(
        (w for w in endpoints["data"] if w["url"] == endpoint_url and w["status"] == "enabled"),
        None,
    )
    if not endpoint:
        if Path(args.webhook_secret_file).exists():
            raise RuntimeError("Signing-secret target already exists; choose a new secure file")
        payload = {
            "url": endpoint_url,
            "api_version": STRIPE_VERSION,
            **{f"enabled_events[{i}]": e for i, e in enumerate(sorted(EVENT_TYPES))},
        }
        endpoint = await stripe_api(
            "POST",
            "webhook_endpoints",
            payload,
            idempotency_key=f"hivemind-webhook-v1:{public_origin}",
        )
        target = Path(args.webhook_secret_file)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(endpoint["secret"] + "\n")
        print(f"New signing secret saved to {target}; load it into STRIPE_WEBHOOK_SECRET")
    else:
        if endpoint["api_version"] != STRIPE_VERSION or not EVENT_TYPES.issubset(
            set(endpoint["enabled_events"])
        ):
            raise RuntimeError(
                "Existing endpoint has unexpected version/events; review it in Stripe Dashboard"
            )
        print("Existing endpoint reused; retain its signing secret from your secret manager")
    print(f"STRIPE_LIVEMODE={'true' if live else 'false'}")


if __name__ == "__main__":
    asyncio.run(main())
