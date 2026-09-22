"""The checkpoint API's errors have to arrive as something a caller can act on.

A Connect code says how an RPC ended, not what to do next: `internal` alone
covers a guest that never came back, bookkeeping that cannot be trusted and a
plain failure, and the three call for three different moves. The server names
the move in a `reason` next to the code; these tests drive the real client
against a server that answers with those bodies and check what comes out.
"""

import inspect
import json
import threading
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore
import httpx
import pytest
from packaging.version import Version

from e2b import (
    AuthenticationException,
    CheckpointBusyException,
    CheckpointChainBrokenException,
    CheckpointDiskFullException,
    CheckpointException,
    CheckpointGuestUnresponsiveException,
    CheckpointInterruptedException,
    CheckpointRootfsPoisonedException,
    CheckpointTooManyException,
    CheckpointTornException,
    ConnectionConfig,
    FileNotFoundException,
    NotFoundException,
    SandboxException,
)
from e2b.envd.api import ahandle_envd_api_exception, handle_envd_api_exception
from e2b.sandbox_async.checkpoint import AsyncCheckpoint
from e2b.sandbox_async.commands.command import Commands as AsyncCommands
from e2b.sandbox_async.filesystem.filesystem import Filesystem as AsyncFilesystem
from e2b.sandbox_sync.checkpoint import Checkpoint
from e2b.sandbox_sync.commands.command import Commands
from e2b.sandbox_sync.filesystem.filesystem import Filesystem

SANDBOX_ID = "sbx-under-test"


class CannedServer:
    """Answers every request with one canned status, body and extra headers."""

    def __init__(self):
        self.status = 200
        self.body = b"{}"
        self.extra_headers = {}
        self.paths = []

        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self._answer()

            def do_POST(self):
                self._answer()

            def _answer(self):
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)

                outer.paths.append(self.path)

                payload = outer.body
                self.send_response(outer.status)
                self.send_header("content-type", "application/json")
                for name, value in outer.extra_headers.items():
                    self.send_header(name, value)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def answer(self, status, body, **extra_headers):
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.extra_headers = extra_headers

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture()
def server():
    s = CannedServer()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def checkpoint(server):
    pool = httpcore.ConnectionPool()
    try:
        yield Checkpoint(
            server.url,
            ConnectionConfig(api_key="test"),
            pool,
            httpx.HTTPTransport(),
            SANDBOX_ID,
        )
    finally:
        pool.close()


@pytest.fixture()
async def async_checkpoint(server):
    pool = httpcore.AsyncConnectionPool()
    try:
        yield AsyncCheckpoint(
            server.url,
            ConnectionConfig(api_key="test"),
            pool,
            httpx.AsyncHTTPTransport(),
            SANDBOX_ID,
        )
    finally:
        await pool.aclose()


# The table the server writes and the SDK reads, one row per reason the
# checkpoint service can answer with.
REASONS = [
    (500, "data_loss", "torn", CheckpointTornException),
    (412, "failed_precondition", "chain_broken", CheckpointChainBrokenException),
    (500, "internal", "rootfs_poisoned", CheckpointRootfsPoisonedException),
    (500, "internal", "guest_unresponsive", CheckpointGuestUnresponsiveException),
    (503, "unavailable", "busy", CheckpointBusyException),
    (409, "aborted", "sandbox_restored", CheckpointInterruptedException),
    # Both of these refuse the call before anything is touched, so the
    # sandbox keeps running - but they are cleared up by different people:
    # a full artifact disk by whoever operates the host, a sandbox over
    # its own limit by deleting checkpoints it no longer needs.
    (507, "resource_exhausted", "disk_full", CheckpointDiskFullException),
    (429, "resource_exhausted", "too_many_checkpoints", CheckpointTooManyException),
]


@pytest.mark.parametrize("status,code,reason,expected", REASONS)
def test_reason_picks_the_exception(server, checkpoint, status, code, reason, expected):
    server.answer(status, {"code": code, "reason": reason, "message": "boom"})

    with pytest.raises(expected) as excinfo:
        checkpoint.create()

    assert type(excinfo.value) is expected
    assert excinfo.value.reason == reason
    assert excinfo.value.sandbox_id == SANDBOX_ID
    assert str(excinfo.value) == "boom"


