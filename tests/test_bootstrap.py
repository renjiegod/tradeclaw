import unittest

from tradeclaw.bootstrap import build_platform_runtime


class BootstrapTests(unittest.TestCase):
    def test_build_runtime_and_run_single_tick(self):
        runtime = build_platform_runtime()
        service = runtime["service"]

        instance = service.create_instance(name="demo", template_id="single-agent-trend")
        service.start_instance(instance.instance_id)
        executed = service.tick_once()

        self.assertEqual(executed, 1)
        status = service.get_instance_status(instance.instance_id)
        self.assertEqual(status["status"], "running")


if __name__ == "__main__":
    unittest.main()
