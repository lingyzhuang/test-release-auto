import requests
from requests.adapters import HTTPAdapter
from requests_kerberos import HTTPKerberosAuth
from urllib3.util.retry import Retry


def requests_session(request_auth=True):
    """Returns http(s) session for request processing."""

    session = requests.Session()
    if request_auth:
        session.auth = HTTPKerberosAuth()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=1,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=Retry.DEFAULT_ALLOWED_METHODS.union(("POST",)),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers["User-Agent"] = "product-configure-tool"
    session.headers["Content-type"] = "application/json"
    return session