@pytest.mark.parametrize("status,code,reason,expected", REASONS)
async def test_reason_picks_the_exception_async(
    server, async_checkpoint, status, code, reason, expected
):
    server.answer(status, {"code": code, "reason": reason, "message": "boom"})

    with pytest.raises(expected) as excinfo:
        await async_checkpoint.create()

    assert type(excinfo.value) is expected
    assert excinfo.value.reason == reason


def test_every_checkpoint_exception_is_still_a_sandbox_exception():
    # Callers that catch the base class today must keep catching these.
    for _, _, _, cls in REASONS:
        assert issubclass(cls, CheckpointException)
        assert issubclass(cls, SandboxException)


def test_busy_carries_retry_after(server, checkpoint):
    server.answer(
        503,
        {"code": "unavailable", "reason": "busy", "message": "still running"},
        **{"Retry-After": "1"},
    )

    with pytest.raises(CheckpointBusyException) as excinfo:
        checkpoint.create()

    assert excinfo.value.retry_after == 1.0


def test_busy_without_retry_after_says_none(server, checkpoint):
    server.answer(503, {"code": "unavailable", "reason": "busy", "message": "busy"})

    with pytest.raises(CheckpointBusyException) as excinfo:
        checkpoint.create()

    assert excinfo.value.retry_after is None


def test_restore_names_the_checkpoint_it_failed_on(server, checkpoint):
    server.answer(500, {"code": "data_loss", "reason": "torn", "message": "torn"})

    with pytest.raises(CheckpointTornException) as excinfo:
        checkpoint.restore("cp-42")

    assert excinfo.value.checkpoint_id == "cp-42"
    assert excinfo.value.sandbox_id == SANDBOX_ID


def test_delete_names_the_checkpoint_it_failed_on(server, checkpoint):
    server.answer(
        500, {"code": "internal", "reason": "rootfs_poisoned", "message": "poisoned"}
    )

    with pytest.raises(CheckpointRootfsPoisonedException) as excinfo:
        checkpoint.delete("cp-42")

    assert excinfo.value.checkpoint_id == "cp-42"


def test_resource_exhausted_without_a_reason_stays_the_base_class(server, checkpoint):
    # Two very different situations answer with this code, and the reason is
    # the only thing that tells them apart. With no reason the SDK says what
    # it knows -- the call was refused for want of a resource -- rather than
    # naming one of the two and sending the caller after the wrong fix.
    server.answer(429, {"code": "resource_exhausted", "message": "no room"})

    with pytest.raises(CheckpointException) as excinfo:
        checkpoint.create()

    assert type(excinfo.value) is CheckpointException
    assert excinfo.value.reason is None


async def test_resource_exhausted_without_a_reason_stays_the_base_class_async(
    server, async_checkpoint
):
    server.answer(507, {"code": "resource_exhausted", "message": "no room"})

    with pytest.raises(CheckpointException) as excinfo:
        await async_checkpoint.create()

    assert type(excinfo.value) is CheckpointException
    assert excinfo.value.reason is None


def test_create_does_not_invent_a_checkpoint_id(server, checkpoint):
    server.answer(500, {"code": "internal", "reason": "internal", "message": "nope"})

    with pytest.raises(CheckpointException) as excinfo:
        checkpoint.create()

    assert excinfo.value.checkpoint_id is None


# A server that predates `reason` sends only the code. The mapping is coarser
# -- internal cannot be told apart from itself -- but must not regress.
LEGACY = [
    (500, "data_loss", CheckpointTornException),
    (500, "failed_precondition", CheckpointChainBrokenException),
    (500, "internal", CheckpointException),
    (409, "aborted", CheckpointInterruptedException),
]


