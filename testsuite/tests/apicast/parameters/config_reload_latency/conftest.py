"""
Conftest for APIcast config reload latency reproduction test.

Creates a dedicated custom tenant so all created objects (200+ products) can
be wiped easily by deleting the tenant.  The self-managed gateway is pointed
at that tenant and is configured with:
  - configurationLoadMode=boot   (reload config on startup + every N seconds)
  - cacheConfigurationSeconds=60 (short interval to make reload easy to trigger)
"""
from urllib.parse import urlparse

import pytest

from testsuite import rawobj, resilient
from testsuite.utils import blame


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
    cacheConfigurationSeconds.  60 s is short enough to trigger a reload
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
