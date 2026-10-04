from typing import Protocol


class LocalStorage(Protocol):
    def close(self) -> None: ...
