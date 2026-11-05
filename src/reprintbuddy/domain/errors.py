"""Domain errors carry a translation key and parameters; the web layer turns them into text."""

from __future__ import annotations


class DomainError(Exception):
    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key)
        self.key = key
        self.params: dict[str, object] = params


class ValidationError(DomainError):
    """The input is not acceptable (wrong number, missing text, ...)."""


class StockLimitError(DomainError):
    """A booking would make a counter negative (Spec 6.4)."""


class NotFoundError(DomainError):
    """The article, part, set or order does not exist."""


class ConflictError(DomainError):
    """The request conflicts with the current state (taken plate, closed order, ...)."""
