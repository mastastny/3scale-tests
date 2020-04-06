"""
Rewrite /spec/functional_specs/do_not_send_openresty_version_spec.rb

Verifies JIRA: https://issues.jboss.org/browse/THREESCALE-1989

When requesting non existing endpoint openresty version should not be sent
in the response body or in the response header
"""
import pytest
import requests
from testsuite import rawobj


@pytest.fixture(scope="module")
def service_proxy_settings():
    """Set backend url"""
    return rawobj.Proxy("https://echo-api.example.local")


@pytest.fixture(scope="module")
def api_client(application):
    """
    Client configured not to retry requests.

    By default, the failed requests are retried by the api_client.
    As 404 is the desired outcome of one of the tests, the client is
    configured not to retry requests to avoid long time execution.
    """
    session = requests.Session()
    session.auth = application.authobj
    return application.api_client(session=session)


def test_do_not_send_openresty_version(api_client):
    """
    Make request to non existing endpoint
    Assert that the response does not contain openresty version in the headers
    Assert that the response does not contain openresty version in the body
    """
    response = api_client.get("/anything")
    assert response.status_code == 503

    assert "server" in response.headers
    assert response.headers["server"] == "openresty"

    assert "<center>openresty</center>" in response.text