@pytest.mark.parametrize("status,code,expected", LEGACY)
def test_code_alone_still_maps(server, checkpoint, status, code, expected):
    server.answer(status, {"code": code, "message": "old server"})

    with pytest.raises(expected) as excinfo:
        checkpoint.create()

    assert type(excinfo.value) is expected
    # Nothing to report: the server never said what to do next.
    assert excinfo.value.reason == expected._default_reason


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (404, "not_found", NotFoundException),
        (401, "unauthenticated", AuthenticationException),
    ],
)
def test_codes_outside_the_checkpoint_map_keep_their_exceptions(
    server, checkpoint, status, code, expected
):
    server.answer(status, {"code": code, "reason": code, "message": "no"})

    with pytest.raises(expected):
        checkpoint.create()


# A restore does not only fail checkpoint RPCs: it kills every call in flight
# into the same sandbox, because the guest's TCP state goes back with it. The
# host answers those with 409 + sandbox_restored, from the proxy rather than
# from envd, so the body is plain JSON and not a Connect frame.
RESTORED_BODY = {
    "code": "aborted",
    "reason": "sandbox_restored",
    "message": "sandbox sbx-under-test was rolled back to a checkpoint",
}


@pytest.fixture()
def commands(server):
    pool = httpcore.ConnectionPool()
    try:
        yield Commands(
            server.url,
            ConnectionConfig(api_key="test"),
            pool,
            Version("0.2.0"),
        )
    finally:
        pool.close()


@pytest.fixture()
async def async_commands(server):
    pool = httpcore.AsyncConnectionPool()
    try:
        yield AsyncCommands(
            server.url,
            ConnectionConfig(api_key="test"),
            pool,
            Version("0.2.0"),
        )
    finally:
        await pool.aclose()


def test_restore_interrupts_a_command(server, commands):
    server.answer(409, RESTORED_BODY)

    with pytest.raises(CheckpointInterruptedException) as excinfo:
        commands.run("echo hi")

    assert excinfo.value.reason == "sandbox_restored"


async def test_restore_interrupts_a_command_async(server, async_commands):
    server.answer(409, RESTORED_BODY)

    with pytest.raises(CheckpointInterruptedException):
        await async_commands.run("echo hi")


def test_restore_interrupts_a_unary_envd_call(server, commands):
    # list() is unary where run() is a stream; both go through the same
    # decoder and the same error handler, and neither may report a closed
    # port.
    server.answer(409, RESTORED_BODY)

    with pytest.raises(CheckpointInterruptedException):
        commands.list()


def test_a_plain_409_is_not_blamed_on_a_restore(server, commands):
    # Without the reason there is nothing saying a rollback happened, and
    # guessing from the status alone would mislabel every other conflict.
    server.answer(409, {"code": "already_exists", "message": "nope"})

    with pytest.raises(SandboxException) as excinfo:
        commands.list()

    assert not isinstance(excinfo.value, CheckpointInterruptedException)


@pytest.mark.parametrize(
    "status,expected",
    [
        (200, True),
        # The proxy cannot reach the host service.
        (502, False),
        # The host has no such sandbox any more, which is what a killed
        # sandbox answers. `Sandbox.is_running()` says False for it, and this
        # probe used to raise NotFoundException for the same sandbox.
        (404, False),
    ],
)
def test_is_running_answers_instead_of_raising(server, checkpoint, status, expected):
    server.answer(status, {"code": "not_found", "message": "gone"} if status != 200 else {})

    assert checkpoint.is_running() is expected
    assert checkpoint.is_available() is expected


@pytest.mark.parametrize(
    "status,expected",
    [(200, True), (502, False), (404, False)],
)
async def test_is_running_answers_instead_of_raising_async(
    server, async_checkpoint, status, expected
):
    server.answer(status, {"code": "not_found", "message": "gone"} if status != 200 else {})

    assert await async_checkpoint.is_running() is expected


def test_is_running_still_raises_on_a_real_failure(server, checkpoint):
    # A host that is there and broken is not the same as no host.
    server.answer(500, {"code": "internal", "message": "broken"})

    with pytest.raises(SandboxException):
        checkpoint.is_running()


def test_restore_and_delete_report_the_server_field(server, checkpoint):
    # The value is whatever the server sent, not a literal - even though the
    # server has no way to send anything but true.
    server.answer(200, {"success": True})
    assert checkpoint.restore("cp-1") is True
    assert checkpoint.delete("cp-1") is True

    server.answer(200, {})
    assert checkpoint.restore("cp-1") is False
    assert checkpoint.delete("cp-1") is False


