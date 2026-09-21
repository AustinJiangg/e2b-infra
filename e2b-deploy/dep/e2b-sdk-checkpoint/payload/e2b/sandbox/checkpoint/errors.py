from typing import Callable, Dict, Optional, Type

from e2b_connect.client import Code, ConnectException

from e2b.envd.rpc import handle_rpc_exception
from e2b.exceptions import (
    CheckpointBusyException,
    CheckpointChainBrokenException,
    CheckpointDiskFullException,
    CheckpointException,
    CheckpointGuestUnresponsiveException,
    CheckpointInterruptedException,
    CheckpointRootfsPoisonedException,
    CheckpointTooManyException,
    CheckpointTornException,
)

# What the server's `reason` means for the caller. This is the primary map:
# the reason is the field that says what to do next, and several of these
# arrive under the same Connect code.
_CHECKPOINT_REASON_MAP: Dict[str, Type[CheckpointException]] = {
    "torn": CheckpointTornException,
    "chain_broken": CheckpointChainBrokenException,
    "rootfs_poisoned": CheckpointRootfsPoisonedException,
    "guest_unresponsive": CheckpointGuestUnresponsiveException,
    "busy": CheckpointBusyException,
    "sandbox_restored": CheckpointInterruptedException,
    "disk_full": CheckpointDiskFullException,
    "too_many_checkpoints": CheckpointTooManyException,
}

# Fallback for a server that predates `reason`, and for the reasons that are
# only a restatement of the code. It is coarser on purpose: `internal` there
# covers a guest that never came back, poisoned bookkeeping and a plain
# failure, and nothing in the response tells them apart.
#
# The codes left out - not_found, unauthenticated, invalid_argument,
# unavailable - keep the exceptions they already raised, which callers catch
# today and which mean the same thing here as everywhere else.
_CHECKPOINT_CODE_MAP: Dict[Code, Type[CheckpointException]] = {
    Code.data_loss: CheckpointTornException,
    Code.failed_precondition: CheckpointChainBrokenException,
    Code.aborted: CheckpointInterruptedException,
    Code.internal: CheckpointException,
    # Both reasons that arrive under this code refuse the call before
    # anything is touched, and the sandbox survives either way - but a
    # full host disk and a sandbox over its own limit are cleared up by
    # different people, and without the reason there is nothing to pick
    # between them with, so the base class is raised.
    Code.resource_exhausted: CheckpointException,
}


def _map_checkpoint_error(
    e: Exception,
    checkpoint_id: Optional[str] = None,
    sandbox_id: Optional[str] = None,
) -> Exception:
    """Map an error from a checkpoint RPC onto a checkpoint exception.

    Reason first, code second: the reason is what the server says the caller
    has to do next, and it is the only thing that separates the three very
    different failures that all end up as `internal`. A server that does not
    send one is mapped from the code alone, which is what the SDK did before
    this map existed.

    :param e: The caught exception, expected to be a ``ConnectException``.
    :param checkpoint_id: The checkpoint the call named, if any.
    :param sandbox_id: The sandbox the call was made against.
    :return: The corresponding exception, or whatever
        :func:`~e2b.envd.rpc.handle_rpc_exception` makes of it when this map
        has nothing more specific to say.
    """
    if not isinstance(e, ConnectException):
        return handle_rpc_exception(e)

    reason = getattr(e, "reason", None)

    factory: Optional[Type[CheckpointException]] = None
    if isinstance(reason, str):
        factory = _CHECKPOINT_REASON_MAP.get(reason)

    if factory is None:
        factory = _CHECKPOINT_CODE_MAP.get(e.status)

    if factory is None:
        return handle_rpc_exception(e)

    kwargs: Dict[str, object] = {
        "reason": reason,
        "checkpoint_id": checkpoint_id,
        "sandbox_id": sandbox_id,
    }

    if issubclass(factory, CheckpointBusyException):
        kwargs["retry_after"] = getattr(e, "retry_after", None)

    return factory(e.message, **kwargs)  # type: ignore[arg-type]
