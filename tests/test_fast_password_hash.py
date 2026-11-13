"""Tests hash passwords with cheap argon2 parameters; the program keeps the real ones (R1).

Signing in during the first-run setup cost most of half a second per test with the production
parameters. A test about the parameters themselves asks for them with `real_password_hash`.
"""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher

from reprintbuddy import auth


def test_tests_hash_fast():
    assert auth._hasher.time_cost == 1
    assert auth.verify_password(auth.hash_password("password123"), "password123")


@pytest.mark.real_password_hash
def test_production_hasher_keeps_argon2_defaults():
    production = PasswordHasher()
    assert (auth._hasher.time_cost, auth._hasher.memory_cost, auth._hasher.parallelism) == (
        production.time_cost, production.memory_cost, production.parallelism,
    )


@pytest.mark.real_password_hash
def test_the_dummy_hash_for_unknown_users_costs_what_a_real_one_costs():
    # A sign-in with an unknown name verifies against this hash, so a miss takes as long as a hit.
    assert not auth._hasher.check_needs_rehash(auth._DUMMY_HASH)
