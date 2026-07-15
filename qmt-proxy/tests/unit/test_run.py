import unittest
from types import SimpleNamespace

import run


def make_settings(*, debug: bool):
    return SimpleNamespace(app=SimpleNamespace(debug=debug))


class RunConfigTests(unittest.TestCase):
    def test_get_reload_config_disables_reload_when_debug_enabled(self):
        settings = make_settings(debug=True)

        self.assertEqual(run.get_reload_config(settings), (False, None))

    def test_get_reload_config_keeps_reload_disabled_when_debug_off(self):
        settings = make_settings(debug=False)

        self.assertEqual(run.get_reload_config(settings), (False, None))


if __name__ == "__main__":
    unittest.main()
