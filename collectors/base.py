import abc
import asyncio
import json
import logging
import backoff
import websockets
from websockets.exceptions import ConnectionClosed
from typing import List, Callable, Coroutine, Any

logger = logging.getLogger(__name__)

class BaseCollector(abc.ABC):
    def __init__(self, name: str, ws_url: str, symbols: List[str], trade_callback: Callable, bbo_callback: Callable):
        self.name = name
        self.ws_url = ws_url
        self.symbols = symbols
        self.trade_callback = trade_callback
        self.bbo_callback = bbo_callback
        self.ws = None

    @abc.abstractmethod
    def get_subscription_payloads(self) -> List[dict]:
        pass

    @abc.abstractmethod
    def parse_message(self, message: str) -> None:
        """Parse incoming WebSocket message and route to appropriate callback."""
        pass

    @backoff.on_exception(
        backoff.expo,
        (ConnectionClosed, websockets.exceptions.WebSocketException, Exception),
        max_tries=None,
        max_time=300
    )
    async def connect_and_run(self):
        logger.info(f"[{self.name}] Connecting to {self.ws_url}...")
        async with websockets.connect(self.ws_url, ping_interval=15, ping_timeout=10) as ws:
            self.ws = ws
            logger.info(f"[{self.name}] Connected.")
            
            # Subscribe
            payloads = self.get_subscription_payloads()
            for payload in payloads:
                await ws.send(json.dumps(payload))
            logger.info(f"[{self.name}] Sent subscription payloads.")

            # Listen
            async for message in ws:
                try:
                    self.parse_message(message)
                except Exception as e:
                    logger.error(f"[{self.name}] Error parsing message: {e} | Message: {message}")

    async def start(self):
        await self.connect_and_run()
