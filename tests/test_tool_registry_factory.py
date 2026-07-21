from __future__ import annotations

import os
import unittest

from doyoutrade.tools import (
    OperationRegistry,
    build_default_tool_registry,
    resolve_tool_registry_factory,
)

_ENV_KEY = "DOYOUTRADE_TOOL_REGISTRY_FACTORY"

# Module-level sentinel targets the resolver can import via
# "tests.test_tool_registry_factory:<name>".
FACTORY_CALLS: list[dict] = []


def fake_factory(**kwargs) -> OperationRegistry:
    """Stand-in deployment factory: records the call, returns an empty registry."""
    FACTORY_CALLS.append(kwargs)
    return OperationRegistry([])


NOT_CALLABLE = "definitely not a callable"


class ResolveToolRegistryFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get(_ENV_KEY)
        FACTORY_CALLS.clear()

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(_ENV_KEY, None)
        else:
            os.environ[_ENV_KEY] = self._saved

    def test_unset_env_returns_builtin_default(self) -> None:
        os.environ.pop(_ENV_KEY, None)
        self.assertIs(resolve_tool_registry_factory(), build_default_tool_registry)

    def test_blank_env_returns_builtin_default(self) -> None:
        os.environ[_ENV_KEY] = "   "
        self.assertIs(resolve_tool_registry_factory(), build_default_tool_registry)

    def test_valid_spec_resolves_named_callable(self) -> None:
        os.environ[_ENV_KEY] = "tests.test_tool_registry_factory:fake_factory"
        factory = resolve_tool_registry_factory()
        self.assertIs(factory, fake_factory)
        registry = factory(tool_result_max_chars=123)
        self.assertIsInstance(registry, OperationRegistry)
        self.assertEqual(FACTORY_CALLS, [{"tool_result_max_chars": 123}])

    def test_spec_without_colon_raises_value_error(self) -> None:
        os.environ[_ENV_KEY] = "tests.test_tool_registry_factory.fake_factory"
        with self.assertRaises(ValueError) as ctx:
            resolve_tool_registry_factory()
        self.assertIn("package.module:callable", str(ctx.exception))

    def test_spec_with_empty_attr_raises_value_error(self) -> None:
        os.environ[_ENV_KEY] = "tests.test_tool_registry_factory:"
        with self.assertRaises(ValueError):
            resolve_tool_registry_factory()

    def test_missing_module_raises_import_error(self) -> None:
        os.environ[_ENV_KEY] = "tests.no_such_module_anywhere:factory"
        with self.assertRaises(ImportError) as ctx:
            resolve_tool_registry_factory()
        self.assertIn("no_such_module_anywhere", str(ctx.exception))

    def test_missing_attribute_raises_attribute_error(self) -> None:
        os.environ[_ENV_KEY] = "tests.test_tool_registry_factory:no_such_attr"
        with self.assertRaises(AttributeError) as ctx:
            resolve_tool_registry_factory()
        self.assertIn("no_such_attr", str(ctx.exception))

    def test_non_callable_target_raises_type_error(self) -> None:
        os.environ[_ENV_KEY] = "tests.test_tool_registry_factory:NOT_CALLABLE"
        with self.assertRaises(TypeError) as ctx:
            resolve_tool_registry_factory()
        self.assertIn("non-callable", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
