from typing import overload


class Overloaded:
    @overload
    def render(self, value: str) -> str: ...

    @overload
    def render(self, value: bytes) -> bytes: ...

    def render(self, value: str | bytes) -> str | bytes:
        return value
