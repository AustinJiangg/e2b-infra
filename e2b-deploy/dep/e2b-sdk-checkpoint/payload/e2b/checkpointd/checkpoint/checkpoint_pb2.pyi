from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CreateCheckpointRequest(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class CreateCheckpointResponse(_message.Message):
    __slots__ = ("checkpoint_id", "mem_mode")
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    MEM_MODE_FIELD_NUMBER: _ClassVar[int]
    checkpoint_id: str
    mem_mode: str
    def __init__(self, checkpoint_id: _Optional[str] = ..., mem_mode: _Optional[str] = ...) -> None: ...

class RestoreCheckpointRequest(_message.Message):
    __slots__ = ("checkpoint_id",)
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    checkpoint_id: str
    def __init__(self, checkpoint_id: _Optional[str] = ...) -> None: ...

class RestoreCheckpointResponse(_message.Message):
    __slots__ = ("success",)
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    success: bool
    def __init__(self, success: bool = ...) -> None: ...

class ListCheckpointsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListCheckpointsResponse(_message.Message):
    __slots__ = ("checkpoints",)
    CHECKPOINTS_FIELD_NUMBER: _ClassVar[int]
    checkpoints: _containers.RepeatedCompositeFieldContainer[CheckpointInfo]
    def __init__(self, checkpoints: _Optional[_Iterable[_Union[CheckpointInfo, _Mapping]]] = ...) -> None: ...

class DeleteCheckpointRequest(_message.Message):
    __slots__ = ("checkpoint_id",)
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    checkpoint_id: str
    def __init__(self, checkpoint_id: _Optional[str] = ...) -> None: ...

class DeleteCheckpointResponse(_message.Message):
    __slots__ = ("success",)
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    success: bool
    def __init__(self, success: bool = ...) -> None: ...

class CheckpointInfo(_message.Message):
    __slots__ = ("checkpoint_id", "name", "created_at", "mem_mode")
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    MEM_MODE_FIELD_NUMBER: _ClassVar[int]
    checkpoint_id: str
    name: str
    created_at: int
    mem_mode: str
    def __init__(self, checkpoint_id: _Optional[str] = ..., name: _Optional[str] = ..., created_at: _Optional[int] = ..., mem_mode: _Optional[str] = ...) -> None: ...
