from typing import Protocol, runtime_checkable

from app.schemas import ProviderResult


@runtime_checkable
class Provider(Protocol):
    name: str

    async def verify(self, email: str) -> ProviderResult: ...

    async def verify_bulk(self, emails: list[str]) -> list[ProviderResult]: ...
