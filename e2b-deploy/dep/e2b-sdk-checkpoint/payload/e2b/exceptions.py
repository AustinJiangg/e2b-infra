from typing import Optional


def format_sandbox_timeout_exception(message: str):
    return TimeoutException(
        f"{message}: This error is likely due to sandbox timeout. You can modify the sandbox timeout by passing 'timeout' when starting the sandbox or calling '.set_timeout' on the sandbox with the desired timeout."
    )


def format_request_timeout_error() -> Exception:
    return TimeoutException(
        "Request timed out — the 'request_timeout' option can be used to increase this timeout",
    )


def format_execution_timeout_error() -> Exception:
    return TimeoutException(
        "Execution timed out — the 'timeout' option can be used to increase this timeout",
    )


class SandboxException(Exception):
    """
    Base class for all sandbox errors.

    Raised when a general sandbox exception occurs.
    """

    pass


class TimeoutException(SandboxException):
    """
    Raised when a timeout occurs.

    The `unavailable` exception type is caused by sandbox timeout.\n
    The `canceled` exception type is caused by exceeding request timeout.\n
    The `deadline_exceeded` exception type is caused by exceeding the timeout for process, watch, etc.\n
    The `unknown` exception type is sometimes caused by the sandbox timeout when the request is not processed correctly.\n
    """

    pass


class InvalidArgumentException(SandboxException):
    """
    Raised when an invalid argument is provided.
    """

    pass


class NotEnoughSpaceException(SandboxException):
    """
    Raised when there is not enough disk space.
    """

    pass


class NotFoundException(SandboxException):
    """
    Raised when a resource is not found.

    .. deprecated::
        Use :class:`FileNotFoundException` or :class:`SandboxNotFoundException` instead.
        This class will be removed in the next major version.
    """

    pass


class FileNotFoundException(NotFoundException):
    """
    Raised when a file or directory is not found inside a sandbox.
    """

    pass


class SandboxNotFoundException(NotFoundException):
    """
    Raised when a sandbox is not found (e.g. it doesn't exist or is no longer running).
    """

    pass


class AuthenticationException(Exception):
    """
    Raised when authentication fails.
    """

    pass


class GitAuthException(AuthenticationException):
    """
    Raised when git authentication fails.
    """

    pass


class GitUpstreamException(SandboxException):
    """
    Raised when git upstream tracking is missing.
    """

    pass


class TemplateException(SandboxException):
    """
    Exception raised when the template uses old envd version. It isn't compatible with the new SDK.
    """


class RateLimitException(SandboxException):
    """
    Raised when the API rate limit is exceeded.
    """


class BuildException(Exception):
    """
    Raised when the build fails.
    """


class FileUploadException(BuildException):
    """
    Raised when the file upload fails.
    """


class VolumeException(Exception):
    """
    Base class for all volume errors.

    Raised when general volume errors occur.
    """


class CheckpointException(SandboxException):
    """
    Base class for errors from the checkpoint API.

    A Connect code says how the RPC ended, not what the caller has to do next:
    ``internal`` alone covers a guest that never came back, bookkeeping that
    cannot be trusted and a snapshot that failed, and those call for three
    different moves. The server names the move in a ``reason`` field next to
    the code, and that is what picks the subclass raised here.

    ``reason`` is ``None`` against a server that predates the field, in which
    case the subclass was inferred from the code alone and is necessarily
    coarser.
    """

    # The reason this subclass stands for, used when the server did not send
    # one. ``None`` on the base class, which is what an unclassified failure
    # is raised as.
    _default_reason: Optional[str] = None

    def __init__(
        self,
        message: str,
        *,
        reason: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
    ):
        super().__init__(message)

        self.reason = reason if reason is not None else self._default_reason
        """What the caller has to do next, as named by the server. ``None``
        when the server does not report it."""

        self.checkpoint_id = checkpoint_id
        """The checkpoint the failed call named, when it named one."""

        self.sandbox_id = sandbox_id
        """The sandbox the failed call was made against."""


