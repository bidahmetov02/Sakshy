"""Reading delivered emails back through Mailpit's HTTP API.

Tests never clear the mailbox (it is shared with the dev stack): each one sends to its own
random address and searches for it.
"""

import asyncio
import secrets
from typing import Any

import httpx2


def new_address() -> str:
    return f"t-{secrets.token_hex(6)}@test.io"


class Mailpit:
    def __init__(self, url: str) -> None:
        self.url = url

    async def messages_to(self, address: str) -> list[dict[str, Any]]:
        """Full messages delivered to `address`, oldest first."""
        async with httpx2.AsyncClient(base_url=self.url) as client:
            found = await client.get("/api/v1/search", params={"query": f'to:"{address}"'})
            found.raise_for_status()
            # By time received: the Date header has only second precision.
            summaries = sorted(found.json()["messages"], key=lambda m: m["Created"])
            return [
                (await client.get(f"/api/v1/message/{m['ID']}")).raise_for_status().json()
                for m in summaries
            ]

    async def wait_for(self, address: str, count: int) -> list[dict[str, Any]]:
        """Poll until `count` messages arrived: delivery happens in another process."""
        async with asyncio.timeout(5):
            while len(messages := await self.messages_to(address)) < count:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        return messages
