# tests/test_compiler_multifile.py
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from doyoutrade.strategy_runtime.compiler import StrategyCompiler


class MultiFileCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.compiler = StrategyCompiler()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _write(self, rel: str, body: str) -> None:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip())

    def test_validate_directory_compiles_entry_and_helper(self) -> None:
        self._write("strategy.py", """
            from helpers import sma
            from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

            class Strategy(BaseStrategy):
                timeframe = "1d"
                startup_history = 10
                def on_bar(self, df, ctx):
                    sma(df["close"], 5)
                    return Signal.hold()
        """)
        self._write("helpers.py", """
            def sma(series, n):
                return series.rolling(n).mean()
        """)
        result = self.compiler.validate_directory(self.tmp)
        self.assertTrue(result.ok, msg=result.errors)
        self.assertEqual(result.artifact.class_name, "Strategy")

    def test_validate_directory_leaves_no_bytecode_in_code_root(self) -> None:
        # Importing the strategy here must NOT drop CPython bytecode into the
        # versioned code_root: __pycache__/*.pyc would leak into the source
        # viewer (binary decoded as UTF-8 → 乱码) and perturb the content hash.
        self._write("strategy.py", """
            from helpers import sma
            from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

            class Strategy(BaseStrategy):
                timeframe = "1d"
                startup_history = 10
                def on_bar(self, df, ctx):
                    sma(df["close"], 5)
                    return Signal.hold()
        """)
        self._write("helpers.py", """
            def sma(series, n):
                return series.rolling(n).mean()
        """)
        result = self.compiler.validate_directory(self.tmp)
        self.assertTrue(result.ok, msg=result.errors)
        leaked = [str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if p.suffix in {".pyc", ".pyo"} or p.name == "__pycache__"]
        self.assertEqual(leaked, [], f"bytecode leaked into code_root: {leaked}")

    def test_disallowed_import_in_helper_is_rejected(self) -> None:
        self._write("strategy.py", """
            from helpers import fetch
            from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

            class Strategy(BaseStrategy):
                timeframe = "1d"
                startup_history = 5
                def on_bar(self, df, ctx):
                    fetch()
                    return Signal.hold()
        """)
        self._write("helpers.py", """
            import requests
            def fetch():
                return requests.get("http://x")
        """)
        result = self.compiler.validate_directory(self.tmp)
        self.assertFalse(result.ok)
        codes = {e["error_code"] for e in result.error_dicts}
        self.assertIn("disallowed_import", codes)

    def test_history_literal_in_helper_is_rejected(self) -> None:
        self._write("strategy.py", """
            from helpers import slow_ma
            from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

            class Strategy(BaseStrategy):
                timeframe = "1d"
                startup_history = 20
                def on_bar(self, df, ctx):
                    slow_ma(df["close"])
                    return Signal.hold()
        """)
        self._write("helpers.py", """
            def slow_ma(series):
                return series.rolling(50).mean()
        """)
        result = self.compiler.validate_directory(self.tmp)
        self.assertFalse(result.ok)
        codes = {e["error_code"] for e in result.error_dicts}
        self.assertIn("history_check_literal_disallowed", codes)

    def test_missing_strategy_class_rejected(self) -> None:
        self._write("strategy.py", """
            from doyoutrade.strategy_sdk import Strategy as BaseStrategy

            class NotStrategy(BaseStrategy):
                timeframe = "1d"
                startup_history = 5
                def on_bar(self, df, ctx):
                    from doyoutrade.strategy_sdk import Signal
                    return Signal.hold()
        """)
        result = self.compiler.validate_directory(self.tmp)
        self.assertFalse(result.ok)

    def test_validate_directory_rejects_unsupported_timeframe(self) -> None:
        # ``4h`` / ``1h`` / ``1M`` are NOT served by any data provider
        # (hourly is ``60m``, monthly is ``1mo``). The directory path must
        # reject them at compile time instead of letting the run fail later
        # with ``data_insufficient``.
        for bad in ("4h", "1h", "1M", "banana"):
            with self.subTest(timeframe=bad):
                self._write("strategy.py", f"""
                    from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

                    class Strategy(BaseStrategy):
                        timeframe = "{bad}"
                        startup_history = 10
                        def on_bar(self, df, ctx):
                            return Signal.hold()
                """)
                result = self.compiler.validate_directory(self.tmp)
                self.assertFalse(result.ok, msg=f"{bad!r} should be rejected")
                codes = {e["error_code"] for e in result.error_dicts}
                self.assertIn("invalid_class_attribute", codes)

    def test_validate_directory_accepts_canonical_intraday_timeframes(self) -> None:
        for good in ("1m", "5m", "15m", "30m", "60m", "1w", "1mo"):
            with self.subTest(timeframe=good):
                self._write("strategy.py", f"""
                    from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

                    class Strategy(BaseStrategy):
                        timeframe = "{good}"
                        startup_history = 10
                        def on_bar(self, df, ctx):
                            return Signal.hold()
                """)
                result = self.compiler.validate_directory(self.tmp)
                self.assertTrue(result.ok, msg=result.errors)

    def test_missing_strategy_py_rejected(self) -> None:
        self._write("helpers.py", "X = 1\n")
        result = self.compiler.validate_directory(self.tmp)
        self.assertFalse(result.ok)
        codes = {e["error_code"] for e in result.error_dicts}
        self.assertIn("entry_file_missing", codes)

    def test_invalid_signal_fraction_literal_rejected(self) -> None:
        for bad in ("1.5", "0", "-0.1"):
            with self.subTest(fraction=bad):
                self._write("strategy.py", f"""
                    from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

                    class Strategy(BaseStrategy):
                        timeframe = "1d"
                        startup_history = 5
                        def on_bar(self, df, ctx):
                            if ctx.position.is_long:
                                return Signal.sell(tag="x", fraction={bad})
                            return Signal.hold()
                """)
                result = self.compiler.validate_directory(self.tmp)
                self.assertFalse(result.ok)
                codes = {e["error_code"] for e in result.error_dicts}
                self.assertIn("invalid_signal_fraction", codes)

    def test_valid_signal_fraction_accepted(self) -> None:
        self._write("strategy.py", """
            from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

            class Strategy(BaseStrategy):
                timeframe = "1d"
                startup_history = 5
                def on_bar(self, df, ctx):
                    if ctx.position.is_long:
                        return Signal.sell(tag="x", fraction=0.5)
                    return Signal.hold()
        """)
        result = self.compiler.validate_directory(self.tmp)
        self.assertTrue(result.ok, msg=result.errors)

    def test_invalid_target_exposure_literal_rejected(self) -> None:
        self._write("strategy.py", """
            from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

            class Strategy(BaseStrategy):
                timeframe = "1d"
                startup_history = 5
                def on_bar(self, df, ctx):
                    return Signal.target_exposure(target=1.2, tag="grid")
        """)
        result = self.compiler.validate_directory(self.tmp)
        self.assertFalse(result.ok)
        codes = {e["error_code"] for e in result.error_dicts}
        self.assertIn("invalid_target_exposure", codes)

    def test_target_exposure_requires_tag(self) -> None:
        self._write("strategy.py", """
            from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

            class Strategy(BaseStrategy):
                timeframe = "1d"
                startup_history = 5
                def on_bar(self, df, ctx):
                    return Signal.target_exposure(target=0.5)
        """)
        result = self.compiler.validate_directory(self.tmp)
        self.assertFalse(result.ok)
        codes = {e["error_code"] for e in result.error_dicts}
        self.assertIn("missing_signal_tag", codes)

    def test_invalid_target_quantity_literal_rejected(self) -> None:
        self._write("strategy.py", """
            from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

            class Strategy(BaseStrategy):
                timeframe = "1d"
                startup_history = 5
                def on_bar(self, df, ctx):
                    return Signal.target_quantity(quantity=-100, tag="grid")
        """)
        result = self.compiler.validate_directory(self.tmp)
        self.assertFalse(result.ok)
        codes = {e["error_code"] for e in result.error_dicts}
        self.assertIn("invalid_target_quantity", codes)

    def test_target_quantity_requires_tag(self) -> None:
        self._write("strategy.py", """
            from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

            class Strategy(BaseStrategy):
                timeframe = "1d"
                startup_history = 5
                def on_bar(self, df, ctx):
                    return Signal.target_quantity(quantity=300)
        """)
        result = self.compiler.validate_directory(self.tmp)
        self.assertFalse(result.ok)
        codes = {e["error_code"] for e in result.error_dicts}
        self.assertIn("missing_signal_tag", codes)


if __name__ == "__main__":
    unittest.main()
