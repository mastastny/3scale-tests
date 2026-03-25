"""
Reproduction test for APIcast config-reload request stall – internal system APIcast.

This variant uses the 3scale-embedded system APIcast (apicast-staging deployment)
instead of deploying a standalone self-managed APIcast.  This is the closest
reproduction of the customer's environment where the embedded APIcast is used.

Services are created as HOSTED (not self_managed) so the system APIcast serves them.
The system APIcast is temporarily set to boot mode with a 30-second config cache.

WARNING: Disruptive – modifies the shared system apicast-staging deployment.
         Run in isolation.  See conftest.py for details.
"""

import time
import logging

import pytest

from testsuite.capabilities import Capability

log = logging.getLogger(__name__)

CACHE_RELOAD_WAIT = 40  # seconds – must be > cacheConfigurationSeconds (30)
MAX_ACCEPTABLE_LATENCY = 3.0  # seconds

pytestmark = [
    pytest.mark.disruptive,
    pytest.mark.sandbag,
    pytest.mark.required_capabilities(Capability.STANDARD_GATEWAY, Capability.CUSTOM_ENVIRONMENT),
    pytest.mark.usefixtures("staging_gateway"),
]


@pytest.mark.usefixtures("many_services")
def test_config_reload_latency_system(api_client):
    """
    Preparation:
        1. Temporarily set the internal system apicast-staging to boot mode
           with a 30-second config cache.
        2. Create 300+ hosted products in the main tenant so the system APIcast
           has a large configuration to reload.
        3. Create one test product + application used to probe request latency.

    Test:
        - Send an initial request to confirm the setup is working.
        - Wait for the config cache to expire (CACHE_RELOAD_WAIT seconds).
        - Send probing requests continuously and record their individual latencies.
        - Assert that no single request took longer than MAX_ACCEPTABLE_LATENCY.

    Expected result on FIXED build:  all assertions pass, max latency < 3 s.
    Expected result on BUGGY build:  at least one request stalls for 10-30 s.

    Teardown: system apicast-staging env vars are reverted to their original values.
    """
    client = api_client(disable_retry_status_list=[503, 404])

    response = client.get("/get")
    assert response.status_code == 200, "Pre-wait sanity check failed"

    log.info("Waiting %d s for the config cache to expire and trigger a reload …", CACHE_RELOAD_WAIT)
    time.sleep(CACHE_RELOAD_WAIT)

    latencies = []
    errors = []
    window_end = time.monotonic() + CACHE_RELOAD_WAIT * 2

    while time.monotonic() < window_end:
        t0 = time.perf_counter()
        response = client.get("/get")
        elapsed = time.perf_counter() - t0

        latencies.append(elapsed)
        if response.status_code != 200:
            errors.append((elapsed, response.status_code))

        log.info(
            "[%.1fs into window] latency: %.3f s  status: %s",
            CACHE_RELOAD_WAIT * 2 - (window_end - time.monotonic()),
            elapsed,
            response.status_code,
        )

    max_latency = max(latencies)
    log.info(
        "Latency summary — min: %.3f s  max: %.3f s  avg: %.3f s  requests: %d  errors: %d  (threshold: %.1f s)",
        min(latencies),
        max_latency,
        sum(latencies) / len(latencies),
        len(latencies),
        len(errors),
        MAX_ACCEPTABLE_LATENCY,
    )

    assert not errors, (
        f"Got {len(errors)} non-200 response(s) during config reload window: "
        + ", ".join(f"{status} in {lat:.2f}s" for lat, status in errors)
    )
    assert max_latency < MAX_ACCEPTABLE_LATENCY, (
        f"Max request latency during system APIcast config reload was {max_latency:.2f} s "
        f"(threshold: {MAX_ACCEPTABLE_LATENCY} s). "
        "This indicates APIcast is blocking incoming requests while rebuilding "
        "the policy chain for all services."
    )