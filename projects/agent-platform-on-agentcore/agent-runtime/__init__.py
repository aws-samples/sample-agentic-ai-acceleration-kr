"""Agent runtime package: the Strands agent deployed to AgentCore Runtime.

Deliberately import-free. Modules are imported by their own paths
(``from core.agent_manager import AgentManager``) and the entrypoint ``main.py``
sits at the package root, so a re-export layer here buys nothing — and an eager
import that rots breaks pytest collection for every test in agent-runtime/.
"""

__version__ = "1.0.0"
