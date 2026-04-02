import unittest

from tradeclaw.platform.service import TradingPlatformService
from tradeclaw.runtime.scheduler import RuntimeScheduler


class _NoopWorker:
    def run_cycle(self):
        return None


class PlatformTemplateTests(unittest.TestCase):
    def test_service_exposes_templates(self):
        service = TradingPlatformService(
            scheduler=RuntimeScheduler(),
            worker_factory=lambda config: _NoopWorker(),
        )

        templates = service.list_templates()

        self.assertGreaterEqual(len(templates), 3)
        ids = {item["template_id"] for item in templates}
        self.assertIn("single-agent-trend", ids)


if __name__ == "__main__":
    unittest.main()