# Which default timeout each RPC gets. The three that take the sandbox's
# checkpoint lock must outlast the server's own wait on it
# (`CHECKPOINT_LOCK_WAIT_TIMEOUT`, 60 seconds by default), or the client gives
# up before the server can answer busy. A delete takes that lock too even
# though it touches no VM; a list does not.
@pytest.mark.parametrize(
    "rpc,call,expected",
    [
        ("create_checkpoint", lambda cp: cp.create(), 300.0),
        ("restore_checkpoint", lambda cp: cp.restore("cp-1"), 300.0),
        ("delete_checkpoint", lambda cp: cp.delete("cp-1"), 300.0),
        ("list_checkpoints", lambda cp: cp.list(), 60.0),
    ],
)
def test_lock_taking_rpcs_outlast_the_server_lock_wait(
    monkeypatch, checkpoint, rpc, call, expected
):
    seen = {}

    def fake(_req, request_timeout=None, **_kwargs):
        seen["request_timeout"] = request_timeout
        return SimpleNamespace(
            checkpoint_id="cp-1", mem_mode="", success=True, checkpoints=[]
        )

    monkeypatch.setattr(checkpoint._rpc, rpc, fake)

    call(checkpoint)

    assert seen["request_timeout"] == expected


@pytest.mark.parametrize(
    "rpc,call",
    [
        ("delete_checkpoint", lambda cp: cp.delete("cp-1", request_timeout=5)),
        ("create_checkpoint", lambda cp: cp.create(request_timeout=5)),
    ],
)
def test_a_per_call_timeout_still_wins(monkeypatch, checkpoint, rpc, call):
    seen = {}

    def fake(_req, request_timeout=None, **_kwargs):
        seen["request_timeout"] = request_timeout
        return SimpleNamespace(checkpoint_id="cp-1", mem_mode="", success=True)

    monkeypatch.setattr(checkpoint._rpc, rpc, fake)

    call(checkpoint)

    assert seen["request_timeout"] == 5


async def test_delete_outlasts_the_server_lock_wait_async(monkeypatch, async_checkpoint):
    seen = {}

    async def fake(_req, request_timeout=None, **_kwargs):
        seen["request_timeout"] = request_timeout
        return SimpleNamespace(success=True)

    monkeypatch.setattr(async_checkpoint._rpc, "adelete_checkpoint", fake)

    await async_checkpoint.delete("cp-1")

    assert seen["request_timeout"] == 300.0


# `files.*` does not go over Connect: the filesystem module talks to envd's
# plain HTTP API with httpx, so the 409 the proxy writes during a restore
# never reaches `handle_rpc_exception`. It has to be recognised again on the
# HTTP side, or a write caught by a restore surfaces as a bare
# `SandboxException: 409: ...` with no reason on it.
ENVD_API_VERSION = Version("0.5.7")


def _canned_transport(status, body, transport_cls):
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()

    def handler(request):
        return httpx.Response(
            status, content=payload, headers={"content-type": "application/json"}
        )

    return transport_cls(handler)


def _filesystem(status, body):
    return Filesystem(
        "http://envd",
        ENVD_API_VERSION,
        ConnectionConfig(api_key="test"),
        httpcore.ConnectionPool(),
        httpx.Client(
            base_url="http://envd",
            transport=_canned_transport(status, body, httpx.MockTransport),
        ),
    )


def _async_filesystem(status, body):
    return AsyncFilesystem(
        "http://envd",
        ENVD_API_VERSION,
        ConnectionConfig(api_key="test"),
        httpcore.AsyncConnectionPool(),
        httpx.AsyncClient(
            base_url="http://envd",
            transport=_canned_transport(status, body, httpx.MockTransport),
        ),
    )


