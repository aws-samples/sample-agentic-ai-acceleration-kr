"""Chat binding tolerates deployed-fallback ids and a registry outage, but a
DRAFT record on a working registry is still refused."""
import os
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.streaming_service import AgentNotApproved, StreamingService  # noqa: E402


class _Reg:
    def __init__(self, exc=None, status="APPROVED"):
        self._exc, self._status = exc, status

    def get_record(self, record_id):
        if self._exc:
            raise self._exc
        return SimpleNamespace(status=self._status)
    def chattable_record(self, record_id):
        # The approval gate reads the chattable revision; these doubles have one.
        return self.get_record(record_id)


def _svc(reg):
    s = StreamingService.__new__(StreamingService)
    s.registry_service = reg
    return s


def test_synthetic_deployed_id_skips_lookup():
    class Boom:
        def get_record(self, rid):
            raise AssertionError("should not be called")
        def chattable_record(self, record_id):
            # The approval gate reads the chattable revision; these doubles have one.
            return self.get_record(record_id)

    _svc(Boom())._require_approved("deployed:arn:aws:...:harness/hx", "hx")


def test_client_error_is_tolerated():
    err = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetRegistryRecord")
    _svc(_Reg(exc=err))._require_approved("rec-1", "a")


def test_other_failures_still_fail_closed():
    with pytest.raises(AgentNotApproved):
        _svc(_Reg(exc=RuntimeError("boom")))._require_approved("rec-1", "a")


def test_draft_still_blocked():
    with pytest.raises(AgentNotApproved):
        _svc(_Reg(status="DRAFT"))._require_approved("rec-1", "a")
