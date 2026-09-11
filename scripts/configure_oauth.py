#!/usr/bin/env python3
"""Configure the optional managed-OAuth overlay interactively; keeps secrets off argv."""
import getpass
import json
import os
import secrets
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(0o600)


def update_env(path, updates):
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if line.partition("=")[0] not in updates]
    if any("'" in value or "\n" in value or "\r" in value for value in updates.values()):
        raise ValueError("Environment values must not contain quotes or newlines")
    lines.extend(f"{key}='{value}'" for key, value in updates.items())
    write_private(path, "\n".join(lines) + "\n")


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    deploy = root / "deploy"
    deploy.mkdir(exist_ok=True)
    deploy.chmod(0o700)
    origin = input("Hivemind HTTPS origin: ").strip().rstrip("/")
    issuer = input("Exact Auth0 issuer, including trailing /: ").strip()
    client_id = input("Auth0 Regular Web Application client ID: ").strip()
    link_id = input("Auth0 Native linking application client ID: ").strip()
    client_secret = getpass.getpass("Auth0 Regular Web Application client secret: ").strip()
    redirects = json.loads(input("Exact hosted client redirect URIs, JSON array: ").strip())
    if not isinstance(redirects, list) or not redirects:
        raise SystemExit("At least one exact redirect URI is required")
    values = {"AUTH_MODE": "oauth", "ENABLE_LEGACY_SSE": "false", "HVM_PUBLIC_ORIGIN": origin, "AUTH0_ISSUER": issuer,
              "AUTH0_CLIENT_ID": client_id, "AUTH0_LINK_CLIENT_ID": link_id,
              "OAUTH_REDIRECT_URIS_JSON": json.dumps(redirects, separators=(",", ":")),
              "OAUTH_REDIS_URL": "redis://oauth-redis:6379/0"}
    from hivemind.oauth import issuer_url, public_origin
    os.environ.update(values)
    issuer_url()
    public_origin()
    for value in values.values():
        if "\n" in value or "\r" in value:
            raise SystemExit("Newlines are not accepted in configuration values")
    generated = {
        "oauth_redis_password": secrets.token_urlsafe(48),
        "oauth_jwt_signing_key": secrets.token_urlsafe(48),
        "oauth_storage_encryption_key": Fernet.generate_key().decode(),
        "auth0_client_secret": client_secret,
    }
    for name, value in generated.items():
        path = deploy / "secrets" / name
        if path.exists() and name != "auth0_client_secret":
            generated[name] = path.read_text().strip()  # Do not rotate live keys on rerun.
        else:
            write_private(path, value + "\n")
        values[name.upper()] = generated[name]
    config = ("bind 0.0.0.0\nprotected-mode yes\nport 6379\nappendonly yes\n"
              "appendfsync everysec\nsave 900 1\ndir /data\nmaxmemory 192mb\n"
              "maxmemory-policy noeviction\nrequirepass " + generated["oauth_redis_password"] + "\n")
    write_private(deploy / "oauth-redis.conf", config)
    # Container redis user must read the mounted file; parent is private on host.
    (deploy / "oauth-redis.conf").chmod(0o644)
    update_env(deploy / "runtime.env", values)
    tag = "redis:7.4-alpine"
    subprocess.run(["/usr/bin/docker", "pull", tag], check=True)  # noqa: S603 -- fixed argv
    image = subprocess.check_output(  # noqa: S603 -- fixed argv, no shell
        ["/usr/bin/docker", "image", "inspect", tag, "--format", "{{index .RepoDigests 0}}"], text=True).strip()
    if "@sha256:" not in image:
        raise SystemExit("Could not resolve immutable Redis image digest")
    update_env(deploy / "versions.env", {"REDIS_IMAGE": image})
    # Preserve the dedicated-host non-root overlay; it already provisions Redis.
    host = (deploy / "host.env").read_text()
    overlay = "deploy/compose.infra.yml" if "COMPOSE_EXTRA='deploy/compose.infra.yml'" in host else "compose.oauth.yml"
    update_env(deploy / "host.env", {"COMPOSE_EXTRA": overlay})
    print("Configured. OAuth overlay persisted in deploy/host.env for future deployments.")
    print("Next: run the documented Auth0 linking and native-client release gates.")


if __name__ == "__main__":
    main()
