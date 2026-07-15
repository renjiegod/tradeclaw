import unittest
from unittest.mock import MagicMock

from doyoutrade.config import get_config
from doyoutrade.platform.service import TradingPlatformService
from doyoutrade.runtime.scheduler import RuntimeScheduler


class _NoopWorker:
    def run_cycle(self):
        return None


class PlatformTemplateTests(unittest.TestCase):
    def test_service_exposes_templates(self):
        service = TradingPlatformService(
            scheduler=RuntimeScheduler(),
            app_cfg=get_config(),
            worker_factory=lambda config, ms: _NoopWorker(),
            task_repository=MagicMock(),
            system_state_repository=MagicMock(),
        )

        templates = service.list_templates()

        self.assertGreaterEqual(len(templates), 3)
        names = {item["name"] for item in templates}
        self.assertIn("Single Agent / Trend Following", names)


if __name__ == "__main__":
    unittest.main()
