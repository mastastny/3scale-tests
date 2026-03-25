"""
Conftest for APIcast config reload latency reproduction tests.

Creates a dedicated custom tenant so all created objects (300+ products) can
be wiped easily by deleting the tenant.  The self-managed gateway is pointed
at that tenant and is configured with:
  - configurationLoadMode=boot   (reload config on startup + every N seconds)
  - cacheConfigurationSeconds=30 (short interval to make reload easy to trigger)

Sub-variants (see sibling test files and system/ subdirectory):
  - Staging self-managed APIcast  (test_config_reload_latency.py)
  - Production self-managed APIcast  (test_config_reload_latency_production.py)
  - Internal system APIcast  (system/test_config_reload_latency_system.py)
"""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import pytest

from testsuite import rawobj, resilient
from testsuite.utils import blame, randomize

import logging

log = logging.getLogger(__name__)

# Number of extra products to create – must be large enough to make the
# policy-chain rebuild take several seconds.
NUM_EXTRA_PRODUCTS = 300

# Built-in policies added to every bulk service to increase policy-chain
# rebuild cost during config reload.
BULK_POLICIES = [
    ("url_rewriting", {"commands": []}),
    ("headers", {"request": [], "response": []}),
    ("caching", {"caching_type": "none"}),
    ("logging", {"condition": {"combine_op": "and", "operations": []}, "enable_access_logs": True}),
    ("upstream_connection", {"connect_timeout": 60, "send_timeout": 60, "read_timeout": 60}),
]


@pytest.fixture(scope="module")
def tenant(custom_tenant):
    """Dedicated custom tenant – deleting it removes every object created here."""
    return custom_tenant()


@pytest.fixture(scope="module")
def threescale(tenant, testconfig):
    """ThreeScale admin client scoped to the custom tenant."""
    return tenant.admin_api(ssl_verify=testconfig["ssl_verify"], wait=0)


@pytest.fixture(scope="module")
def custom_account(threescale, request, testconfig):
    """Module-scoped custom_account that creates accounts in the custom tenant."""

    def _custom_account(params, autoclean=True, threescale_client=threescale):
        acc = resilient.accounts_create(threescale_client, params=params)
        if autoclean and not testconfig["skip_cleanup"]:
            request.addfinalizer(acc.delete)
        return acc

    return _custom_account


@pytest.fixture(scope="module")
def account(custom_account, request, account_password):
    """Account within the custom tenant used for test applications."""
    iname = blame(request, "id")
    params = rawobj.Account(org_name=iname, monthly_billing_enabled=None, monthly_charging_enabled=None)
    params.update(
        {
            "name": iname,
            "username": iname,
            "email": f"{iname}@example.com",
            "password": account_password,
        }
    )
    return custom_account(params=params)


@pytest.fixture(scope="module")
def gateway_environment():
    """
    Boot mode: APIcast loads configuration at startup and reloads every
    cacheConfigurationSeconds.  30 s is short enough to trigger a reload
    quickly during a test run without waiting the default 5 minutes.
    """
    return {
        "APICAST_CONFIGURATION_LOADER": "boot",
        "APICAST_CONFIGURATION_CACHE": 30,
    }


@pytest.fixture(scope="module")
def gateway_options(tenant):
    """
    Point the self-managed APIcast at the custom tenant's admin portal so it
    loads configuration only for the products created in that tenant.
    """
    parsed = urlparse(tenant.admin_base_url)
    portal_endpoint = f"https://{tenant.admin_token}@{parsed.netloc}"
    return {"portal_endpoint": portal_endpoint}


@pytest.fixture(scope="module")
def many_services(request, custom_service, service_proxy_settings, lifecycle_hooks, custom_backend, testconfig):
    """
    Create NUM_EXTRA_PRODUCTS additional products in the custom tenant.

    These products do not need applications; their sole purpose is to inflate
    the configuration that APIcast must reload, reproducing the customer's
    200+ product environment.

    Returns the list of created Service objects so callers can promote them
    to production if needed.

    Cleanup: each service is registered as an orphan finalizer so it is
    deleted on teardown unless skip_cleanup is set.  Deleting the custom
    tenant (see above) cascades and removes everything anyway.
    """
    backend = custom_backend("be-bulk")
    backend_mapping = {"/": backend}
    services = []
    lock = threading.Lock()

    def _create_one():
        params = {"name": randomize("svc-bulk")}
        svc = custom_service(params, service_proxy_settings, backend_mapping, autoclean=False, hooks=lifecycle_hooks)
        log.debug("Setting policies on %s …", svc["system_name"])
        svc.proxy.list().policies.append(*[rawobj.PolicyConfig(name, cfg) for name, cfg in BULK_POLICIES])
        svc.proxy.deploy()
        log.debug("Done: %s", svc["system_name"])
        with lock:
            services.append(svc)

    log.info("Creating %d extra products …", NUM_EXTRA_PRODUCTS)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_create_one) for _ in range(NUM_EXTRA_PRODUCTS)]
        for fut in as_completed(futures):
            fut.result()  # re-raise any exception
    log.info("Done creating %d extra products.", len(services))

    if not testconfig["skip_cleanup"]:
        request.addfinalizer(lambda: [f() for f in custom_service.orphan_finalizers])

    return services