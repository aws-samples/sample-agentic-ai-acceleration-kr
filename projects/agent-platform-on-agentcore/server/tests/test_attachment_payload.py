"""
What reaches the runtime when a message carries attachments.

The text-only payload shape must not change: a runtime deployed before this
feature keeps working for ordinary chats, and only attachment sends need the new
one. When attachments are present the prompt becomes a content-block list, with a
text block first — Bedrock rejects a document block that has no accompanying
text.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agentcore_client import AgentCoreClient  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class StubAttachments:
    def __init__(self, blocks=None):
        self.blocks = blocks or []
        self.asked = []

    def model_blocks(self, thread_id, refs):
        self.asked.append((thread_id, refs))
        return self.blocks


def client(attachments=None):
    return AgentCoreClient(
        agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1",
        region_name="us-east-1",
        attachment_service=attachments,
    )


def text_message(text="hello"):
    return [{"id": "1", "type": "human", "content": text}]


def message_with_attachment(text="what is this?"):
    return [
        {
            "id": "1",
            "type": "human",
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "attachment",
                    "attachment_id": "att_1",
                    "filename": "shot.png",
                    "kind": "image",
                },
            ],
        }
    ]


def payload_for(messages, attachments=None, thread_id="t-1"):
    raw = client(attachments)._prepare_payload(messages, thread_id=thread_id)
    return json.loads(raw)


def test_a_text_only_message_still_sends_a_plain_string():
    """Byte-identical to before, so an un-redeployed runtime keeps working."""
    assert payload_for(text_message())["prompt"] == "hello"


def test_skip_recall_is_omitted_unless_the_server_sets_it():
    """Absent means 'recall as before': an old runtime and every continuing turn
    behave exactly as they did, so the key must not appear by default."""
    raw = client()._prepare_payload(text_message(), thread_id="t-1")
    assert "skip_recall" not in json.loads(raw)


def test_skip_recall_rides_the_payload_on_a_first_turn():
    raw = client()._prepare_payload(text_message(), thread_id="t-1", skip_recall=True)
    assert json.loads(raw)["skip_recall"] is True


def test_attachments_turn_the_prompt_into_content_blocks():
    blocks = [{"image": {"format": "png", "source": {"bytes": PNG}}}]

    payload = payload_for(message_with_attachment(), StubAttachments(blocks))

    assert isinstance(payload["prompt"], list)
    assert payload["prompt"][0] == {"text": "what is this?"}
    assert len(payload["prompt"]) == 2


def test_the_text_block_comes_first():
    """Bedrock requires text alongside a document block, and leads with it."""
    blocks = [{"document": {"format": "pdf", "name": "r", "source": {"bytes": b"%PDF"}}}]

    prompt = payload_for(message_with_attachment(), StubAttachments(blocks))["prompt"]

    assert "text" in prompt[0]


def test_an_attachment_without_typed_text_gets_synthesised_text():
    """An empty text block would be rejected; say what was attached instead."""
    blocks = [{"image": {"format": "png", "source": {"bytes": PNG}}}]

    prompt = payload_for(message_with_attachment(text=""), StubAttachments(blocks))["prompt"]

    assert prompt[0]["text"].strip() != ""


def test_the_reference_is_resolved_against_the_thread():
    attachments = StubAttachments([{"image": {"format": "png", "source": {"bytes": PNG}}}])

    payload_for(message_with_attachment(), attachments, thread_id="t-99")

    thread_id, refs = attachments.asked[0]
    assert thread_id == "t-99"
    assert refs[0]["attachment_id"] == "att_1"


def test_no_attachment_service_falls_back_to_text():
    """Storage unconfigured must not break chatting."""
    payload = payload_for(message_with_attachment(), None)

    assert payload["prompt"] == "what is this?"
