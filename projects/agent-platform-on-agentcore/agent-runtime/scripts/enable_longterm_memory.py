"""Create per-agent AgentCore Memory resources with a session-summary strategy.

Idempotent: if a memory with the target name already exists it is reused, and
the summary strategy is added only when absent. Prints `name=memory_id` lines
so the redeploy step can pick up the ids. Live AWS side effects — run
deliberately, not in tests.

Usage:
    python agent-runtime/scripts/enable_longterm_memory.py --region ap-northeast-1
"""
import argparse
import sys

from bedrock_agentcore.memory import MemoryClient

# One resource per conversational runtime that opts into long-term recall.
TARGETS = ("bap_conversations_default",)
NAMESPACE_TEMPLATES = ["/summaries/{actorId}/{sessionId}"]
STRATEGY = {
    "summaryMemoryStrategy": {
        "name": "session_summary",
        # `namespaces`, not `namespaceTemplates`: older botocore/CLI models reject the
        # newer key client-side, and the service accepts both.
        "namespaces": NAMESPACE_TEMPLATES,
    }
}
EXPIRY_DAYS = 365


def _find(client: MemoryClient, name: str):
    for mem in client.list_memories():
        mem_id = mem.get("id") or ""
        # list_memories omits name; the id is `<name>-<suffix>`.
        if mem_id.startswith(f"{name}-"):
            return mem_id
    return None


def _has_summary_strategy(client: MemoryClient, memory_id: str) -> bool:
    for strat in client.get_memory_strategies(memory_id):
        # Strategy type surfaces either as `type` or the nested key.
        if "SUMMAR" in str(strat.get("type", "")).upper():
            return True
        if any("summary" in k.lower() for k in strat.keys()):
            return True
    return False


def ensure(client: MemoryClient, name: str) -> str:
    memory_id = _find(client, name)
    if memory_id is None:
        print(f"creating {name} ...", file=sys.stderr)
        created = client.create_memory_and_wait(
            name=name,
            strategies=[STRATEGY],
            event_expiry_days=EXPIRY_DAYS,
        )
        memory_id = created.get("id") or created.get("memoryId")
        return memory_id
    if not _has_summary_strategy(client, memory_id):
        print(f"adding summary strategy to {memory_id} ...", file=sys.stderr)
        client.add_summary_strategy_and_wait(
            memory_id=memory_id,
            name="session_summary",
            namespace_templates=NAMESPACE_TEMPLATES,
        )
    return memory_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="ap-northeast-1")
    args = parser.parse_args()

    client = MemoryClient(region_name=args.region)
    for name in TARGETS:
        memory_id = ensure(client, name)
        print(f"{name}={memory_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
