#!/usr/bin/env python3
"""Generate private, reviewable native MCP client configs; never modify installed apps.

The default output contains a project key. Run locally in a private directory.
OAuth setup is a separate, real login flow: metadata does not turn an API key into OAuth.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import ipaddress
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_METADATA_BYTES = 65_536
BRIDGE_SOURCE = '''"""Private local stdio-to-HTTP bridge for Claude Desktop; FastMCP 3.4.7."""
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy


async def main():
    config_path = Path(__file__).with_name("desktop_bridge_settings.json")
    info = config_path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Bridge settings must be a regular file")
    if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("Bridge settings must be owned by you with mode 0600")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    transport = StreamableHttpTransport(
        config["url"], headers={"Authorization": "Bearer " + config["key"]}
    )
    async with Client(transport, timeout=40, init_timeout=20) as upstream:
        initialized = await upstream.initialize()
        proxy = create_proxy(
            upstream, name="Hivemind Scale", instructions=initialized.instructions,
            provider_error_strategy="raise",
        )
        await proxy.run_async(transport="stdio", show_banner=False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        print("Hivemind bridge failed. Check endpoint, key and network access.", file=sys.stderr)
        raise SystemExit(1) from None
'''


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("OAuth metadata must be served without redirecting")


def canonical_origin(value: str) -> str:
    """Accept only the deployment's HTTPS root origin, with no credential-bearing URL."""
    if any(ord(char) < 33 for char in value) or "\\" in value:
        raise ValueError("The base URL must be an HTTPS origin without whitespace")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("Use the HTTPS deployment origin only, without path, credentials or query")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("The base URL has an invalid port") from exc
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        if "%" in host:
            raise ValueError("IPv6 scope identifiers are not valid public deployment origins")
        host = "[" + ipaddress.IPv6Address(host).compressed + "]"
    elif len(host) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in host.split(".")
    ):
        raise ValueError("The deployment hostname is invalid")
    return "https://" + host + (":" + str(port) if port else "")


def validate_key(value: str) -> str:
    if not re.fullmatch(r"hvm_[A-Za-z0-9_-]{40,128}", value):
        raise ValueError("Expected an issued hvm_ project key, 44 to 132 characters")
    return value


def validate_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("Name must be 1 to 64 letters, numbers, underscores or hyphens")
    return value


def encode_json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def cursor_link(name: str, entry: dict) -> str:
    """Cursor's documented URI contains only URL configuration, never the project key."""
    encoded = base64.b64encode(json.dumps(entry, separators=(",", ":")).encode()).decode()
    return "cursor://anysphere.cursor-deeplink/mcp/install?" + urlencode({
        "name": name, "config": encoded,
    })


def oauth_endpoint_inventory(origin: str) -> dict:
    return {
        "authentication_mode_required": "oauth",
        "mcp_url": origin + "/mcp/v1",
        "resource_metadata_url": origin + "/.well-known/oauth-protected-resource/mcp/v1",
        "authorization_server_metadata_url": origin + "/.well-known/oauth-authorization-server",
        "issuer": origin + "/",
        "authorization_endpoint": origin + "/authorize",
        "token_endpoint": origin + "/token",
        "registration_endpoint": origin + "/register",
        "upstream_callback": origin + "/auth/callback",
        "account_link_page": origin + "/account/",
        "required_scope": "memory:access",
        "chatgpt_install_manifest": None,
        "metadata_status": "expected routes of the pinned deployment, not a live verification",
    }


