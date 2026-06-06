from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Any
from decimal import Decimal

@dataclass
class MarketRef:
    exchange_code: str
    symbol: str
    native_symbol: str
    base_asset: str
    quote_asset: str
    market_type: str
    status: str
    active: bool
    price_precision: Optional[int]
    qty_precision: Optional[int]
    meta: dict[str, Any]

@dataclass
class TradeTick:
    ts_event: datetime
    ts_ingested: datetime
    exchange_code: str
    symbol: str
    native_symbol: str
    source_channel: str
    event_uid: str
    source_sequence: Optional[int]
    trade_id: Optional[str]
    side: str
    price: Decimal
    qty: Decimal
    quote_qty: Optional[Decimal]
    is_maker: Optional[bool]
    payload: dict[str, Any]

@dataclass
class BBOTick:
    ts_event: datetime
    ts_ingested: datetime
    exchange_code: str
    symbol: str
    native_symbol: str
    source_channel: str
    event_uid: str
    source_sequence: Optional[int]
    bid_px: Decimal
    bid_qty: Decimal
    ask_px: Decimal
    ask_qty: Decimal
    payload: dict[str, Any]
