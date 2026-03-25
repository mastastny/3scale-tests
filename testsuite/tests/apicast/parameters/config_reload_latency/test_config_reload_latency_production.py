"""
Reproduction test for APIcast config-reload request stall – production self-managed APIcast.

Same as the staging variant but uses a production (deploymentEnvironment=production)
self-managed APIcast.  Bulk services are promoted to the production proxy config so
APIcast loads them during each reload cycle.

Key difference from the staging variant:
  - Gateway deployed with staging=False (production environment)
  - All 300+ bulk services are promoted to production before the test runs
  - Traffic is measured against the production endpoint (prod_client)
"""

import time
import logging

import pytest

from testsuite.capabilities import Capability
from testsuite.gateways import gateway as create_gateway
from testsuite.utils import blame

log = logging.getLogger(__name__)

CACHE_RELOAD_WAIT = 40  # seconds – must be > cacheConfigurationSeconds (30)
MAX_ACCEPTABLE_LATENCY = 3.0  # seconds

pytestmark = [
    pytest.mark.disruptive,
    pytest.mark.sandbag,
    pytest.mark.required_capabilities(
        Capability.STANDARD_GATEWAY, Capability.CUSTOM_ENVIRONMENT, Capability.PRODUCTION_GATEWAY
    ),
]


@pytest.fixture(scope="module")
def production_gateway(request, gateway_kind, gateway_options, gateway_environment, testconfig):
    """Production self-managed APIcast in boot mode pointing to the custom tenant."""
    gw = create_gateway(kind=gateway_kind, staging=False, name=blame(request, "gw-prod"), **gateway_options)
    if not testconfig["skip_cleanup"]:
        request.addfinalizer(gw.destroy)
    gw.create()
    if gateway_environment:
        gw.environ.set_many(gateway_environment)
    return gw


@pytest.fixture(scope="module")
def promoted_many_services(many_services):
    """
    Promote all bulk services to production so the production APIcast loads them
    during each reload cycle.
    """
    log.info("Promoting %d bulk services to production …", len(many_services))
    for svc in many_services:
        version = svc.proxy.list().configs.latest()["version"]
        svc.proxy.list().promote(version=version)
    log.info("Done promoting bulk services.")
    return many_services


@pytest.fixture(scope="module")
def prod_client(production_gateway, application, request, testconfig):
    """Production client that promotes the test service and uses the production endpoint."""

    def _prod_client(app=application, promote=True, version=-1, redeploy=True):
        if promote:
            if version == -1:
                version = app.service.proxy.list().configs.latest()["version"]
            app.service.proxy.list().promote(version=version)
        if redeploy:
            production_gateway.reload()
        client = app.api_client(endpoint="endpoint", disable_retry_status_list=[503, 404])
        if not testconfig["skip_cleanup"]:
            request.addfinalizer(client.close)
        return client

    return _prod_client


@pytest.mark.usefixtures("promoted_many_services")
def test_config_reload_latency_production(prod_client):
    """
    Preparation:
        1. Create a dedicated custom tenant.
        2. Deploy a production self-managed APIcast in boot mode with 30-second cache.
        3. Create 300+ products and promote them to production so APIcast loads them.
        4. Create and promote one test product + application for probing.

    Test:
        - Send an initial request via the production APIcast to confirm the setup.
        - Wait for the config cache to expire.
        - Send probing requests continuously and record latencies.
        - Assert that no request stalled longer than MAX_ACCEPTABLE_LATENCY.

    Expected result on FIXED build:  all assertions pass, max latency < 3 s.
    Expected result on BUGGY build:  at least one request stalls for 10-30 s.
    """
    client = prod_client()

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
        f"Max request latency during production config reload was {max_latency:.2f} s "
        f"(threshold: {MAX_ACCEPTABLE_LATENCY} s). "
        "This indicates APIcast is blocking incoming requests while rebuilding "
        "the policy chain for all services."
    )