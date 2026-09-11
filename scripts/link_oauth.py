#!/usr/bin/env python3
"""Link a paid Hivemind key to a managed Auth0 identity; no secrets in argv."""
import argparse
import getpass
import time
import webbrowser
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--client-id", required=True)
    args = parser.parse_args()
    origin, issuer = args.origin.rstrip("/"), args.issuer.rstrip("/") + "/"
    for value in (origin, issuer):
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.query or url.fragment or url.username:
            parser.error("origin and issuer must be trusted HTTPS URLs")
    key = getpass.getpass("Paste your Hivemind MCP key (hidden): ").strip()
    if not key.startswith("hvm_"):
        parser.error("Expected a Hivemind hvm_ key")
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.post(issuer + "oauth/device/code", data={
            "client_id": args.client_id,
            "audience": origin + "/mcp/v1",
            "scope": "openid memory:access",
        })
        response.raise_for_status()
        device = response.json()
        print("Visit", device["verification_uri"], "and confirm code", device["user_code"])
        webbrowser.open(device.get("verification_uri_complete", device["verification_uri"]))
        interval = max(1, int(device.get("interval", 5)))
        deadline = time.monotonic() + int(device["expires_in"])
        while time.monotonic() < deadline:
            time.sleep(interval)
            response = client.post(issuer + "oauth/token", data={
                "client_id": args.client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device["device_code"],
            })
            token = response.json()
            if response.status_code == 200:
                break
            if token.get("error") == "slow_down":
                interval += 5
            elif token.get("error") != "authorization_pending":
                raise SystemExit("Auth0 authorization failed: " + token.get("error", "unknown"))
        else:
            raise SystemExit("Authorization expired; run the command again.")
        response = client.post(origin + "/account/link-oauth",
            headers={"Authorization": "Bearer " + token["access_token"]},
            json={"api_key": key})
        if response.status_code != 200:
            raise SystemExit("Account linking failed: " + str(response.status_code))
        print("Linked. Add this URL to your native MCP client:", response.json()["mcp_url"])


if __name__ == "__main__":
    main()
