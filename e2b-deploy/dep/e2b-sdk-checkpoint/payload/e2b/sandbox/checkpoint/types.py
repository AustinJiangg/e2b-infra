from typing import Optional


class CheckpointInfo:
    """Metadata of one checkpoint, returned by ``create()`` and ``list()``."""

    def __init__(
        self,
        checkpoint_id: str,
        name: Optional[str] = None,
        created_at: Optional[int] = None,
        mem_mode: Optional[str] = None,
    ):
        self.checkpoint_id = checkpoint_id
        self.name = name
        self.created_at = created_at
        self.mem_mode = mem_mode
        """How the memory image was produced: ``"incremental"`` when only the
        pages dirtied since the previous checkpoint were written, ``"full"``
        when all of guest memory was copied. A ``"full"`` on anything but the
        first checkpoint of a sandbox means the host is not tracking dirty
        pages, which costs time and space without reporting an error.
        ``None`` when the server does not report the field."""

    def __repr__(self) -> str:
        return (
            f"CheckpointInfo(id={self.checkpoint_id}, name={self.name}, "
            f"mem_mode={self.mem_mode})"
        )
