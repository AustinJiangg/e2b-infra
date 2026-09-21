import os
import ssl

from typing import Optional, Dict, TypedDict, Union

from httpx._types import ProxyTypes
from typing_extensions import Unpack

from e2b.api.metadata import package_version

REQUEST_TIMEOUT: float = 60.0  # 60 seconds

CHECKPOINT_REQUEST_TIMEOUT: float = 300.0  # 5 minutes
"""
Default timeout for creating and restoring a checkpoint.

These two are not ordinary requests. The host pauses the VM, writes or reads a
memory snapshot and then waits up to 45 seconds for envd inside the guest to
answer again before it gives up and reports the failure - so the *error* path
alone can take longer than the 60 second default used everywhere else, and a
full snapshot of a large template can take longer still. A client timeout below
that only abandons work the server keeps doing: it has no cancellation, and for
a create it means the checkpoint is made but its ID never reaches the caller.
Deleting a checkpoint touches no VM, but it takes the same per-sandbox
lock as create and restore and so can queue behind one of them: the server
waits up to its own ``CHECKPOINT_LOCK_WAIT_TIMEOUT`` (60 seconds by
default) before answering busy, and a client sitting on the 60 second
default would time out first instead of seeing that answer. Delete uses
this timeout too. Listing takes no lock and keeps the 60 second default.
"""

KEEPALIVE_PING_INTERVAL_SEC = 50  # 50 seconds
KEEPALIVE_PING_HEADER = "Keepalive-Ping-Interval"


class ApiParams(TypedDict, total=False):
    """
    Parameters for a request.

    In the case of a sandbox, it applies to all **requests made to the returned sandbox**.
    """

    request_timeout: Optional[float]
    """Timeout for the request in **seconds**, defaults to 60 seconds."""

    headers: Optional[Dict[str, str]]
    """Additional headers to send with the request."""

    api_key: Optional[str]
    """E2B API Key to use for authentication, defaults to `E2B_API_KEY` environment variable."""

    domain: Optional[str]
    """E2B domain to use for authentication, defaults to `E2B_DOMAIN` environment variable."""

    api_url: Optional[str]
    """URL to use for the API, defaults to `https://api.<domain>`. For internal use only."""

    debug: Optional[bool]
    """Whether to use debug mode, defaults to `E2B_DEBUG` environment variable."""

    proxy: Optional[ProxyTypes]
    """Proxy to use for the request. In case of a sandbox it applies to all **requests made to the returned sandbox**."""

    sandbox_url: Optional[str]
    """URL to connect to sandbox, defaults to `E2B_SANDBOX_URL` environment variable."""


