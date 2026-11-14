"""
Two different threads must never share a runtime session.

The session id derived from a thread id is the AgentCore runtimeSessionId, the
Memory session, and the scope the built-in tools gateway labels a sandbox with.
The streaming route lets the client pick the thread id of a new thread, and the
derivation used to replace every character outside [A-Za-z0-9_-] with "-" and
stop there. So a caller who knew another user's thread id could create a thread
of their own whose id differed only in such a character — ownership passes, it
is a new thread — and land in the other user's runtime session and sandbox.

The derivation is now injective: an id that is already a valid session id is
used as is, and anything else carries a digest of the raw id.
"""
import hashlib
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agentcore_client import AgentCoreClient  # noqa: E402

session_id = AgentCoreClient._session_id
VALID = re.compile(r"^[A-Za-z0-9_-]{33,128}$")


def test_a_uuid_thread_keeps_its_session_id():
    """Existing threads must stay on the session their memory is stored under."""
    thread_id = str(uuid.uuid4())
    assert session_id(thread_id) == thread_id


def test_a_short_id_keeps_the_padding_it_always_had():
    digest = hashlib.sha256(b"t-1").hexdigest()
    assert session_id("t-1") == f"t-1-{digest}"


def test_an_id_that_differs_only_in_a_replaced_character_gets_another_session():
    victim = "0b9d6a52-6c1e-4f0e-9d2a-3c1b7e5f8a90"
    for attacker in (
        victim.replace("-", ".", 1),
        victim.replace("-", "/", 1),
        victim.replace("-", " ", 1),
        victim[:8] + "‐" + victim[9:],
    ):
        assert attacker != victim
        assert session_id(attacker) != session_id(victim), attacker


def test_two_ids_that_sanitize_alike_do_not_collide_with_each_other():
    a = "team.alpha/thread:0000000000000000000000001"
    b = "team-alpha-thread-0000000000000000000000001"
    assert session_id(a) != session_id(b)


def test_ids_longer_than_the_limit_do_not_collide_on_their_prefix():
    a = "a" * 128 + "x"
    b = "a" * 128 + "y"
    assert session_id(a) != session_id(b)


def test_a_thread_cannot_be_named_after_another_threads_derived_session():
    """The derived form is not a fixed point, so it cannot be claimed as an id."""
    other = "someone.elses/thread:000000000000000000000001"
    claimed = session_id(other)
    assert session_id(claimed) != claimed


def test_every_session_id_is_one_agentcore_accepts():
    for thread_id in (
        str(uuid.uuid4()), "t", "", "한글 스레드", "a" * 300, "x.y" * 40,
        "ok-" + "0" * 64, "a/b",
    ):
        assert VALID.fullmatch(session_id(thread_id)), thread_id


def test_a_trailing_newline_does_not_pass_as_a_valid_id():
    """`$` also matches before a final newline. An id that slipped through as
    is would reach a header, lose the newline there, and become the victim's."""
    victim = "0b9d6a52-6c1e-4f0e-9d2a-3c1b7e5f8a90"
    for attacker in (victim + "\n", victim + "\r\n", "\n" + victim, victim + " "):
        derived = session_id(attacker)
        assert derived != attacker, repr(attacker)
        assert derived.strip() != victim, repr(attacker)
        assert VALID.fullmatch(derived), repr(attacker)


def test_a_derived_looking_id_with_a_trailing_newline_is_not_a_fixed_point():
    claimed = session_id("someone.elses/thread:000000000000000000000001")
    assert session_id(claimed + "\n") != claimed + "\n"
    assert VALID.fullmatch(session_id(claimed + "\n"))
