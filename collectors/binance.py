import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import List
from models.canonical import TradeTick, BBOTick
from collectors.base import BaseCollector

def utc_now():
    return datetime.now(timezone.utc)

def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

class BinanceCollector(BaseCollector):
    def __init__(self, symbols: List[str], trade_callback, bbo_callback):
        # symbols arrive as "BTC/USDT", need to transform to "btcusdt"
        self.native_symbols = [s.replace('/', '').lower() for s in symbols]
        self.symbol_map = {s.replace('/', '').upper(): s for s in symbols}
        
        super().__init__(
            name="binance",
            ws_url="wss://stream.binance.com:9443/ws",
            symbols=self.native_symbols,
            trade_callback=trade_callback,
            bbo_callback=bbo_callback
        )

    def get_subscription_payloads(self) -> List[dict]:
        params = []
        for s in self.native_symbols:
            params.append(f"{s}@aggTrade")
            params.append(f"{s}@bookTicker")
        
        return [{
            "method": "SUBSCRIBE",
            "params": params,
            "id": 1
        }]

    def parse_message(self, message: str) -> None:
        msg = json.loads(message)
        event_type = msg.get("e")

        if event_type == "aggTrade":
            canonical_symbol = self.symbol_map.get(msg["s"], msg["s"])
            trade = TradeTick(
                ts_event=ms_to_dt(msg["T"]),
                ts_ingested=utc_now(),
                exchange_code="binance",
                symbol=canonical_symbol,
                native_symbol=msg["s"],
                source_channel="aggTrade",
                event_uid=f"binance:{msg['s']}:aggTrade:{msg['a']}:{msg['T']}",
                source_sequence=None,
                trade_id=str(msg["a"]),
                side="sell" if msg.get("m") else "buy",
                price=Decimal(msg["p"]),
                qty=Decimal(msg["q"]),
                quote_qty=Decimal(msg["p"]) * Decimal(msg["q"]),
                is_maker=bool(msg.get("m")),
                payload=msg
            )
            self.trade_callback(trade)
        elif "u" in msg and "b" in msg and "a" in msg and "B" in msg and "A" in msg:
            # bookTicker event (no 'e' field for bookTicker usually unless via stream wrapper)
            canonical_symbol = self.symbol_map.get(msg["s"], msg["s"])
            event_ms = int(time.time() * 1000) # Binance spot bookTicker often lacks exact timestamp
            bbo = BBOTick(
                ts_event=ms_to_dt(event_ms),
                ts_ingested=utc_now(),
                exchange_code="binance",
                symbol=canonical_symbol,
                native_symbol=msg["s"],
                source_channel="bookTicker",
                event_uid=f"binance:{msg['s']}:bookTicker:{msg['u']}",
                source_sequence=int(msg["u"]),
                bid_px=Decimal(msg["b"]),
                bid_qty=Decimal(msg["B"]),
                ask_px=Decimal(msg["a"]),
                ask_qty=Decimal(msg["A"]),
                payload=msg
            )
            self.bbo_callback(bbo)
