import json
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from doyoutrade.cli._envelope import EXIT_OK, EXIT_VALIDATION
from doyoutrade.cli.commands.account import account as account_group


class AccountStatementCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.calls: list[tuple[str, str, dict | None]] = []
        self._patch = patch(
            "doyoutrade.cli.commands.account.invoke_api",
            new=self._fake_invoke_api,
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    async def _fake_invoke_api(self, method: str, path: str, *, params=None, meta=None, **kwargs):
        self.calls.append((method, path, params))
        return (
            {
                "ok": True,
                "data": {
                    "account_id": params.get("account_id") if params else None,
                    "asof": params.get("asof") if params else None,
                    "errors": [],
                },
            },
            EXIT_OK,
        )

    def _invoke(self, args: list[str]):
        return self.runner.invoke(
            account_group,
            args,
            obj={"fmt": "json"},
            catch_exceptions=False,
        )

    def test_statement_forwards_query_params(self) -> None:
        result = self._invoke(
            ["statement", "--account", "acct-123", "--asof", "2026-06-18"]
        )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertTrue(envelope["ok"])
        self.assertEqual(
            self.calls,
            [("GET", "/accounts/statement", {"account_id": "acct-123", "asof": "2026-06-18"})],
        )

    def test_statement_rejects_bad_asof(self) -> None:
        result = self._invoke(["statement", "--asof", "20260618"])

        self.assertEqual(result.exit_code, EXIT_VALIDATION, msg=result.output)
        envelope = json.loads(result.output)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["error_code"], "validation_error")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
