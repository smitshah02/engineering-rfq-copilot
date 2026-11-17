"""Deterministically hold two decision requests after both initially miss their receipt."""

import asyncio

from rfq_copilot import quote_approval


def hold_receipt_misses(monkeypatch, operation):
    barrier = asyncio.Barrier(2)
    original = quote_approval._replay
    arrivals = []

    async def replay(session, principal, key, digest, requested_operation):
        result = await original(session, principal, key, digest, requested_operation)
        if requested_operation == operation and result is None and len(arrivals) < 2:
            arrivals.append(requested_operation)
            await barrier.wait()
        return result

    monkeypatch.setattr(quote_approval, "_replay", replay)
    return arrivals
