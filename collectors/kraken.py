import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import List
from models.canonical import TradeTick, BBOTick
from collectors.base import BaseCollector

class KrakenCollector(BaseCollector):
    def __init__(self, symbols: List[str], trade_callback, bbo_callback):
        # Kraken symbols in WS v2 are like "BTC/USD"
        self.native_symbols = symbols
        self.symbol_map = {s: s for s in symbols}

        super().__init__(
            name="kraken",
            ws_url="wss://ws.kraken.com/v2",
            symbols=self.native_symbols,
            trade_callback=trade_callback,
            bbo_callback=bbo_callback
        )

    def get_subscription_payloads(self) -> List[dict]:
        return [
            {
                "method": "subscribe",
                "params": {
                    "channel": "trade",
                    "symbol": self.native_symbols
                }
            },
            {
                "method": "subscribe",
                "params": {
                    "channel": "ticker",
                    "symbol": self.native_symbols
                }
            }
        ]

    def parse_message(self, message: str) -> None:
        msg = json.loads(message)
        channel = msg.get("channel")
        
        if channel == "trade" and msg.get("type") == "update":
            for data in msg.get("data", []):
                symbol = data["symbol"]
                canonical_symbol = self.symbol_map.get(symbol, symbol)
                trade_id = str(data["trade_id"])
                
                trade = TradeTick(
                    ts_event=datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00")),
                    ts_ingested=datetime.now(timezone.utc),
                    exchange_code="kraken",
                    symbol=canonical_symbol,
                    native_symbol=symbol,
                    source_channel="trade",
                    event_uid=f"kraken:{symbol}:trade:{trade_id}",
                    source_sequence=None,
                    trade_id=trade_id,
                    side=data["side"],
                    price=Decimal(str(data["price"])),
                    qty=Decimal(str(data["qty"])),
                    quote_qty=Decimal(str(data["price"])) * Decimal(str(data["qty"])),
                    is_maker=None, # Kraken v2 trade stream doesn't easily expose maker/taker
                    payload=data
                )
                self.trade_callback(trade)
                
        elif channel == "ticker" and msg.get("type") == "update":
            for data in msg.get("data", []):
                symbol = data["symbol"]
                canonical_symbol = self.symbol_map.get(symbol, symbol)
                
                bbo = BBOTick(
                    ts_event=datetime.now(timezone.utc), # ticker might not have exact ts in v2 payload without parsing subfields
                    ts_ingested=datetime.now(timezone.utc),
                    exchange_code="kraken",
                    symbol=canonical_symbol,
                    native_symbol=symbol,
                    source_channel="ticker",
                    event_uid=f"kraken:{symbol}:ticker:{datetime.now(timezone.utc).timestamp()}",
                    source_sequence=None,
                    bid_px=Decimal(str(data["bid"])),
                    bid_qty=Decimal(str(data["bid_qty"])),
                    ask_px=Decimal(str(data["ask"])),
                    ask_qty=Decimal(str(data["ask_qty"])),
                    payload=data
                )
                self.bbo_callback(bbo)
