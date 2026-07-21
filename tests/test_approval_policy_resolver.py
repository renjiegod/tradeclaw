from __future__ import annotations

import os
import unittest

from doyoutrade.assistant.approvals import (
    DEFAULT_APPROVAL_RULES,
    ApprovalRule,
    resolve_approval_policy,
)

_ENV_KEY = "DOYOUTRADE_APPROVAL_POLICY"

# Module-level policy targets the resolver imports by dotted path.
def empty_policy():
    return ()


def one_rule_policy():
    return (
        ApprovalRule(
            key="demo",
            tool="execute_bash",
            command_pattern=r"\bdemo\b",
            description="demo",
        ),
    )


NOT_CALLABLE = 123


class ResolveApprovalPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get(_ENV_KEY)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(_ENV_KEY, None)
        else:
            os.environ[_ENV_KEY] = self._saved

    def test_unset_returns_default_rules(self) -> None:
        os.environ.pop(_ENV_KEY, None)
        self.assertEqual(resolve_approval_policy(), DEFAULT_APPROVAL_RULES)

    def test_blank_returns_default_rules(self) -> None:
        os.environ[_ENV_KEY] = "  "
        self.assertEqual(resolve_approval_policy(), DEFAULT_APPROVAL_RULES)

    def test_empty_policy_yields_no_rules(self) -> None:
        os.environ[_ENV_KEY] = "tests.test_approval_policy_resolver:empty_policy"
        self.assertEqual(resolve_approval_policy(), ())

    def test_custom_policy_returns_tuple(self) -> None:
        os.environ[_ENV_KEY] = "tests.test_approval_policy_resolver:one_rule_policy"
        rules = resolve_approval_policy()
        self.assertIsInstance(rules, tuple)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].key, "demo")

    def test_bad_spec_raises(self) -> None:
        os.environ[_ENV_KEY] = "no_colon_here"
        with self.assertRaises(ValueError):
            resolve_approval_policy()

    def test_missing_module_raises(self) -> None:
        os.environ[_ENV_KEY] = "tests.nope_module:empty_policy"
        with self.assertRaises(ImportError):
            resolve_approval_policy()

    def test_missing_attr_raises(self) -> None:
        os.environ[_ENV_KEY] = "tests.test_approval_policy_resolver:nope_attr"
        with self.assertRaises(AttributeError):
            resolve_approval_policy()

    def test_non_callable_raises(self) -> None:
        os.environ[_ENV_KEY] = "tests.test_approval_policy_resolver:NOT_CALLABLE"
        with self.assertRaises(TypeError):
            resolve_approval_policy()


if __name__ == "__main__":
    unittest.main()
