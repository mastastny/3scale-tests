"""
Conftest for the internal (system) APIcast variant of the config-reload latency test.

Differences from the standalone variants:
  - Uses the MAIN tenant (overrides the custom-tenant threescale from the parent conftest).
  - Uses the internal system staging APIcast (apicast-staging deployment) instead of
    deploying a new self-managed APIcast.
  - Temporarily sets boot mode + short cache on the system APIcast and reverts on teardown.
  - Services are created as HOSTED (deployment_option not overridden) so the system
    APIcast serves them – lifecycle_hooks is therefore empty.

WARNING: This is disruptive.  Modifying the system apicast-staging affects all tests
running in the same 3scale namespace concurrently.  The 3scale-operator may reconcile
and revert env-var changes; if that happens the test may not reproduce the issue
reliably.  Run this in isolation.
"""
import logging

import pytest
from threescale_api import client as ts_client

from testsuite.gateways.apicast.system import SystemApicast

log = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def threescale(testconfig):
    """
    Override the parent conftest's custom-tenant threescale.
    Returns a client for the MAIN tenant so services are visible to the
    system APIcast.
    """
    return ts_client.ThreeScaleClient(
        testconfig["threescale"]["admin"]["url"],
        testconfig["threescale"]["admin"]["token"],
        ssl_verify=testconfig["ssl_verify"],
        wait=0,
    )


@pytest.fixture(scope="module")
def lifecycle_hooks():
    """
    Empty hooks so services are created with deployment_option=hosted (the 3scale
    default) instead of self_managed.  The system APIcast serves hosted services.
    """
    return []


@pytest.fixture(scope="module")
def staging_gateway(openshift, testconfig, request):
    """
    Use the internal system staging APIcast, temporarily setting boot mode and
    a short cache interval.  Env vars are reverted on teardown.
    """
    gw = SystemApicast(staging=True, openshift=openshift())

    env_overrides = {
        "APICAST_CONFIGURATION_LOADER": "boot",
        "APICAST_CONFIGURATION_CACHE": "30",
    }
    log.info("Setting system apicast-staging to boot mode with 30 s cache …")
    gw.environ.set_many(env_overrides)
    gw.reload()

    def _revert():
        log.info("Reverting system apicast-staging env vars …")
        try:
            del gw.environ["APICAST_CONFIGURATION_CACHE"]
        except Exception:  # pylint: disable=broad-except
            pass
        try:
            del gw.environ["APICAST_CONFIGURATION_LOADER"]
        except Exception:  # pylint: disable=broad-except
            pass
        gw.reload()

    if not testconfig["skip_cleanup"]:
        request.addfinalizer(_revert)

    return gw