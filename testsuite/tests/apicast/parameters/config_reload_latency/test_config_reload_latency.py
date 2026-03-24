"""
Reproduction test for APIcast config-reload request stall.

Root cause: when APIcast reloads a large configuration (boot mode,
every cacheConfigurationSeconds), it rebuilds the policy chain for every
service sequentially.  During that rebuild incoming requests are stalled.
With 200+ products customers reported delays of 10-30 s.

How to run (old version – should FAIL the latency assertion):
    make testsuite/tests/apicast/parameters/config_reload_latency/ \\
        NAMESPACE=<ns> flags="--disruptive -s"

How to run (fixed version – should PASS):
    same command against a fixed build

Debug / manual verification:
    Add skip_cleanup: true to config/settings.local.yaml, run in debug mode
    (-s), and pause after setup to inspect the environment manually.
    The custom tenant and all 200 products will survive the test run and can
    be deleted by removing the tenant from the master admin portal.
"""

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from testsuite import rawobj
from testsuite.capabilities import Capability
from testsuite.utils import randomize

log = logging.getLogger(__name__)

# Built-in policies added to every bulk service to increase policy-chain
# rebuild cost during config reload.  No custom policies needed – standard
# ones are sufficient to lengthen the chain.
BULK_POLICIES = [
    ("url_rewriting", {"commands": []}),
    ("headers", {"request": [], "response": []}),
    ("caching", {"caching_type": "none"}),
    ("logging", {"condition": {"combine_op": "and", "operations": []}, "enable_access_logs": True}),
    ("upstream_connection", {"connect_timeout": 60, "send_timeout": 60, "read_timeout": 60}),
]

# How long to wait for the config cache to expire before measuring latency.
# Must be slightly above cacheConfigurationSeconds (60) defined in conftest.
CACHE_RELOAD_WAIT = 40  # seconds

# Number of extra products to create – must be large enough to make the
# policy-chain rebuild take several seconds.
NUM_EXTRA_PRODUCTS = 300

# Maximum acceptable latency for a single request during the reload window.
# Buggy versions stall for 10-30 s; fixed versions should stay well under 3 s.
MAX_ACCEPTABLE_LATENCY = 3.0  # seconds

pytestmark = [
    pytest.mark.disruptive,
    pytest.mark.sandbag,
    pytest.mark.required_capabilities(Capability.STANDARD_GATEWAY, Capability.CUSTOM_ENVIRONMENT),
    pytest.mark.usefixtures("staging_gateway"),
]


@pytest.fixture(scope="module")
def many_services(request, custom_service, service_proxy_settings, lifecycle_hooks, custom_backend, testconfig):
    """
    Create NUM_EXTRA_PRODUCTS additional products in the custom tenant.

    These products do not need applications; their sole purpose is to inflate
    the configuration that APIcast must reload, reproducing the customer's
    200+ product environment.

    Cleanup: each service is registered as an orphan finalizer so it is
    deleted on teardown unless skip_cleanup is set.  Deleting the custom
    tenant (see conftest) cascades and removes everything anyway.
    """
    backend = custom_backend("be-bulk")
    backend_mapping = {"/": backend}

    def _create_one():
        params = {"name": randomize("svc-bulk")}
        svc = custom_service(params, service_proxy_settings, backend_mapping, autoclean=False, hooks=lifecycle_hooks)
        svc.proxy.list().policies.append(*[rawobj.PolicyConfig(name, cfg) for name, cfg in BULK_POLICIES])
        svc.proxy.deploy()

    log.info("Creating %d extra products …", NUM_EXTRA_PRODUCTS)
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(_create_one) for _ in range(NUM_EXTRA_PRODUCTS)]
        for fut in as_completed(futures):
            fut.result()  # re-raise any exception
    log.info("Done creating extra products.")

    if not testconfig["skip_cleanup"]:
        request.addfinalizer(lambda: [f() for f in custom_service.orphan_finalizers])


@pytest.mark.usefixtures("many_services")
def test_config_reload_latency(api_client):
    """
    Preparation:
        1. Create a dedicated custom tenant.
        2. Deploy a self-managed APIcast in boot mode with a 60-second config cache.
        3. Create 200+ products in that tenant so APIcast has a large config to reload.
        4. Create one test product + application used to probe request latency.

    Test:
        - Send an initial request to confirm the setup is working.
        - Wait for the config cache to expire (CACHE_RELOAD_WAIT seconds).
        - Send several probing requests and record their individual latencies.
        - Assert that no single request took longer than MAX_ACCEPTABLE_LATENCY.

    Expected result on FIXED build:  all assertions pass, max latency < 3 s.
    Expected result on BUGGY build:  at least one request stalls for 10-30 s,
                                     causing the latency assertion to fail.
    """
    client = api_client()

    # Verify the test product is reachable before we wait.
    response = client.get("/get")
    assert response.status_code == 200, "Pre-wait sanity check failed"

    log.info(
        "Waiting %d s for the config cache to expire and trigger a reload …",
        CACHE_RELOAD_WAIT,
    )
    time.sleep(CACHE_RELOAD_WAIT)

    # Send probing requests during / immediately after the reload window.
    # We sample over a 30-second window to increase the chance of catching
    # the stall even if the exact reload moment is hard to predict.
    max_latency = 0.0
    latencies = []
    window_end = time.monotonic() + CACHE_RELOAD_WAIT * 2

    while time.monotonic() < window_end:
        t0 = time.perf_counter()
        response = client.get("/get")
        elapsed = time.perf_counter() - t0

        latencies.append(elapsed)
        max_latency = max(max_latency, elapsed)

        log.info(
            "[%.1fs into window] latency: %.3f s  status: %s",
            30 - (window_end - time.monotonic()),
            elapsed,
            response.status_code,
        )
        assert response.status_code == 200, f"Request returned {response.status_code} during reload window"

    log.info(
        "Latency summary — min: %.3f s  max: %.3f s  avg: %.3f s  (threshold: %.1f s)",
        min(latencies),
        max_latency,
        sum(latencies) / len(latencies),
        MAX_ACCEPTABLE_LATENCY,
    )

    assert max_latency < MAX_ACCEPTABLE_LATENCY, (
        f"Max request latency during config reload was {max_latency:.2f} s "
        f"(threshold: {MAX_ACCEPTABLE_LATENCY} s). "
        "This indicates APIcast is blocking incoming requests while rebuilding "
        "the policy chain for all services."
    )