#!/usr/bin/env python3
"""Bounded read-only MCP load test. Requires the project's dev/client dependencies."""
import argparse
import asyncio
import json
import os
import statistics
import time
from uuid import UUID

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def run(args):
    if not os.environ.get('HIVEMIND_TOKEN'):
        raise SystemExit('Set HIVEMIND_TOKEN in this process environment.')
    UUID(args.project)
    if not args.url.startswith('https://') and not args.url.startswith('http://127.0.0.1:'):
        raise SystemExit('Use HTTPS, or a loopback address for local tests.')
    samples, failures = [], 0
    started = time.monotonic()

    async def user():
        nonlocal failures
        try:
            transport = StreamableHttpTransport(
                args.url, headers={'Authorization': 'Bearer ' + os.environ['HIVEMIND_TOKEN']}
            )
            async with Client(transport, timeout=30) as client:
                for _ in range(args.requests):
                    before = time.monotonic()
                    try:
                        result = await client.call_tool('sync_context', {'request': {
                            'project_id': args.project, 'query': args.query,
                            'checkpoint': {'state': 'no_new_context'}
                        }})
                        if result.is_error:
                            failures += 1
                        else:
                            samples.append((time.monotonic() - before) * 1000)
                    except Exception:
                        failures += 1
        except Exception:
            failures += args.requests

    await asyncio.gather(*(user() for _ in range(args.concurrency)))
    ordered = sorted(samples)
    def percentile(p):
        return round(ordered[min(len(ordered)-1, int((len(ordered)-1)*p))], 1) if ordered else None
    print(json.dumps({'attempted': args.requests * args.concurrency, 'successful': len(samples),
        'failures': failures, 'p50_ms': percentile(.5), 'p95_ms': percentile(.95),
        'p99_ms': percentile(.99), 'mean_ms': round(statistics.mean(samples), 1) if samples else None,
        'elapsed_s': round(time.monotonic()-started, 2)}, indent=2))
    return 1 if failures or (ordered and percentile(.95) > args.max_p95_ms) else 0


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', required=True)
    p.add_argument('--project', required=True)
    p.add_argument('--concurrency', type=int, choices=range(1, 101), default=10)
    p.add_argument('--requests', type=int, choices=range(1, 101), default=20)
    p.add_argument('--query', default='What decisions and constraints govern this project?')
    p.add_argument('--max-p95-ms', type=int, default=2000)
    raise SystemExit(asyncio.run(run(p.parse_args())))
