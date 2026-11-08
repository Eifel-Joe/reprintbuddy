"""What can go wrong on the way to the printer: a translation key and its parameters, never a
traceback for the person (the web layer turns the key into a sentence)."""

from __future__ import annotations


class LabelError(Exception):
    key = "labels.err.generic"

    def __init__(self, **params: object) -> None:
        super().__init__(self.key)
        self.params = params


class PrinterAddressRefused(LabelError):
    """The address names this very machine (loopback, wildcard) or no printer at all."""

    key = "labels.err.address"


class PrinterUnreachable(LabelError):
    """Nobody answered at the printer's address in time (or the name does not resolve)."""

    key = "labels.err.unreachable"


class SendAborted(LabelError):
    """The connection broke while the job was being sent."""

    key = "labels.err.aborted"