class CheckpointTornException(CheckpointException):
    """
    Raised when a restore failed past the point of no return.

    The sandbox is left between two moments in time: part of it is at the
    checkpoint, part of it is where it was, and nothing can tell how much of
    the target landed. Not retryable and not recoverable - the server refuses
    every further checkpoint and restore for this sandbox. Destroy it and
    create a new one.
    """

    _default_reason = "torn"


class CheckpointChainBrokenException(CheckpointException):
    """
    Raised when the checkpoint being restored has no usable disk view.

    The incremental chain is broken, so there is nothing sound to roll back
    to. The sandbox itself keeps running at its current state. Take a fresh
    checkpoint, which starts a new chain, before restoring again.
    """

    _default_reason = "chain_broken"


class CheckpointRootfsPoisonedException(CheckpointException):
    """
    Raised when the rootfs bookkeeping can no longer be trusted.

    A checkpoint's write layer was sealed into the live read stack without
    anything recording it, so a later checkpoint could serve stale disk
    content without a word. The sandbox keeps running and checkpoints are
    refused until a restore reseeds the bookkeeping: restoring any existing
    checkpoint clears it.
    """

    _default_reason = "rootfs_poisoned"


class CheckpointGuestUnresponsiveException(CheckpointException):
    """
    Raised when the host side of the operation finished but the guest did not
    come back.

    The VM is at the intended state and the host is done; envd inside the
    guest did not answer within the wait. The sandbox is not torn - what is
    stuck is inside it.
    """

    _default_reason = "guest_unresponsive"


class CheckpointBusyException(CheckpointException):
    """
    Raised when another checkpoint operation on the same sandbox is still
    running.

    Checkpoint operations on one sandbox are serialized on the host, and a
    caller that queues longer than it is worth queueing is told to come back
    rather than held on a connection. Retryable.
    """

    _default_reason = "busy"

    def __init__(
        self,
        message: str,
        *,
        reason: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
        retry_after: Optional[float] = None,
    ):
        super().__init__(
            message,
            reason=reason,
            checkpoint_id=checkpoint_id,
            sandbox_id=sandbox_id,
        )

        self.retry_after = retry_after
        """Seconds the server asked the caller to wait, from ``Retry-After``.
        ``None`` when the server did not send the header or sent it as a
        date."""


class CheckpointInterruptedException(CheckpointException):
    """
    Raised when a call into the sandbox was cut short by a restore.

    A restore rolls the guest's TCP state back to checkpoint time, so every
    connection held across it is talking to a peer that has never heard of
    it. The call did not fail on its own and the service inside the sandbox
    is not broken: reconnect and retry. Whether the call had already taken
    effect in the guest is not knowable from here - the rollback undid
    everything up to the checkpoint either way.
    """

    _default_reason = "sandbox_restored"


class CheckpointDiskFullException(CheckpointException):
    """
    Raised when the host has too little free space left on the artifact disk
    to take or restore a checkpoint.

    The server checks free space before it touches anything, so nothing was
    started: the sandbox keeps running at its current state and the
    checkpoints it already has are intact. This is about the host and not
    about this sandbox - deleting this sandbox's checkpoints is unlikely to
    free enough, and the fix is for whoever operates the host to reclaim
    space. Retryable once that has happened; retrying in a tight loop only
    repeats the same refusal.
    """

    _default_reason = "disk_full"


class CheckpointTooManyException(CheckpointException):
    """
    Raised when the sandbox already holds as many checkpoints as the server
    allows one sandbox to keep.

    The limit is per sandbox and the check runs before anything is written, so
    the sandbox keeps running and its existing checkpoints are intact. Unlike
    a full disk this is the caller's own to fix: list the sandbox's
    checkpoints, delete the ones no longer needed, and create again.
    """

    _default_reason = "too_many_checkpoints"
