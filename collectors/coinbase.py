import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import List
from models.canonical import TradeTick, BBOTick
from collectors.base import BaseCollector

class CoinbaseCollector(BaseCollector):
    def __init__(self, symbols: List[str], trade_callback, bbo_callback):
        # Coinbase symbols are like BTC-USD
        self.native_symbols = [s.replace('/', '-') for s in symbols]
        self.symbol_map = {s.replace('/', '-'): s for s in symbols}

        super().__init__(
            name="coinbase",
            ws_url="wss://advanced-trade-ws.coinbase.com",
            symbols=self.native_symbols,
            trade_callback=trade_callback,
            bbo_callback=bbo_callback
        )

    def get_subscription_payloads(self) -> List[dict]:
        return [{
            "type": "subscribe",
            "channel": "market_trades",
            "product_ids": self.native_symbols
        }, {
            "type": "subscribe",
            "channel": "ticker",
            "product_ids": self.native_symbols
        }, {
            "type": "subscribe",
            "channel": "heartbeats",
            "product_ids": self.native_symbols
        }]

    def parse_message(self, message: str) -> None:
        msg = json.loads(message)
        channel = msg.get("channel")
        
        if channel == "market_trades" and "events" in msg:
            for event in msg["events"]:
                for trade_data in event.get("trades", []):
                    product_id = trade_data["product_id"]
                    canonical_symbol = self.symbol_map.get(product_id, product_id)
                    trade_id = str(trade_data["trade_id"])
                    
                    trade = TradeTick(
                        ts_event=datetime.fromisoformat(trade_data["time"].replace("Z", "+00:00")),
                        ts_ingested=datetime.now(timezone.utc),
                        exchange_code="coinbase",
                        symbol=canonical_symbol,
                        native_symbol=product_id,
                        source_channel="market_trades",
                        event_uid=f"coinbase:{product_id}:trade:{trade_id}",
                        source_sequence=None,
                        trade_id=trade_id,
                        side=trade_data["side"],
                        price=Decimal(str(trade_data["price"])),
                        qty=Decimal(str(trade_data["size"])),
                        quote_qty=Decimal(str(trade_data["price"])) * Decimal(str(trade_data["size"])),
                        is_maker=None, # Taker/maker not explicitly in basic public trade message
                        payload=trade_data
                    )
                    self.trade_callback(trade)
                    
        elif channel == "ticker" and "events" in msg:
            for event in msg["events"]:
                for ticker_data in event.get("tickers", []):
                    product_id = ticker_data["product_id"]
                    canonical_symbol = self.symbol_map.get(product_id, product_id)
                    
                    bbo = BBOTick(
                        ts_event=datetime.now(timezone.utc),
                        ts_ingested=datetime.now(timezone.utc),
                        exchange_code="coinbase",
                        symbol=canonical_symbol,
                        native_symbol=product_id,
                        source_channel="ticker",
                        event_uid=f"coinbase:{product_id}:ticker:{datetime.now(timezone.utc).timestamp()}",
                        source_sequence=None,
                        bid_px=Decimal(str(ticker_data["best_bid"])),
                        bid_qty=Decimal(str(ticker_data["best_bid_quantity"])),
                        ask_px=Decimal(str(ticker_data["best_ask"])),
                        ask_qty=Decimal(str(ticker_data["best_ask_quantity"])),
                        payload=ticker_data
                    )
                    self.bbo_callback(bbo)
