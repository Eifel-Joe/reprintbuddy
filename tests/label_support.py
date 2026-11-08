"""A stand-in for the network in the tests of label printing (Spec 8.5): what the printer answers to
the status question and what happens to each job. Nothing here touches a socket."""

from reprintbuddy.labels import brother, snmp

# The status block the real printer sent (QL-820NWBc, a 62 x 29 roll, ready): 32 bytes.
READY = bytes.fromhex("80204234413004000000" "3e0b" "0000030000" "1d" "00000000000000" "01" "0000000000" "00")


def block(**changes):
    """READY with single bytes changed: `block(b9=0x10)` sets byte 9 (cover open)."""
    data = bytearray(READY)
    for index, value in changes.items():
        data[int(index[1:])] = value
    return bytes(data)


class Recorder:
    """Takes the place of the network: `asked` lists the status questions, `sent` the jobs;
    `outcomes` says what each job does (None = fine, an exception = it raises)."""

    def __init__(self, monkeypatch, *, answer=READY, outcomes=()):
        self.asked, self.sent = [], []
        self.answer, self.outcomes = answer, list(outcomes)
        self.during_send = None  # a function called while a job is "on its way"
        monkeypatch.setattr(snmp, "query_status", self.ask)
        monkeypatch.setattr(brother, "send_job", self.send)

    def ask(self, host, **kwargs):
        self.asked.append((host, kwargs))
        return self.answer

    def send(self, host, data, **kwargs):
        self.sent.append((host, data, kwargs))
        if self.during_send is not None:
            self.during_send()
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