class ConnectionConfig:
    """
    Configuration for the connection to the API.
    """

    envd_port = 49983
    checkpointd_port = 49984

    @staticmethod
    def _domain():
        return os.getenv("E2B_DOMAIN") or "e2b.app"

    @staticmethod
    def _debug():
        return os.getenv("E2B_DEBUG", "false").lower() == "true"

    @staticmethod
    def _api_key():
        return os.getenv("E2B_API_KEY")

    @staticmethod
    def _api_url():
        return os.getenv("E2B_API_URL")

    @staticmethod
    def _sandbox_url():
        return os.getenv("E2B_SANDBOX_URL")

    @staticmethod
    def _access_token():
        return os.getenv("E2B_ACCESS_TOKEN")

    @staticmethod
    def _verify_ssl():
        val = os.getenv("E2B_HTTP_SSL", "true").lower()
        if val == "false":
            return False
        return True

    def __init__(
        self,
        domain: Optional[str] = None,
        debug: Optional[bool] = None,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        sandbox_url: Optional[str] = None,
        access_token: Optional[str] = None,
        request_timeout: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
        extra_sandbox_headers: Optional[Dict[str, str]] = None,
        proxy: Optional[ProxyTypes] = None,
        traffic_access_token: Optional[str] = None,
    ):
        self.domain = domain or ConnectionConfig._domain()
        self.debug = debug or ConnectionConfig._debug()
        self.api_key = api_key or ConnectionConfig._api_key()
        self.access_token = access_token or ConnectionConfig._access_token()
        self.headers = headers or {}
        self.headers["User-Agent"] = f"e2b-python-sdk/{package_version}"
        self.__extra_sandbox_headers = extra_sandbox_headers or {}
        self.__traffic_access_token = traffic_access_token

        self.proxy = proxy
        self.verify_ssl: Union[str, bool, ssl.SSLContext] = True
        if ConnectionConfig._verify_ssl() is False:
            self.verify_ssl = False

        self.request_timeout = ConnectionConfig._get_request_timeout(
            REQUEST_TIMEOUT,
            request_timeout,
        )

        if request_timeout == 0:
            self.request_timeout = None
        elif request_timeout is not None:
            self.request_timeout = request_timeout
        else:
            self.request_timeout = REQUEST_TIMEOUT

        self.api_url = (
            api_url
            or ConnectionConfig._api_url()
            or ("http://localhost:3000" if self.debug else f"https://api.{self.domain}")
        )

        self._sandbox_url: Optional[str] = (
            sandbox_url or ConnectionConfig._sandbox_url()
        )

    @staticmethod
    def _get_request_timeout(
        default_timeout: Optional[float],
        request_timeout: Optional[float],
    ):
        if request_timeout == 0:
            return None
        elif request_timeout is not None:
            return request_timeout
        else:
            return default_timeout

    def get_request_timeout(self, request_timeout: Optional[float] = None):
        return self._get_request_timeout(self.request_timeout, request_timeout)

    def get_checkpoint_request_timeout(self, request_timeout: Optional[float] = None):
        """
        Timeout for the checkpoint operations that take the sandbox's
        checkpoint lock.

        A per-call ``request_timeout`` still wins; what this deliberately does
        not inherit is the connection-wide default, which is tuned for envd
        requests and is below what the host needs to even report a checkpoint
        failure. See :data:`CHECKPOINT_REQUEST_TIMEOUT`.
        """
        return self._get_request_timeout(CHECKPOINT_REQUEST_TIMEOUT, request_timeout)

    def get_sandbox_url(self, sandbox_id: str, sandbox_domain: str) -> str:
        if self._sandbox_url:
            return self._sandbox_url  # type: ignore[return-value]

        return f"{'http' if self.debug or self.verify_ssl is False else 'https'}://{self.get_host(sandbox_id, sandbox_domain, self.envd_port)}"

    def get_checkpointd_url(self, sandbox_id: str, sandbox_domain: str) -> str:
        return f"{'http' if self.debug or self.verify_ssl is False else 'https'}://{self.get_host(sandbox_id, sandbox_domain, self.checkpointd_port)}"

    def get_host(self, sandbox_id: str, sandbox_domain: str, port: int) -> str:
        """
        Get the host address to connect to the sandbox.
        You can then use this address to connect to the sandbox port from outside the sandbox via HTTP or WebSocket.

        :param port: Port to connect to
        :param sandbox_domain: Domain to connect to
        :param sandbox_id: Sandbox to connect to

        :return: Host address to connect to
        """
        if self.debug:
            return f"localhost:{port}"

        return f"{port}-{sandbox_id}.{sandbox_domain}"

    def get_api_params(
        self,
        **opts: Unpack[ApiParams],
    ) -> dict:
        """
        Get the parameters for the API call.

        This is used to avoid passing the following attributes to the API call:
        - access_token
        - api_url

        It also returns a copy, so the original object is not modified.

        :return: Dictionary of parameters for the API call
        """
        headers = opts.get("headers")
        request_timeout = opts.get("request_timeout")
        api_key = opts.get("api_key")
        api_url = opts.get("api_url")
        domain = opts.get("domain")
        debug = opts.get("debug")
        proxy = opts.get("proxy")

        req_headers = self.headers.copy()
        if headers is not None:
            req_headers.update(headers)

        return dict(
            ApiParams(
                api_key=api_key if api_key is not None else self.api_key,
                api_url=api_url if api_url is not None else self.api_url,
                domain=domain if domain is not None else self.domain,
                debug=debug if debug is not None else self.debug,
                request_timeout=self.get_request_timeout(request_timeout),
                headers=req_headers,
                proxy=proxy if proxy is not None else self.proxy,
            )
        )

    @property
    def sandbox_headers(self):
        """
        We need this separate as we use the same header for E2B access token to API and envd access token to sandbox.
        """
        return {
            **self.headers,
            **self.__extra_sandbox_headers,
        }

    @property
    def checkpointd_headers(self):
        # No Authorization header: the checkpoint API is served by the
        # orchestrator on the host, and it authenticates the caller with the
        # sandbox's own traffic access token. That token is *not* part of the
        # shared headers, so it has to be added here. Sandboxes that allow
        # public access have no such token and the server lets them through.
        headers = {
            **self.headers,
            **self.__extra_sandbox_headers,
            "E2b-Sandbox-Port": str(self.checkpointd_port),
        }

        if self.__traffic_access_token is not None:
            headers["e2b-traffic-access-token"] = self.__traffic_access_token

        return headers


Username = str
"""
User used for the operation in the sandbox.
"""

default_username: Username = "user"
"""
Default user used for the operation in the sandbox.
"""