def fetch_json(url: str) -> dict:
    if urlsplit(url).scheme != "https":
        raise ValueError("OAuth discovery requires HTTPS")
    request = Request(url, headers={"Accept": "application/json"}, method="GET")  # noqa: S310 -- HTTPS verified
    with build_opener(NoRedirects()).open(request, timeout=10) as response:  # noqa: S310 -- validated HTTPS origin, fixed path
        if response.headers.get_content_type() != "application/json":
            raise ValueError("OAuth discovery must return application/json")
        raw = response.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("OAuth discovery document exceeds size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("OAuth discovery must return a JSON object")
    return value


def verify_oauth(origin: str, fetch=fetch_json) -> dict[str, str]:
    """Verify deployed metadata without transmitting the personal key or following redirects."""
    endpoints = oauth_endpoint_inventory(origin)
    resource = fetch(endpoints["resource_metadata_url"])
    if (resource.get("resource") != endpoints["mcp_url"]
            or resource.get("authorization_servers") != [origin + "/"]
            or "memory:access" not in resource.get("scopes_supported", [])):
        raise ValueError("Protected-resource metadata does not match this deployment")
    metadata = fetch(endpoints["authorization_server_metadata_url"])
    for field in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if metadata.get(field) != endpoints[field]:
            raise ValueError("Authorization-server metadata has an unexpected " + field)
    if "S256" not in metadata.get("code_challenge_methods_supported", []):
        raise ValueError("Authorization server must advertise S256 PKCE")
    if "authorization_code" not in metadata.get("grant_types_supported", []):
        raise ValueError("Authorization server must support authorization_code")
    return {
        "oauth-protected-resource.json": encode_json(resource),
        "oauth-authorization-server.json": encode_json(metadata),
    }


def build_files(
    origin: str, key: str, output: Path, name: str, python: str, *, claude_code_hooks: bool = False,
) -> dict[str, str]:
    endpoint = origin + "/mcp/v1"
    cursor_entry = {"url": endpoint, "headers": {"Authorization": "Bearer " + key}}
    code_entry = {
        "type": "http", "url": endpoint,
        "headers": {"Authorization": "Bearer ${HVM_PROJECT_KEY}"},
    }
    desktop_entry = {"command": python, "args": [str(output / "desktop_bridge.py")]}
    add_command = " ".join(shlex.quote(part) for part in [
        "claude", "mcp", "add", "--transport", "http", "--scope", "user", name,
        endpoint, "--header", "Authorization: Bearer ${HVM_PROJECT_KEY}",
    ])
    key_env = str(output / "key.env")
    launcher = (
        "#!/bin/sh\nset -eu\nset +x\n. " + shlex.quote(key_env)
        + "\nexec claude --mcp-config " + shlex.quote(str(output / ".mcp.json")) + ' "$@"\n'
    )
    setup = "#!/bin/sh\nset -eu\nset +x\n. " + shlex.quote(key_env) + "\n" + add_command + "\n"
    powershell_prefix = (
        "$ErrorActionPreference = 'Stop'\n"
        "$hvmSettings = Join-Path $PSScriptRoot 'desktop_bridge_settings.json'\n"
        "$env:HVM_PROJECT_KEY = (Get-Content -LiteralPath $hvmSettings -Raw | ConvertFrom-Json).key\n"
    )
    powershell_add = (
        f"& claude mcp add --transport http --scope user '{name}' '{endpoint}' "
        "--header 'Authorization: Bearer ${HVM_PROJECT_KEY}'\nexit $LASTEXITCODE\n"
    )
    inventory = oauth_endpoint_inventory(origin)
    files = {
        "claude_desktop_config.json": encode_json({"mcpServers": {name: desktop_entry}}),
        "desktop_bridge.py": BRIDGE_SOURCE,
        "desktop_bridge_settings.json": encode_json({"url": endpoint, "key": key}),
        "cursor.mcp.json": encode_json({"mcpServers": {name: cursor_entry}}),
        "mcp.json": encode_json({"mcpServers": {name: cursor_entry}}),
        ".mcp.json": encode_json({"mcpServers": {name: code_entry}}),
        "key.env": "export HVM_PROJECT_KEY=" + shlex.quote(key) + "\n",
        "claude-code-add.sh": setup,
        "claude-code-run.sh": launcher,
        "claude-code-add.ps1": powershell_prefix + powershell_add,
        "claude-code-run.ps1": powershell_prefix + (
            "& claude --mcp-config (Join-Path $PSScriptRoot '.mcp.json') @args\nexit $LASTEXITCODE\n"
        ),
        "oauth-endpoints.json": encode_json(inventory),
        "chatgpt-connection.json": encode_json({
            "name": name, "mcp_server_url": endpoint,
            "authentication": "OAuth authorization code with S256 PKCE",
            "client_registration": "Dynamic Client Registration (DCR)",
            "setup_page": "https://chatgpt.com/plugins",
            "account_link_page": origin + "/account/",
            "requires": ["Deployed OAuth overlay", "Paid key linked to login identity",
                         "Exact client redirect URI in server allowlist", "Host user consent"],
            "format": "Operator setup data; not a ChatGPT-importable manifest",
            "documented_surface": "ChatGPT web Developer mode",
            "desktop_mac_acceptance": "Not independently verified; use the documented web setup",
        }),
        "cursor-oauth-install.txt": cursor_link(name, {"url": endpoint}) + "\n",
        "CONNECT.md": f"""# Connect {name}

MCP URL: {endpoint}

These files were generated locally. They do not install integrations or change host permissions.
The key selects its authorized tenant and project; no project-identification prompt is needed.
This directory contains credentials. Keep it private, outside source control and shared folders.

## Claude Desktop

Prefer account-based remote OAuth at https://claude.ai/customize/connectors after linking
at {origin}/account/. This route can be available across your Claude devices.
For a local Desktop installation, merge the generated claude_desktop_config.json entry into
Settings > Developer > Edit Config. The generated stdio bridge uses {python} and requires
fastmcp-slim[server,client]==3.4.7 installed in that interpreter. Keep this output directory
in place; generated paths are absolute. Restart Desktop. This local bridge is not a mobile connector.

## Cursor

Merge cursor.mcp.json into .cursor/mcp.json for one workspace or ~/.cursor/mcp.json for your
user. This file contains the key, so use a private untracked config and restrictive permissions.
The cursor-oauth-install.txt URI instead contains only the endpoint and starts Cursor's install
flow. It needs the deployed OAuth overlay plus an allowed callback for your Cursor surface.
It never embeds the personal key in a URL. The included OAuth configuration is intended for
hosted HTTPS callback clients; local desktop OAuth callbacks need a separately validated allowlist.

## Claude Code

Run `sh {shlex.quote(str(output / 'claude-code-run.sh'))}` to use the generated remote config.
It reads the key into the child environment and forwards command-line arguments.
Windows PowerShell: run `./claude-code-run.ps1` from this directory; `./claude-code-add.ps1`
provides persistent user-scope registration. These scripts read the private JSON file, not a
secret passed as a process argument.
To register it in user scope, run `sh {shlex.quote(str(output / 'claude-code-add.sh'))}`.
The exact generated registration command is:

```sh
{add_command}
```

That command stores the literal environment reference, not the key. For later plain `claude`
invocations, source key.env into that shell first. The run script does this automatically.
Connection registration and tool permissions remain separate host controls.

## ChatGPT / native Claude OAuth

Deploy and configure the real OAuth overlay before this step. Link the paid key to your login
identity at {origin}/account/. ChatGPT: enable Developer mode under Settings > Security and
login, open https://chatgpt.com/plugins, create the MCP app with {endpoint}, select OAuth
and DCR, and use the exact callback URI shown by that connection in the server allowlist.
Sign in, then enable the integration in your conversation. Claude: Customize > Connectors >
Add custom connector, enter {endpoint}, then connect and enable it for the conversation.
Tool approvals and integration availability are controlled by each host/account/workspace.

`oauth-endpoints.json` lists the actual route contract of the pinned server. With
`--verify-oauth`, the generator fetches and saves the live resource and authorization-server
metadata, failing if they disagree with the deployment. Those JSON files are evidence snapshots;
the running FastMCP service serves discovery. `chatgpt-connection.json` is setup information,
not a supported import manifest. No universal ChatGPT or Claude one-click installation URL exists
in the cited documentation. Custom GPT Actions need a separate OpenAPI adapter.

No model prompt is pasted during this setup. Initialization instructions request automatic
memory use. Only the host can decide to call tools, display calls or require confirmation;
connecting does not give a server access to every conversation turn.
""",
    }


    if claude_code_hooks:
        files.update(build_hook_files(origin, key, output, python))
        files["CONNECT.md"] += HOOK_INSTRUCTIONS
    return files


def build_hook_files(origin: str, key: str, output: Path, python: str) -> dict[str, str]:
    handler = {
        "type": "command", "command": python,
        "args": [str(output / "claude_code_hook.py")], "timeout": 15,
    }
    return {
        "claude_code_hook.py": Path(__file__).with_name("claude_code_hook.py").read_text(
            encoding="utf-8"
        ),
        "desktop_bridge_settings.json": encode_json({
            "url": origin + "/mcp/v1", "key": key, "hook_capture_opt_in": True,
        }),
        "claude-code-hooks.settings.json": encode_json({
            "hooks": {
                "UserPromptSubmit": [{"hooks": [handler]}],
                "Stop": [{"hooks": [handler]}],
            },
        }),
    }


HOOK_INSTRUCTIONS = """
## Opt-in Claude Code automatic capture

You explicitly enabled hook generation. Merge the hooks from
claude-code-hooks.settings.json into your project's .claude/settings.local.json.
Keep this private directory in place. The command uses exec-form arguments, so paths
are not shell-expanded. These hooks consume the host's UserPromptSubmit prompt and
Stop last_assistant_message fields. They never open a transcript file, capture tool
results, read hidden reasoning, or change tool approvals. The private setting
hook_capture_opt_in must remain true. Remove these hook entries to disable capture.

Each prompt triggers recall and bounded raw-text capture before Claude processes it.
The final visible answer is captured after a normal Stop event. Captures are proposals;
only an authorized structured ledger write can establish accepted constraints.
The generator does not configure hooks for other products or for cloud sessions where
these local paths are unavailable. This is a documented Claude Code adapter, not a
universal transcript bridge. It requires a current Claude Code build with command-hook
exec-form args support. Refer to https://code.claude.com/docs/en/hooks.

Input is limited to 12,000 characters and 16,000 UTF-8 bytes; longer text is rejected
without transmitting a truncated fragment. Credential patterns are rejected before
network use, but pattern matching cannot identify every secret. Use the opt-in only
for projects whose ordinary prompts and final answers may be stored by Hivemind and
processed by its configured extraction provider. Source code and personal information
inside those fields are included. Nothing is accepted merely because a hook captured it.

The adapter has a 12-second network deadline and does not trap the user in a retry loop.
Failures report an incomplete checkpoint. Host policy can disable hooks, host timeout
can discard their output, and interrupts/API failures may omit Stop events. Native
Claude Code execution remains a deployment acceptance test; generated-hook regression
tests do not prove invocation in every client version.
"""


def protect_windows_path(path: Path, *, directory: bool) -> None:
    """Apply a protected owner-only DACL before any credential bytes are written.

    Windows chmod does not implement POSIX confidentiality. WinAPI failure aborts
    generation. OW is the well-known Owner Rights SID, not a supplied username.
    """
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
    convert.restype = wintypes.BOOL
    set_security = advapi.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    local_free = kernel.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    sddl = "D:P(A;OICI;FA;;;OW)" if directory else "D:P(A;;FA;;;OW)"
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
        if not set_security(str(path), 0x00000004 | 0x80000000, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        local_free(descriptor)


def private_output_directory(output: Path) -> Path:
    output = output.expanduser().absolute()
    for ancestor in (output, *output.parents):
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise ValueError("Output directories must not contain symbolic links")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = output.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Output path must be a directory")
    if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("Output directory must be owned by you with mode 0700")
    if os.name == "nt":
        protect_windows_path(output, directory=True)
    elif os.name != "posix":
        raise ValueError("Unsupported operating system for private credential files")
    return output


def write_files(output: Path, files: dict[str, str], *, overwrite: bool = False) -> None:
    """Each file is atomically published, mode 0600, with race-safe no-clobber default."""
    for name in files:
        if Path(name).name != name:
            raise ValueError("Generated filenames must not include directories")
        target = output / name
        if target.is_symlink():
            raise ValueError("Refusing to write a symbolic-link target")
        if target.exists() and (not overwrite or not target.is_file()):
            raise FileExistsError("Output exists; select a new directory or use --overwrite")
    for name, content in files.items():
        fd, raw_temp = tempfile.mkstemp(prefix=".hvm-", dir=output)
        temporary = Path(raw_temp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                os.chmod(temporary, 0o600)
                if os.name == "nt":
                    protect_windows_path(temporary, directory=False)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                os.replace(temporary, output / name)
            else:
                os.link(temporary, output / name)
        finally:
            temporary.unlink(missing_ok=True)
    if os.name == "posix":
        directory_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Actual deployment HTTPS origin")
    parser.add_argument("--output", type=Path, default=Path("client-config"))
    parser.add_argument("--name", default="hivemind-scale")
    parser.add_argument("--key-env", default="HVM_PROJECT_KEY", help="Environment variable holding the issued key")
    parser.add_argument("--python", default=sys.executable, help="Absolute local interpreter path for Desktop's bridge")
    parser.add_argument("--overwrite", action="store_true", help="Atomically replace files in the output directory")
    parser.add_argument("--verify-oauth", action="store_true", help="Fetch and validate public OAuth discovery before writing")
    parser.add_argument(
        "--claude-code-hooks", action="store_true",
        help="Opt in to generating hooks that transmit bounded user prompts and final answers as tentative text",
    )
    args = parser.parse_args(argv)
    try:
        origin = canonical_origin(args.base_url)
        name = validate_name(args.name)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.key_env):
            raise ValueError("Invalid key environment-variable name")
        if not Path(args.python).is_absolute() or not Path(args.python).is_file():
            raise ValueError("--python must identify an existing absolute interpreter path")
        key_value = os.environ.get(args.key_env)
        if key_value is None:
            if not sys.stdin.isatty():
                raise ValueError("Set the key environment variable or run in an interactive terminal")
            key_value = getpass.getpass("Issued Hivemind project key: ")
        key = validate_key(key_value)
        output = args.output.expanduser().absolute()
        files = build_files(
            origin, key, output, name, args.python, claude_code_hooks=args.claude_code_hooks,
        )
        if args.verify_oauth:
            files.update(verify_oauth(origin))
        output = private_output_directory(output)
        write_files(output, files, overwrite=args.overwrite)
    except (ValueError, OSError) as exc:
        parser.exit(2, "Could not generate configs: " + str(exc) + "\n")
    print("Created private client configs in " + str(output))
    print("Read CONNECT.md; native OAuth and host consent require the documented setup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