# This branch's py-sdk baseline predates `Filesystem.write(use_octet_stream=...)`
# (openEuler delivery branch; the parameter arrived later upstream). The write
# path the test drives is the same either way, so ask the signature what it
# takes: where the parameter exists both encodings are covered, where it does
# not the single path this baseline has is covered once, and nothing has to be
# edited again when the parameter lands.
_WRITE_TAKES_OCTET_STREAM = (
    "use_octet_stream" in inspect.signature(Filesystem.write).parameters
)
OCTET_STREAM_CASES = [False, True] if _WRITE_TAKES_OCTET_STREAM else [None]


def _write_kwargs(use_octet_stream):
    if use_octet_stream is None:
        return {}

    return {"use_octet_stream": use_octet_stream}


@pytest.mark.parametrize("use_octet_stream", OCTET_STREAM_CASES)
def test_restore_interrupts_a_file_write(use_octet_stream):
    fs = _filesystem(409, RESTORED_BODY)

    with pytest.raises(CheckpointInterruptedException) as excinfo:
        fs.write("/tmp/f", "hello", **_write_kwargs(use_octet_stream))

    assert excinfo.value.reason == "sandbox_restored"
    assert str(excinfo.value) == RESTORED_BODY["message"]


@pytest.mark.parametrize("use_octet_stream", OCTET_STREAM_CASES)
async def test_restore_interrupts_a_file_write_async(use_octet_stream):
    fs = _async_filesystem(409, RESTORED_BODY)

    with pytest.raises(CheckpointInterruptedException) as excinfo:
        await fs.write("/tmp/f", "hello", **_write_kwargs(use_octet_stream))

    assert excinfo.value.reason == "sandbox_restored"


def test_restore_interrupts_a_file_read():
    fs = _filesystem(409, RESTORED_BODY)

    with pytest.raises(CheckpointInterruptedException):
        fs.read("/tmp/f")


async def test_restore_interrupts_a_file_read_async():
    fs = _async_filesystem(409, RESTORED_BODY)

    with pytest.raises(CheckpointInterruptedException):
        await fs.read("/tmp/f")


def test_a_plain_409_from_the_files_api_is_not_blamed_on_a_restore():
    fs = _filesystem(409, {"code": "already_exists", "message": "nope"})

    with pytest.raises(SandboxException) as excinfo:
        fs.write("/tmp/f", "hello")

    assert not isinstance(excinfo.value, CheckpointInterruptedException)
    assert "409" in str(excinfo.value)


def test_a_409_with_an_unparsable_body_is_not_blamed_on_a_restore():
    fs = _filesystem(409, b"<html>conflict</html>")

    with pytest.raises(SandboxException) as excinfo:
        fs.write("/tmp/f", "hello")

    assert not isinstance(excinfo.value, CheckpointInterruptedException)


def test_the_files_error_map_still_wins_where_the_restore_does_not_apply():
    # 404 keeps its own exception, and a reason on some other status is not
    # enough to call it a restore.
    fs = _filesystem(404, {"code": "not_found", "message": "no such file"})

    with pytest.raises(FileNotFoundException):
        fs.read("/tmp/f")

    fs = _filesystem(500, RESTORED_BODY)

    with pytest.raises(SandboxException) as excinfo:
        fs.read("/tmp/f")

    assert not isinstance(excinfo.value, CheckpointInterruptedException)


# The same handler serves every non-Connect envd route, the health probe
# behind `Sandbox.is_running()` included.
@pytest.mark.parametrize(
    "status,body,expected_restore",
    [
        (409, RESTORED_BODY, True),
        (409, {"code": "aborted", "message": "no reason"}, False),
        (200, {}, None),
    ],
)
def test_the_envd_api_handler_reads_the_reason(status, body, expected_restore):
    res = httpx.Response(status, json=body)

    err = handle_envd_api_exception(res)

    if expected_restore is None:
        assert err is None
    elif expected_restore:
        assert isinstance(err, CheckpointInterruptedException)
        assert err.reason == "sandbox_restored"
    else:
        assert isinstance(err, SandboxException)
        assert not isinstance(err, CheckpointInterruptedException)


async def test_the_async_envd_api_handler_reads_the_reason():
    err = await ahandle_envd_api_exception(httpx.Response(409, json=RESTORED_BODY))

    assert isinstance(err, CheckpointInterruptedException)
    assert err.reason == "sandbox_restored"
