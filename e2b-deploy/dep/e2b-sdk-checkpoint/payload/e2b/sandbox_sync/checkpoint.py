from typing import List, Optional

import httpcore
import httpx
from e2b.connection_config import ConnectionConfig
from e2b.envd.rpc import handle_rpc_exception
from e2b.checkpointd.api import CHECKPOINTD_HEALTH_ROUTE, handle_checkpointd_exception
from e2b.checkpointd.checkpoint import checkpoint_connect, checkpoint_pb2
from e2b.sandbox.checkpoint.types import CheckpointInfo


class Checkpoint:
    """
    Module for checkpointing and restoring sandbox state.

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
        pool: httpcore.ConnectionPool,
        transport: httpx.BaseTransport,
    ) -> None:
        self._connection_config = connection_config
        self._rpc = checkpoint_connect.CheckpointClient(
            checkpointd_api_url,
            pool=pool,
            json=True,
            headers=connection_config.checkpointd_headers,
        )
        self._health_api = httpx.Client(
            base_url=checkpointd_api_url,
            transport=transport,
            headers=connection_config.checkpointd_headers,
            verify=connection_config.verify_ssl,
        )

    def is_running(self, request_timeout: Optional[float] = None) -> bool:
        """
        Check whether the checkpoint API answers for this sandbox.

        Kept under this name for backwards compatibility; :meth:`is_available`
        is the same call under a name that matches what it does. It is a
        liveness probe on the host-side service, not on anything in the guest,
        so a live sandbox answers it whether or not any daemon runs inside.

        :param request_timeout: Timeout for the request in **seconds**

        :return: ``True`` if the checkpoint API answers, ``False`` otherwise
        """
        try:
            r = self._health_api.get(
                CHECKPOINTD_HEALTH_ROUTE,
                timeout=self._connection_config.get_request_timeout(request_timeout),
            )

            if r.status_code == 502:
                return False

            err = handle_checkpointd_exception(r)

            if err:
                raise err

        except httpx.TimeoutException:
            raise

        return True

    def is_available(self, request_timeout: Optional[float] = None) -> bool:
        """
        Whether the checkpoint API answers for this sandbox. Same call as
        :meth:`is_running`, under a name that says what it checks.
        """
        return self.is_running(request_timeout)

    def create(
        self,
        name: Optional[str] = None,
        request_timeout: Optional[float] = None,
    ) -> CheckpointInfo:
        """
        Create a checkpoint of the sandbox's current state.

        :param name: Optional name for the checkpoint
        :param request_timeout: Timeout for the request in **seconds**

        :return: CheckpointInfo with the checkpoint ID and metadata
        """
        try:
            req = checkpoint_pb2.CreateCheckpointRequest()
            if name is not None:
                req.name = name

            res = self._rpc.create_checkpoint(
                req,
                request_timeout=self._connection_config.get_request_timeout(
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
            raise handle_rpc_exception(e)

    def restore(
        self,
        checkpoint_id: str,
        request_timeout: Optional[float] = None,
    ) -> bool:
        """
        Restore the sandbox to a previously created checkpoint.

        :param checkpoint_id: ID of the checkpoint to restore
        :param request_timeout: Timeout for the request in **seconds**

        :return: True if the restore was successful
        """
        try:
            res = self._rpc.restore_checkpoint(
                checkpoint_pb2.RestoreCheckpointRequest(
                    checkpoint_id=checkpoint_id,
                ),
                request_timeout=self._connection_config.get_request_timeout(
                    request_timeout
                ),
            )
            return res.success
        except Exception as e:
            raise handle_rpc_exception(e)

    def list(
        self,
        request_timeout: Optional[float] = None,
    ) -> List[CheckpointInfo]:
        """
        List all checkpoints in the sandbox.

        :param request_timeout: Timeout for the request in **seconds**

        :return: List of CheckpointInfo objects
        """
        try:
            res = self._rpc.list_checkpoints(
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
            raise handle_rpc_exception(e)

    def delete(
        self,
        checkpoint_id: str,
        request_timeout: Optional[float] = None,
    ) -> bool:
        """
        Delete a checkpoint.

        :param checkpoint_id: ID of the checkpoint to delete
        :param request_timeout: Timeout for the request in **seconds**

        :return: True if the deletion was successful
        """
        try:
            res = self._rpc.delete_checkpoint(
                checkpoint_pb2.DeleteCheckpointRequest(
                    checkpoint_id=checkpoint_id,
                ),
                request_timeout=self._connection_config.get_request_timeout(
                    request_timeout
                ),
            )
            return res.success
        except Exception as e:
            raise handle_rpc_exception(e)