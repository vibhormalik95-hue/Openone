#!/usr/bin/env python3
"""Read HVM_BASE_URL and optional MCP_TOKEN from the process environment.
A token is optional for public health checks; it is never printed or put in the URL.
"""
import json
import os
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'Smoke redirects are refused', headers, fp)


opener = urllib.request.build_opener(NoRedirect)
base = os.environ['HVM_BASE_URL'].rstrip('/')
loopback = base == 'http://127.0.0.1:8000' and os.environ.get('HVM_ALLOW_LOOPBACK') == 'true'
if not base.startswith('https://') and not loopback:
    raise SystemExit('Smoke checks require HTTPS, except explicit in-container loopback.')
for path in ('/health/live', '/health/ready'):
    with opener.open(base + path, timeout=10) as response:  # noqa: S310 -- HTTPS or fixed loopback validated above
        assert response.status == 200, path
headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
token = os.environ.get('MCP_TOKEN')
if token:
    headers['Authorization'] = 'Bearer ' + token


def rpc(payload):
    request = urllib.request.Request(base + '/mcp/v1', data=json.dumps(payload).encode(), headers=headers)  # noqa: S310 -- validated base
    with opener.open(request, timeout=30) as response:  # noqa: S310 -- validated base
        if session := response.headers.get('Mcp-Session-Id'):
            headers['Mcp-Session-Id'] = session
        if response.status == 202:
            return None
        if response.headers.get_content_type() == 'text/event-stream':
            for raw in response:
                if raw.startswith(b'data: '):
                    message = json.loads(raw[6:])
                    if message.get('id') == payload.get('id'):
                        assert 'error' not in message, 'MCP error response'
                        return message['result']
            raise AssertionError('Stream ended before expected MCP result')
        message = json.load(response)
        assert 'error' not in message, 'MCP error response'
        return message['result']


if token:
    initialized = rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-11-25', 'capabilities': {},
        'clientInfo': {'name': 'hivemind-deploy-smoke', 'version': '1.0.0'}}})
    headers['MCP-Protocol-Version'] = initialized['protocolVersion']
    rpc({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    listed = rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
    assert {tool['name'] for tool in listed['tools']} == {'sync_context', 'manage_ledger'}
    if project := os.environ.get('HVM_SMOKE_PROJECT_ID'):
        from uuid import UUID
        project = str(UUID(project))
        recalled = rpc({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
            'name': 'sync_context', 'arguments': {'request': {
                'project_id': project, 'query': 'deployment canary project context',
                'checkpoint': {'state': 'no_new_context'}}}}})
        assert not recalled.get('isError'), 'Canary recall failed'
        packet = recalled.get('structuredContent')
        if packet is None:
            packet = json.loads(next(item['text'] for item in recalled['content'] if item['type'] == 'text'))
        assert packet['project_id'] == project, 'Canary returned the wrong project'
        assert packet['constraints_complete'] is True, 'Exact context incomplete'
        assert packet['semantic_status'] == 'ready', 'Semantic provider unavailable'
        print('Authenticated MCP initialize/tools/list and canary recall passed.')
    else:
        print('Health and authenticated MCP initialize/tools/list passed; canary recall not configured.')
else:
    print('HTTPS health passed. Authenticated MCP check skipped: MCP_TOKEN was not set.')
