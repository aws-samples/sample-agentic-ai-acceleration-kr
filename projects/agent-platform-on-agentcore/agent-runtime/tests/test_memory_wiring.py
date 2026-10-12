"""
The memory hook is attached only when a memory is configured.

Local runs and memory-less deployments must behave exactly as before, so an
unset MEMORY_ID has to mean "no hook" rather than a crash or an empty memory id.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402


def test_memory_id_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("MEMORY_ID", raising=False)
    assert Config.from_env().memory_id == ""


def test_memory_id_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MEMORY_ID", "mem-abc123")
    assert Config.from_env().memory_id == "mem-abc123"
