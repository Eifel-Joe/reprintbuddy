"""Every test starts with the logging of a fresh process (v0.3.15, found while merging the round).

`configure_logging()` (p166) changes the whole process: a handler on the root logger and the levels
of the root and the `reprintbuddy` loggers. In the service that is the point. In the test suite the
`serve` tests of test_cli.py run it in-process, and every later test inherited it: the `reprintbuddy`
loggers stayed at INFO, so a test that expects a DEBUG line (notify/test_notify_homeassistant.py,
test_neither_token_nor_answer_text_appear_in_the_log) failed whenever test_cli.py ran before it.

The two tests below run in file order: the first sets the log up like `serve`, the second must not
see it. conftest.py restores the state after each test.
"""

import logging

from reprintbuddy.logsetup import _HANDLER_NAME, configure_logging


def test_a_test_may_set_up_the_log_like_serve():
    configure_logging()
    assert any(h.get_name() == _HANDLER_NAME for h in logging.getLogger().handlers)
    assert logging.getLogger("reprintbuddy").level == logging.INFO


def test_the_next_test_starts_without_that_setup():
    assert not any(h.get_name() == _HANDLER_NAME for h in logging.getLogger().handlers)
    assert logging.getLogger("reprintbuddy").level == logging.NOTSET
