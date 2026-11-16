import httpcore
import httpx

from e2b import ConnectionConfig
from e2b.gsd.api import (
    ACCESS_TOKEN_HEADER,
    TRAFFIC_ACCESS_TOKEN_HEADER,
    checkpointd_headers,
)
from e2b.sandbox_async.checkpoint import AsyncCheckpoint
from e2b.sandbox_sync.checkpoint import Checkpoint

URL = "http://49984-sbx.example.test"


def _config(extra=None):
    return ConnectionConfig(extra_sandbox_headers=extra or {})


def test_token_added_when_config_lacks_it():
    headers = checkpointd_headers(_config(), "tok")
    assert headers[ACCESS_TOKEN_HEADER] == "tok"
    assert headers["E2b-Sandbox-Port"] == "49984"


def test_token_from_extra_headers_kept():
    headers = checkpointd_headers(_config({ACCESS_TOKEN_HEADER: "tok"}), None)
    assert headers[ACCESS_TOKEN_HEADER] == "tok"


def test_no_token_for_non_secure_sandbox():
    headers = checkpointd_headers(_config(), None)
    assert ACCESS_TOKEN_HEADER not in headers
    # api client models use an Unset sentinel instead of None
    headers = checkpointd_headers(_config(), object())
    assert ACCESS_TOKEN_HEADER not in headers


def test_no_basic_auth_header():
    # GSD authenticates X-Access-Token only; the old fixed "Basic root:" is gone.
    assert "Authorization" not in checkpointd_headers(_config(), "tok")


def test_sync_checkpoint_sends_token():
    ckpt = Checkpoint(
        URL,
        _config(),
        httpcore.ConnectionPool(),
        httpx.Client(),
        envd_access_token="tok",
    )
    assert ckpt._headers[ACCESS_TOKEN_HEADER] == "tok"
    assert ckpt._rpc._create_checkpoint._headers[ACCESS_TOKEN_HEADER] == "tok"
    assert ckpt._rpc._restore_checkpoint._headers[ACCESS_TOKEN_HEADER] == "tok"


def test_async_checkpoint_sends_token():
    ckpt = AsyncCheckpoint(
        URL,
        _config(),
        httpcore.AsyncConnectionPool(),
        httpx.AsyncClient(),
        envd_access_token="tok",
    )
    assert ckpt._headers[ACCESS_TOKEN_HEADER] == "tok"
    assert ckpt._rpc._create_checkpoint._headers[ACCESS_TOKEN_HEADER] == "tok"
    assert ckpt._rpc._delete_checkpoint._headers[ACCESS_TOKEN_HEADER] == "tok"


def test_traffic_token_sent_when_sandbox_has_one():
    # allow_public_traffic=False: the proxy refuses the GSD port without this header.
    headers = checkpointd_headers(_config(), "tok", "traffic")
    assert headers[TRAFFIC_ACCESS_TOKEN_HEADER] == "traffic"
    assert TRAFFIC_ACCESS_TOKEN_HEADER not in checkpointd_headers(_config(), "tok")
    assert TRAFFIC_ACCESS_TOKEN_HEADER not in checkpointd_headers(
        _config(), "tok", object()
    )

    ckpt = Checkpoint(
        URL,
        _config(),
        httpcore.ConnectionPool(),
        httpx.Client(),
        traffic_access_token="traffic",
    )
    assert ckpt._headers[TRAFFIC_ACCESS_TOKEN_HEADER] == "traffic"
    assert (
        ckpt._rpc._create_checkpoint._headers[TRAFFIC_ACCESS_TOKEN_HEADER] == "traffic"
    )
