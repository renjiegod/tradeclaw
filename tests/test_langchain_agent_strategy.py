import unittest

from tradeclaw.domain.models import AccountSnapshot, MarketContext, OrderProposal, PositionSnapshot
from tradeclaw.models.base import ModelRequest, ModelResponse
from tradeclaw.strategies.agent import LangChainAgentStrategy


class _OkAdapter:
    def __init__(self, text: str):
        self.text = text
        self.calls = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(text=self.text)


class _ErrorAdapter:
    def generate(self, request: ModelRequest) -> ModelResponse:
        raise RuntimeError("boom")


class LangChainAgentStrategyTests(unittest.TestCase):
    def _proposals(self):
        return [
            OrderProposal(symbol="600000.SH", side="buy", quantity=100, strategy_tag="s1", rationale="r1"),
            OrderProposal(symbol="601318.SH", side="sell", quantity=100, strategy_tag="s2", rationale="r2"),
        ]

    def _market(self):
        return MarketContext(symbol_to_price={"600000.SH": 10.0, "601318.SH": 50.0})

    def _account(self):
        return AccountSnapshot(cash=100000.0, equity=120000.0)

    def _positions(self):
        return [PositionSnapshot(symbol="600000.SH", quantity=100, cost_price=9.5)]

    def test_parses_valid_model_json(self):
        adapter = _OkAdapter(
            """
            {
              "reviews": [
                {"proposal_index": 0, "approved": true, "confidence": 0.82, "rationale_appendix": "ok"},
                {"proposal_index": 1, "approved": false, "confidence": 0.12, "rationale_appendix": "reject"}
              ]
            }
            """
        )
        strategy = LangChainAgentStrategy(adapter=adapter)

        reviews = strategy.review(self._proposals(), self._market(), self._account(), self._positions())

        self.assertEqual(len(reviews), 2)
        self.assertTrue(reviews[0].approved)
        self.assertFalse(reviews[1].approved)
        self.assertEqual(reviews[0].proposal_index, 0)
        self.assertEqual(reviews[1].proposal_index, 1)

    def test_invalid_json_rejects_all(self):
        adapter = _OkAdapter("not-json")
        strategy = LangChainAgentStrategy(adapter=adapter)

        reviews = strategy.review(self._proposals(), self._market(), self._account(), self._positions())

        self.assertEqual(len(reviews), 2)
        self.assertTrue(all(not item.approved for item in reviews))
        self.assertTrue(all(item.confidence == 0.0 for item in reviews))

    def test_model_error_rejects_all(self):
        strategy = LangChainAgentStrategy(adapter=_ErrorAdapter())

        reviews = strategy.review(self._proposals(), self._market(), self._account(), self._positions())

        self.assertEqual(len(reviews), 2)
        self.assertTrue(all(not item.approved for item in reviews))
        self.assertTrue(all(item.confidence == 0.0 for item in reviews))


if __name__ == "__main__":
    unittest.main()
