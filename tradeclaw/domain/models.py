from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class MarketContext:
    symbol_to_price: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class InstrumentKey:
    symbol: str
    market: str


@dataclass
class Bar:
    symbol: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    adjust_type: str = "none"


@dataclass
class Quote:
    symbol: str
    price: float
    timestamp: str


@dataclass
class AccountSnapshot:
    cash: float
    equity: float


@dataclass
class PositionSnapshot:
    symbol: str
    quantity: float
    cost_price: float


@dataclass
class OrderProposal:
    symbol: str
    side: str
    quantity: Optional[float] = None
    amount: Optional[float] = None
    strategy_tag: str = ""
    rationale: str = ""


@dataclass
class AgentReview:
    proposal_index: int
    confidence: float
    approved: bool
    rationale_appendix: str = ""


@dataclass
class OrderIntent:
    intent_id: str
    symbol: str
    side: str
    quantity: Optional[float]
    amount: Optional[float]
    order_type: str
    tif: str
    strategy_tag: str
    price_reference: float
    rationale: str


@dataclass
class ValidationResult:
    ok: bool
    error: str = ""


@dataclass
class RiskDecision:
    intent_id: str
    action: str
    reason: str = ""
    scaled_quantity: Optional[float] = None
    scaled_amount: Optional[float] = None


@dataclass
class FillRecord:
    intent_id: str
    symbol: str
    side: str
    quantity: float
    price: float


@dataclass
class CycleReport:
    submitted_count: int
    vetoed_count: int
    pending_approval_count: int
    completed_phases: List[str]
