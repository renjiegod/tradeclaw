from __future__ import annotations

from tradeclaw.domain.models import FillRecord


class PaperExecutionAdapter:
    def __init__(self):
        self.submitted = []
        self.fills = []

    async def submit_intent(self, intent):
        self.submitted.append(intent)
        fill = FillRecord(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=float(intent.quantity or 0.0),
            price=float(intent.price_reference),
        )
        self.fills.append(fill)
        return fill

    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": "cancelled"}

    def query_order_status(self, order_id):
        return {"order_id": order_id, "status": "filled"}

    def sync_account_state(self):
        return {"status": "ok"}


class SimulatedBrokerAdapter(PaperExecutionAdapter):
    pass
