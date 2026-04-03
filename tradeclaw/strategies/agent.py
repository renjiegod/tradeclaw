from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List

from tradeclaw.domain.models import AgentReview, OrderProposal
from tradeclaw.models.base import ModelAdapter, ModelRequest


SYSTEM_PROMPT = """
You are a trading review agent.
Return ONLY valid JSON with this schema:
{
  "reviews": [
    {
      "proposal_index": 0,
      "approved": true,
      "confidence": 0.0,
      "rationale_appendix": "short reason"
    }
  ]
}
Rules:
- confidence must be between 0 and 1.
- include one review for each proposal index.
- if uncertain, set approved=false with low confidence.
""".strip()


@dataclass
class LangChainAgentStrategy:
    adapter: ModelAdapter

    def review(self, proposals, market_context, account_snapshot, positions) -> List[AgentReview]:
        if not proposals:
            return []

        request = ModelRequest(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=self._build_user_prompt(proposals, market_context, account_snapshot, positions),
        )

        try:
            response = self.adapter.generate(request)
            return self._parse_reviews(response.text, proposals)
        except Exception as exc:  # pragma: no cover - defensive fallback
            return _reject_all(proposals, f"model_error: {exc}")

    def _build_user_prompt(self, proposals, market_context, account_snapshot, positions) -> str:
        payload = {
            "market": market_context.symbol_to_price,
            "account": {
                "cash": account_snapshot.cash,
                "equity": account_snapshot.equity,
            },
            "positions": [
                {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "cost_price": position.cost_price,
                }
                for position in positions
            ],
            "proposals": [
                {
                    "proposal_index": index,
                    "symbol": proposal.symbol,
                    "side": proposal.side,
                    "quantity": proposal.quantity,
                    "amount": proposal.amount,
                    "strategy_tag": proposal.strategy_tag,
                    "rationale": proposal.rationale,
                }
                for index, proposal in enumerate(proposals)
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

    def _parse_reviews(self, text: str, proposals: List[OrderProposal]) -> List[AgentReview]:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return _reject_all(proposals, "invalid_json")

        raw_reviews = parsed.get("reviews")
        if not isinstance(raw_reviews, list):
            return _reject_all(proposals, "invalid_reviews")

        expected_indexes = set(range(len(proposals)))
        review_by_index: dict[int, AgentReview] = {}

        for item in raw_reviews:
            if not isinstance(item, dict):
                continue
            index = item.get("proposal_index")
            if not isinstance(index, int):
                continue
            if index not in expected_indexes:
                continue

            approved = bool(item.get("approved", False))
            confidence = _normalize_confidence(item.get("confidence"))
            appendix = str(item.get("rationale_appendix", ""))

            review_by_index[index] = AgentReview(
                proposal_index=index,
                confidence=confidence,
                approved=approved,
                rationale_appendix=appendix,
            )

        if set(review_by_index) != expected_indexes:
            return _reject_all(proposals, "missing_reviews")

        return [review_by_index[index] for index in range(len(proposals))]


def _normalize_confidence(value) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed < 0.0:
        return 0.0
    if parsed > 1.0:
        return 1.0
    return parsed


def _reject_all(proposals: List[OrderProposal], reason: str) -> List[AgentReview]:
    return [
        AgentReview(
            proposal_index=index,
            confidence=0.0,
            approved=False,
            rationale_appendix=reason,
        )
        for index, _ in enumerate(proposals)
    ]
