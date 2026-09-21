import httpx

from typing import Any, Callable, Optional

from e2b.envd.rpc import RESTORED_REASON
from e2b.exceptions import (
    CheckpointInterruptedException,
    SandboxException,
    NotFoundException,
    AuthenticationException,
    InvalidArgumentException,
    NotEnoughSpaceException,
    format_sandbox_timeout_exception,
)


ENVD_API_FILES_ROUTE = "/files"
ENVD_API_HEALTH_ROUTE = "/health"

_DEFAULT_API_ERROR_MAP: dict[int, Callable[[str], Exception]] = {
    400: InvalidArgumentException,
    401: AuthenticationException,
    404: NotFoundException,
    429: lambda message: SandboxException(
        f"{message}: The requests are being rate limited."
    ),
    502: format_sandbox_timeout_exception,
    507: NotEnoughSpaceException,
}


def get_error_body(e: httpx.Response) -> dict[str, Any]:
    """Read an error response body as the JSON object envd and the proxy send.

    Anything that is not a JSON object - an HTML error page from something in
    between, a truncated body - reads as no fields at all, so callers fall
    back on the raw text.
    """
    try:
        body = e.json()
    except ValueError:
        return {}

    return body if isinstance(body, dict) else {}


def get_message(e: httpx.Response) -> str:
    return get_error_body(e).get("message", e.text)


def handle_envd_api_exception(
    res: httpx.Response,
    error_map: Optional[dict[int, Callable[[str], Exception]]] = None,
):
    """Handle errors from envd API responses by mapping HTTP status codes to specific exception types.

    :param res: The HTTP response.
    :param error_map: Optional map of HTTP status codes to exception factories that override the defaults.
    :return: The corresponding exception, or ``None`` if the response is successful.
    """
    if res.is_success:
        return

    res.read()

    return _format_envd_api_response_exception(res, error_map)


async def ahandle_envd_api_exception(
    res: httpx.Response,
    error_map: Optional[dict[int, Callable[[str], Exception]]] = None,
):
    """Async version of :func:`handle_envd_api_exception`."""
    if res.is_success:
        return

    await res.aread()

    return _format_envd_api_response_exception(res, error_map)


def _format_envd_api_response_exception(
    res: httpx.Response,
    error_map: Optional[dict[int, Callable[[str], Exception]]] = None,
):
    body = get_error_body(res)

    return format_envd_api_exception(
        res.status_code,
        body.get("message", res.text),
        error_map,
        reason=body.get("reason"),
    )


def format_envd_api_exception(
    status_code: int,
    message: str,
    error_map: Optional[dict[int, Callable[[str], Exception]]] = None,
    reason: Optional[str] = None,
):
    """Map an HTTP status code and message to the appropriate exception.

    :param status_code: The HTTP status code.
    :param message: The error message from the response body.
    :param error_map: Optional map of HTTP status codes to exception factories that override the defaults.
    :param reason: The ``reason`` field of the error body, when the server sent one.
    :return: The corresponding exception.
    """
    # Ahead of both maps, as in :func:`e2b.envd.rpc.handle_rpc_exception`: the
    # reason says why the call ended and the status cannot. These requests go
    # to envd over plain HTTP rather than over Connect, so a restore that cuts
    # one short lands here instead of there - same 409 written by the same
    # proxy, and the caller needs the same answer: reconnect and retry.
    #
    # The status is part of the test because a 409 without the reason is an
    # ordinary conflict, and there is nothing else saying a rollback happened.
    if status_code == 409 and reason == RESTORED_REASON:
        return CheckpointInterruptedException(message, reason=RESTORED_REASON)

    if error_map and status_code in error_map:
        return error_map[status_code](message)

    if status_code in _DEFAULT_API_ERROR_MAP:
        return _DEFAULT_API_ERROR_MAP[status_code](message)

    return SandboxException(f"{status_code}: {message}")
