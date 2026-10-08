"""Test the memory name the installer creates and looks up."""
from installer.core import probe


def test_memory_name_is_the_default_agents():
    assert probe.MEMORY_NAME == "bap_conversations_default"
