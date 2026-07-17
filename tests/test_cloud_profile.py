from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from doyoutrade.data.cloud_profile import (
    CLOUD_HELLO_PATH,
    CLOUD_PROFILE_CACHE_TTL_SECONDS,
    CloudProfile,
    get_cloud_profile,
    get_cloud_profile_sync,
    parse_cloud_profile,
    reset_cloud_profile_cache,
)

HELLO_PAYLOAD = {
    "protocol_version": 1,
    "service": "doyoutrade-cloud",
    "plan": {
        "plan_name": "free",
        "rate_per_minute": 30,
        "daily_requests": 2000,
        "scopes": ["history", "realtime"],
        "max_ws_connections": 1,
    },
    "quota": {"daily_requests": 2000, "used_today": 123, "remaining_today": 1877},
    "capabilities": ["rate_limit_headers", "control_ws"],
    "recommendations": {
        "disable_download": True,
        "sync_lookback_years": 2,
        "provider_rate_limit_per_second": 0.4,
    },
}


class _CountingTransport:
    """MockTransport wrapper that records every request it serves."""

    def __init__(self, responder):
        self.requests: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return responder(request)

        self.transport = httpx.MockTransport(_handler)


def _json_transport(status_code: int = 200, payload=None) -> _CountingTransport:
    body = HELLO_PAYLOAD if payload is None else payload
    return _CountingTransport(
        lambda request: httpx.Response(status_code, json=body)
    )


class ParseCloudProfileTests(unittest.TestCase):
    def test_parses_full_hello_payload(self) -> None:
        profile = parse_cloud_profile(HELLO_PAYLOAD)
        self.assertIsInstance(profile, CloudProfile)
        self.assertEqual(profile.service, "doyoutrade-cloud")
        self.assertEqual(profile.protocol_version, 1)
        self.assertEqual(profile.plan.plan_name, "free")
        self.assertEqual(profile.plan.rate_per_minute, 30)
        self.assertEqual(profile.plan.daily_requests, 2000)
        self.assertEqual(profile.plan.scopes, ("history", "realtime"))
        self.assertEqual(profile.plan.max_ws_connections, 1)
        self.assertEqual(profile.quota.daily_requests, 2000)
        self.assertEqual(profile.quota.used_today, 123)
        self.assertEqual(profile.quota.remaining_today, 1877)
        self.assertEqual(profile.capabilities, ("rate_limit_headers", "control_ws"))
        self.assertTrue(profile.has_capability("control_ws"))
        self.assertFalse(profile.has_capability("nope"))
        self.assertTrue(profile.recommendations.disable_download)
        self.assertEqual(profile.recommendations.sync_lookback_years, 2)
        self.assertEqual(profile.recommendations.provider_rate_limit_per_second, 0.4)

    def test_parses_minimal_payload_with_defaults(self) -> None:
        profile = parse_cloud_profile({"service": "doyoutrade-cloud"})
        self.assertIsNone(profile.protocol_version)
        self.assertEqual(profile.plan.plan_name, "")
        self.assertEqual(profile.capabilities, ())
        self.assertFalse(profile.recommendations.disable_download)
        self.assertIsNone(profile.recommendations.sync_lookback_years)
        self.assertIsNone(profile.recommendations.provider_rate_limit_per_second)

    def test_rejects_service_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "service mismatch"):
            parse_cloud_profile({**HELLO_PAYLOAD, "service": "qmt-proxy"})

    def test_rejects_non_dict_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be an object"):
            parse_cloud_profile(["not", "a", "dict"])

    def test_rejects_bad_recommendation_types(self) -> None:
        bad = {
            **HELLO_PAYLOAD,
            "recommendations": {"disable_download": "yes"},
        }
        with self.assertRaisesRegex(ValueError, "disable_download"):
            parse_cloud_profile(bad)
        bad = {
            **HELLO_PAYLOAD,
            "recommendations": {"sync_lookback_years": "two"},
        }
        with self.assertRaisesRegex(ValueError, "sync_lookback_years"):
            parse_cloud_profile(bad)


class GetCloudProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_cloud_profile_cache()

    def tearDown(self) -> None:
        reset_cloud_profile_cache()

    async def test_cloud_hello_success_returns_profile(self) -> None:
        transport = _json_transport()
        profile = await get_cloud_profile(
            "http://cloud.example:8443/", "dytc_secret", transport=transport.transport
        )
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.plan.plan_name, "free")
        # Probe hit the hello path with Bearer auth; trailing slash normalized.
        request = transport.requests[0]
        self.assertEqual(str(request.url), f"http://cloud.example:8443{CLOUD_HELLO_PATH}")
        self.assertEqual(request.headers["Authorization"], "Bearer dytc_secret")

    async def test_success_is_cached_per_account(self) -> None:
        transport = _json_transport()
        first = await get_cloud_profile(
            "http://cloud.example", "dytc_secret", transport=transport.transport
        )
        second = await get_cloud_profile(
            "http://cloud.example", "dytc_secret", transport=transport.transport
        )
        self.assertIs(first, second)
        self.assertEqual(len(transport.requests), 1)

    async def test_404_means_classic_mode_and_is_cached(self) -> None:
        transport = _json_transport(status_code=404, payload={"detail": "Not Found"})
        first = await get_cloud_profile(
            "http://qmt-proxy.local:8000", "token-1", transport=transport.transport
        )
        second = await get_cloud_profile(
            "http://qmt-proxy.local:8000", "token-1", transport=transport.transport
        )
        self.assertIsNone(first)
        self.assertIsNone(second)
        # Negative verdict cached: only one probe.
        self.assertEqual(len(transport.requests), 1)

    async def test_non_200_means_classic_mode(self) -> None:
        transport = _json_transport(status_code=500, payload={"detail": "boom"})
        profile = await get_cloud_profile(
            "http://cloud.example", "dytc_secret", transport=transport.transport
        )
        self.assertIsNone(profile)

    async def test_network_error_means_classic_mode(self) -> None:
        def _raise(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        transport = _CountingTransport(_raise)
        profile = await get_cloud_profile(
            "http://down.example", "dytc_secret", transport=transport.transport
        )
        self.assertIsNone(profile)
        # Failure is cached too — no re-probe storm.
        again = await get_cloud_profile(
            "http://down.example", "dytc_secret", transport=transport.transport
        )
        self.assertIsNone(again)
        self.assertEqual(len(transport.requests), 1)

    async def test_invalid_200_payload_means_classic_mode(self) -> None:
        transport = _CountingTransport(
            lambda request: httpx.Response(200, content=b"not json")
        )
        profile = await get_cloud_profile(
            "http://weird.example", "dytc_secret", transport=transport.transport
        )
        self.assertIsNone(profile)

    async def test_200_from_non_cloud_service_means_classic_mode(self) -> None:
        transport = _json_transport(payload={"service": "some-other-gateway"})
        profile = await get_cloud_profile(
            "http://other.example", "dytc_secret", transport=transport.transport
        )
        self.assertIsNone(profile)

    async def test_missing_base_url_or_token_skips_probe(self) -> None:
        transport = _json_transport()
        self.assertIsNone(
            await get_cloud_profile("", "dytc_secret", transport=transport.transport)
        )
        self.assertIsNone(
            await get_cloud_profile("http://cloud.example", None, transport=transport.transport)
        )
        self.assertIsNone(
            await get_cloud_profile("http://cloud.example", "  ", transport=transport.transport)
        )
        self.assertEqual(len(transport.requests), 0)

    async def test_cache_expires_after_ttl(self) -> None:
        transport = _json_transport()
        clock = {"now": 1000.0}
        with patch(
            "doyoutrade.data.cloud_profile._now", side_effect=lambda: clock["now"]
        ):
            await get_cloud_profile(
                "http://cloud.example", "dytc_secret", transport=transport.transport
            )
            clock["now"] += CLOUD_PROFILE_CACHE_TTL_SECONDS + 1
            await get_cloud_profile(
                "http://cloud.example", "dytc_secret", transport=transport.transport
            )
        self.assertEqual(len(transport.requests), 2)

    async def test_distinct_tokens_probe_separately(self) -> None:
        transport = _json_transport()
        await get_cloud_profile(
            "http://cloud.example", "dytc_a", transport=transport.transport
        )
        await get_cloud_profile(
            "http://cloud.example", "dytc_b", transport=transport.transport
        )
        self.assertEqual(len(transport.requests), 2)


class GetCloudProfileSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_cloud_profile_cache()

    def tearDown(self) -> None:
        reset_cloud_profile_cache()

    def test_sync_probe_success_and_shared_cache(self) -> None:
        transport = _json_transport()
        profile = get_cloud_profile_sync(
            "http://cloud.example", "dytc_secret", transport=transport.transport
        )
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.quota.remaining_today, 1877)
        again = get_cloud_profile_sync(
            "http://cloud.example", "dytc_secret", transport=transport.transport
        )
        self.assertIs(profile, again)
        self.assertEqual(len(transport.requests), 1)

    def test_sync_probe_404_means_classic_mode(self) -> None:
        transport = _json_transport(status_code=404, payload={"detail": "Not Found"})
        self.assertIsNone(
            get_cloud_profile_sync(
                "http://qmt-proxy.local", "token-1", transport=transport.transport
            )
        )


if __name__ == "__main__":
    unittest.main()
