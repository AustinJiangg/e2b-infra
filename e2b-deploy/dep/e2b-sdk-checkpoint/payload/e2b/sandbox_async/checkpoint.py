from typing import List, Optional

import httpcore
import httpx
from e2b.connection_config import ConnectionConfig
from e2b.checkpointd.api import CHECKPOINTD_HEALTH_ROUTE, ahandle_checkpointd_exception
from e2b.checkpointd.checkpoint import checkpoint_connect, checkpoint_pb2
from e2b.sandbox.checkpoint.errors import _map_checkpoint_error
from e2b.sandbox.checkpoint.types import CheckpointInfo


class AsyncCheckpoint:
    """
    Module for checkpointing and restoring sandbox state (async).

    The checkpoint endpoints live on their own port on the sandbox address, but
    nothing inside the sandbox answers them: a checkpoint pauses the VM and
    drives the hypervisor's snapshot API, neither of which anything running in
    the guest can do. The orchestrator on the host intercepts this port and
    answers it directly. Sandbox-local state, removed with the sandbox.
    """

    def __init__(
        self,
        checkpointd_api_url: str,
        connection_config: ConnectionConfig,
        pool: httpcore.AsyncConnectionPool,
        transport: httpx.AsyncBaseTransport,
        sandbox_id: Optional[str] = None,
    ) -> None:
        self._connection_config = connection_config
        # Only carried so a raised exception can name the sandbox it is about.
        self._sandbox_id = sandbox_id
        self._rpc = checkpoint_connect.CheckpointClient(
            checkpointd_api_url,
            async_pool=pool,
            json=True,
            headers=connection_config.checkpointd_headers,
            # No transport-level retries. None of these RPCs is safe to
            # replay: a replayed create makes a second checkpoint the caller
            # never learns about, a replayed restore rolls the guest back a
            # second time, and a replayed delete comes back as 404. The
            # default (4 attempts on a mid-response disconnect) is meant for
            # envd's idempotent calls.
            retries=0,
        )
        self._health_api = httpx.AsyncClient(
            base_url=checkpointd_api_url,
            transport=transport,
            headers=connection_config.checkpointd_headers,
            verify=connection_config.verify_ssl,
        )

    async def is_running(self, request_timeout: Optional[float] = None) -> bool:
        """
        Check whether the checkpoint API answers for this sandbox.

        Kept under this name for backwards compatibility; :meth:`is_available`
        is the same call under a name that matches what it does. It is a
        liveness probe on the host-side service, not on anything in the guest,
        so a live sandbox answers it whether or not any daemon runs inside.

        :param request_timeout: Timeout for the request in **seconds**

        :return: ``True`` if the checkpoint API answers, ``False`` otherwise
        """
        r = await self._health_api.get(
            CHECKPOINTD_HEALTH_ROUTE,
            timeout=self._connection_config.get_request_timeout(request_timeout),
        )

        # 502 is the proxy failing to reach the host; 404 is the host saying
        # it has no such sandbox, which is what a killed sandbox answers.
        # Both mean the API does not answer for this sandbox, which is what
        # this probe is asked, and `Sandbox.is_running()` returns False for
        # the same sandbox rather than raising. A timeout still propagates:
        # not getting an answer is not the same as getting a no.
        if r.status_code in (404, 502):
            return False

        err = await ahandle_checkpointd_exception(r)

        if err:
            raise err

        return True

    async def is_available(self, request_timeout: Optional[float] = None) -> bool:
        """
        Whether the checkpoint API answers for this sandbox. Same call as
        :meth:`is_running`, under a name that says what it checks.
        """
        return await self.is_running(request_timeout)

    async def create(
        self,
        name: Optional[str] = None,
        request_timeout: Optional[float] = None,
    ) -> CheckpointInfo:
        """
        Create a checkpoint of the sandbox's current state.

        :param name: Optional name for the checkpoint
        :param request_timeout: Timeout for the request in **seconds**,
            defaults to 300 seconds. The host pauses the VM, writes the memory
            snapshot and then waits up to 45 seconds for envd in the guest to
            answer again before reporting a failure, so a lower timeout can
            expire while the checkpoint is still being made - and it is the
            server that names it, so its ID would be lost.

        :return: CheckpointInfo with the checkpoint ID and metadata
        """
        try:
            req = checkpoint_pb2.CreateCheckpointRequest()
            if name is not None:
                req.name = name

            res = await self._rpc.acreate_checkpoint(
                req,
                request_timeout=self._connection_config.get_checkpoint_request_timeout(
                    request_timeout
                ),
            )

            return CheckpointInfo(
                checkpoint_id=res.checkpoint_id,
                name=name,
                # Empty when the server predates the field. "full" here means
                # the checkpoint copied all of guest memory instead of only the
                # pages dirtied since the last one — which is what silently
                # happens when dirty page tracking is off on the host.
                mem_mode=res.mem_mode or None,
            )
        except Exception as e:
            raise _map_checkpoint_error(e, sandbox_id=self._sandbox_id)

    async def restore(
        self,
        checkpoint_id: str,
        request_timeout: Optional[float] = None,
    ) -> bool:
        """
        Restore the sandbox to a previously created checkpoint.

        :param checkpoint_id: ID of the checkpoint to restore
        :param request_timeout: Timeout for the request in **seconds**,
            defaults to 300 seconds. The host rolls the VM back and then waits
            up to 45 seconds for envd in the guest to answer again before
            reporting a failure, so a lower timeout can expire while the
            sandbox is mid-rollback and the client learns nothing about how it
            ended.

        :return: ``True``. A restore that fails answers with an error status
            and is raised, so this is the only value that can be returned and
            is kept for backwards compatibility. Do not branch on it: the
            outcome to check for is an exception, and which one.
        """
        try:
            res = await self._rpc.arestore_checkpoint(
                checkpoint_pb2.RestoreCheckpointRequest(
                    checkpoint_id=checkpoint_id,
                ),
                request_timeout=self._connection_config.get_checkpoint_request_timeout(
                    request_timeout
                ),
            )
            return res.success
        except Exception as e:
            raise _map_checkpoint_error(
                e, checkpoint_id=checkpoint_id, sandbox_id=self._sandbox_id
            )

    async def list(
        self,
        request_timeout: Optional[float] = None,
    ) -> List[CheckpointInfo]:
        """
        List all checkpoints in the sandbox.

        :param request_timeout: Timeout for the request in **seconds**

        :return: List of CheckpointInfo objects
        """
        try:
            res = await self._rpc.alist_checkpoints(
                checkpoint_pb2.ListCheckpointsRequest(),
                request_timeout=self._connection_config.get_request_timeout(
                    request_timeout
                ),
            )

            result = []
            for cp in res.checkpoints:
                result.append(
                    CheckpointInfo(
                        checkpoint_id=cp.checkpoint_id,
                        name=cp.name if cp.HasField("name") else None,
                        created_at=cp.created_at,
                        mem_mode=cp.mem_mode or None,
                    )
                )
            return result
        except Exception as e:
            raise _map_checkpoint_error(e, sandbox_id=self._sandbox_id)

    async def delete(
        self,
        checkpoint_id: str,
        request_timeout: Optional[float] = None,
    ) -> bool:
        """
        Delete a checkpoint.

        :param checkpoint_id: ID of the checkpoint to delete
        :param request_timeout: Timeout for the request in **seconds**,
            defaults to 300 seconds. A delete touches no VM, but it takes the
            same per-sandbox checkpoint lock as create and restore, so it can
            queue behind one of those: the server waits up to its own
            ``CHECKPOINT_LOCK_WAIT_TIMEOUT`` (60 seconds by default) before
            giving up and answering busy. A client timeout at or below that
            expires first and turns an answer the caller could retry on into a
            bare timeout.

        :return: ``True``. As with :meth:`restore`, a failure is raised rather
            than reported in the return value, which is kept for backwards
            compatibility.
        """
        try:
            res = await self._rpc.adelete_checkpoint(
                checkpoint_pb2.DeleteCheckpointRequest(
                    checkpoint_id=checkpoint_id,
                ),
                request_timeout=self._connection_config.get_checkpoint_request_timeout(
                    request_timeout
                ),
            )
            return res.success
        except Exception as e:
            raise _map_checkpoint_error(
                e, checkpoint_id=checkpoint_id, sandbox_id=self._sandbox_id
            )