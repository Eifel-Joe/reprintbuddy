"""Probes that count open database sessions and checked-out connections."""


class CountingSessions:
    """Stands in for the sessionmaker and counts how many sessions are open at once."""

    def __init__(self, inner):
        self.inner = inner
        self.open = 0
        self.most = 0
        self.created = 0

    def __call__(self):
        session = self.inner()
        self.open += 1
        self.created += 1
        self.most = max(self.most, self.open)
        real_close = session.close
        closed = []

        def close():
            if not closed:
                closed.append(True)
                self.open -= 1
            real_close()

        session.close = close
        return session


class Connections:
    """Counts database connections that are checked out right now."""

    def __init__(self, engine):
        from sqlalchemy import event

        self.out = 0
        event.listen(engine, "checkout", lambda *a: setattr(self, "out", self.out + 1))
        event.listen(engine, "checkin", lambda *a: setattr(self, "out", self.out - 1))
