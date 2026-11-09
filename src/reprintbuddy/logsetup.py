"""The log of the running service (p166).

Without a setup Python writes only WARNING and above, through `logging.lastResort`: the INFO
lines of Reprintbuddy (live channel connected, the outcome of a Home Assistant message, a failed
automatic update check) never reached the system log. `serve` therefore sets up one handler on
stderr, which systemd hands to the journal (it adds the time itself, so the line has none).

Only the `reprintbuddy` loggers write INFO. Other libraries stay at WARNING: an HTTP client logs
every request line at INFO, and a request line may carry a token (the live channel's address).
The library logger of the live channel is held at WARNING by `bambuddy.live` in any case.
Rejected: `logging.basicConfig(level=INFO)`, which would open the root logger and so every library.
"""

from __future__ import annotations

import logging
import sys

FORMAT = "%(levelname)s %(name)s: %(message)s"

_HANDLER_NAME = "reprintbuddy-stderr"


def configure_logging() -> None:
    """Send INFO and above of Reprintbuddy, WARNING and above of everything else, to stderr."""
    root = logging.getLogger()
    if not any(handler.get_name() == _HANDLER_NAME for handler in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.set_name(_HANDLER_NAME)
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.WARNING:
        root.setLevel(logging.WARNING)
    logging.getLogger("reprintbuddy").setLevel(logging.INFO)
