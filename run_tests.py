#!/usr/bin/env python3
"""Custom test runner that works around unittest discover hanging issue."""
import sys
import os

# Ensure src is on path like tests/__init__.py does
ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import unittest
import time


def run_all_tests():
    loader = unittest.TestLoader()
    suite = loader.discover('tests')

    print(f"Discovered {suite.countTestCases()} tests", flush=True)

    start = time.time()
    result = unittest.TestResult()
    suite.run(result)
    elapsed = time.time() - start

    print(f"\nRan {result.testsRun} tests in {elapsed:.1f}s", flush=True)
    print(f"Failures: {len(result.failures)}", flush=True)
    print(f"Errors: {len(result.errors)}", flush=True)
    print(f"Skipped: {len(result.skipped)}", flush=True)

    for test, trace in result.failures:
        print(f"\nFAILURE: {test}", flush=True)
        print(trace, flush=True)

    for test, trace in result.errors:
        print(f"\nERROR: {test}", flush=True)
        print(trace, flush=True)

    print("About to exit...", flush=True, file=sys.stderr)
    import os
    os._exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    run_all_tests()
