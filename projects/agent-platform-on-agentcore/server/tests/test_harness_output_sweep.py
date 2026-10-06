"""Collecting the files a harness turn left in its sandbox.

The diff is the heart of it: `/home` persists for the life of the session, so
every turn re-lists the same files. Without an exact "already have this" rule the
sweep re-uploads the whole directory on every turn, and each re-upload looks like
a new version in the panel.

The rule is (path, size, mtime) against the rows already stored — mtime and size
are why `source_path`/`source_mtime` live on the artifact row.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.artifact import ArtifactVersion  # noqa: E402
from services.harness_output_service import (  # noqa: E402
    HarnessOutputService,
    PlannedUpload,
    SweptFile,
    plan_uploads,
)

THREAD = "t1"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/writer-abc"


def stored(path, size, mtime, version=1, artifact_id="a1"):
    return ArtifactVersion(
        artifact_id=artifact_id,
        version=version,
        thread_id=THREAD,
        title=os.path.basename(path),
        kind="file",
        s3_key=f"artifacts/{THREAD}/{artifact_id}/v{version}.docx",
        size_bytes=size,
        created_at="2026-08-14T00:00:00",
        filename=os.path.basename(path),
        source_path=path,
        source_mtime=mtime,
    )


def key_for(thread_id, artifact_id, version, extension):
    """Stands in for ArtifactService.key_for, which owns the real format."""
    return f"artifacts/{thread_id}/{artifact_id}/v{version}.{extension}"


def test_a_new_file_becomes_version_one():
    planned = plan_uploads(THREAD, [SweptFile("/home/보고서.docx", 100, 5)], [], key_for)
    assert len(planned) == 1
    assert planned[0].version == 1
    assert planned[0].filename == "보고서.docx"
    assert planned[0].s3_key.endswith("/v1.docx")
    assert planned[0].s3_key.isascii()


def test_an_unchanged_file_is_skipped():
    known = [stored("/home/보고서.docx", 100, 5)]
    assert plan_uploads(THREAD, [SweptFile("/home/보고서.docx", 100, 5)], known, key_for) == []


def test_a_rewritten_file_becomes_the_next_version_of_the_same_artifact():
    known = [stored("/home/보고서.docx", 100, 5)]
    planned = plan_uploads(THREAD, [SweptFile("/home/보고서.docx", 250, 9)], known, key_for)
    assert len(planned) == 1
    assert planned[0].artifact_id == known[0].artifact_id
    assert planned[0].version == 2
    assert planned[0].s3_key.endswith("/v2.docx")


def test_a_size_change_alone_counts_as_a_change():
    known = [stored("/home/보고서.docx", 100, 5)]
    planned = plan_uploads(THREAD, [SweptFile("/home/보고서.docx", 101, 5)], known, key_for)
    assert planned and planned[0].version == 2


def test_the_newest_stored_version_decides_the_next_number():
    known = [
        stored("/home/보고서.docx", 100, 5, version=1),
        stored("/home/보고서.docx", 200, 7, version=2),
    ]
    planned = plan_uploads(THREAD, [SweptFile("/home/보고서.docx", 300, 9)], known, key_for)
    assert planned[0].version == 3


def test_two_files_with_different_korean_names_get_different_artifacts():
    planned = plan_uploads(
        THREAD,
        [SweptFile("/home/보고서.docx", 10, 1), SweptFile("/home/발표자료.docx", 20, 2)],
        [],
        key_for,
    )
    assert planned[0].artifact_id != planned[1].artifact_id


def test_text_artifacts_from_the_runtime_path_do_not_confuse_the_diff():
    """Rows with no source_path came from the runtime's inline artifacts."""
    known = [
        ArtifactVersion(
            artifact_id="text1",
            version=1,
            thread_id=THREAD,
            title="notes",
            kind="markdown",
            s3_key="artifacts/t1/text1/v1.md",
            size_bytes=5,
            created_at="2026-08-14T00:00:00",
        )
    ]
    planned = plan_uploads(THREAD, [SweptFile("/home/보고서.docx", 10, 1)], known, key_for)
    assert len(planned) == 1
    assert planned[0].version == 1


# --- orchestration ---------------------------------------------------------


class StubCommands:
    """Answers the listing command, then the upload command(s)."""

    def __init__(self, listing_json, upload_json=None):
        self.commands = []
        self.listing_json = listing_json
        self.upload_json = upload_json
        self.last_upload_batch = None

    def run_sync(self, harness_arn, session_id, command, timeout=60):
        import json
        import base64
        from agents.harness_command_client import CommandResult

        self.commands.append((harness_arn, session_id, command))
        if len(self.commands) == 1:
            # Listing command
            return CommandResult(stdout=self.listing_json, exit_code=0, status="COMPLETED")
        else:
            # Upload command
            if self.upload_json is not None:
                return CommandResult(stdout=self.upload_json, exit_code=0, status="COMPLETED")

            # Default: parse the upload command to generate successful responses
            # The command contains base64-encoded arguments with the uploads list
            try:
                # Extract the base64 argument from the command
                # Command format: /bin/bash -c 'echo <script_b64> | base64 -d | python3 - <args_b64>'
                parts = command.split(" - ")
                if len(parts) >= 2:
                    args_b64 = parts[-1].split("'")[0]
                    payload = json.loads(base64.b64decode(args_b64))
                    uploads = payload.get("uploads", [])
                    response = {
                        "uploads": [{"p": u["p"], "status": 200} for u in uploads]
                    }
                    self.last_upload_batch = uploads
                    return CommandResult(stdout=json.dumps(response), exit_code=0, status="COMPLETED")
            except Exception as e:
                pass

            # Fallback
            return CommandResult(stdout='{"uploads": []}', exit_code=0, status="COMPLETED")


class StubArtifacts:
    enabled = True

    def __init__(self, known=None):
        self.known = known or []
        self.registered = []
        self.presigned = []

    def list_for_thread(self, thread_id):
        return self.known

    def key_for(self, thread_id, artifact_id, version, extension):
        return f"artifacts/{thread_id}/{artifact_id}/v{version}.{extension}"

    def presign_put(self, s3_key, expires_in=300):
        self.presigned.append(s3_key)
        return f"https://s3.test/{s3_key}?sig=1"

    def register_stored(self, thread_id, **kwargs):
        record = ArtifactVersion(
            artifact_id=kwargs["artifact_id"],
            version=kwargs["version"],
            thread_id=thread_id,
            title=kwargs["filename"],
            kind="file",
            s3_key=kwargs["s3_key"],
            size_bytes=kwargs["size_bytes"],
            created_at="2026-08-14T00:00:00",
            filename=kwargs["filename"],
            source_path=kwargs["source_path"],
            source_mtime=kwargs["source_mtime"],
        )
        self.registered.append(record)
        return record


def listing_json(entries):
    import base64
    import json

    return json.dumps(
        {
            "files": [
                {
                    "p": base64.b64encode(p.encode()).decode(),
                    "s": s,
                    "m": m,
                }
                for p, s, m in entries
            ],
            "dropped": [],
        }
    )


def build(commands, artifacts):
    return HarnessOutputService(
        command_client=commands,
        artifact_service=artifacts,
        sweep_enabled=True,
        roots=["/home"],
        extensions=["docx"],
        max_file_bytes=1024,
        max_files=10,
        command_timeout=30,
    )


def test_sweep_lists_uploads_and_registers():
    commands = StubCommands(listing_json([("/home/보고서.docx", 100, 5)]))
    artifacts = StubArtifacts()
    registered = build(commands, artifacts).sweep_sync(THREAD, HARNESS_ARN)

    assert len(commands.commands) == 2                    # list, then upload
    assert artifacts.presigned == ["artifacts/t1/" + registered[0].artifact_id + "/v1.docx"]
    assert [r.filename for r in registered] == ["보고서.docx"]
    # The same deterministic session id the turn itself used.
    from agents.agentcore_client import AgentCoreClient

    assert commands.commands[0][1] == AgentCoreClient._session_id(THREAD)


def test_sweep_does_nothing_when_there_is_nothing_new():
    commands = StubCommands(listing_json([("/home/보고서.docx", 100, 5)]))
    artifacts = StubArtifacts(known=[stored("/home/보고서.docx", 100, 5)])
    assert build(commands, artifacts).sweep_sync(THREAD, HARNESS_ARN) == []
    assert len(commands.commands) == 1                    # no upload command at all


def test_a_file_that_failed_to_upload_is_not_registered():
    commands = StubCommands(
        listing_json([("/home/보고서.docx", 100, 5)]),
        upload_json='{"uploads": [{"p": "x", "error": "connection refused"}]}',
    )
    artifacts = StubArtifacts()
    assert build(commands, artifacts).sweep_sync(THREAD, HARNESS_ARN) == []
    assert artifacts.registered == []


def test_uploads_are_batched():
    entries = [(f"/home/f{i}.docx", 10, i) for i in range(12)]
    commands = StubCommands(
        listing_json(entries),
        upload_json='{"uploads": []}',   # registration is asserted elsewhere
    )
    build(commands, StubArtifacts()).sweep_sync(THREAD, HARNESS_ARN)
    from agents.harness_command_client import UPLOAD_BATCH_SIZE

    expected = 1 + -(-12 // UPLOAD_BATCH_SIZE)   # listing + ceil(12 / batch)
    assert len(commands.commands) == expected


def test_a_failed_listing_command_is_not_an_exception():
    from agents.harness_command_client import CommandResult

    class Failing:
        def run_sync(self, *args, **kwargs):
            return CommandResult(stdout="", stderr="no such file", exit_code=127, status="COMPLETED")

    assert build(Failing(), StubArtifacts()).sweep_sync(THREAD, HARNESS_ARN) == []


def test_an_access_denied_disables_further_sweeps():
    from botocore.exceptions import ClientError

    class Denied:
        def __init__(self):
            self.calls = 0

        def run_sync(self, *args, **kwargs):
            self.calls += 1
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "InvokeAgentRuntimeCommand",
            )

    denied = Denied()
    service = build(denied, StubArtifacts())
    assert service.sweep_sync(THREAD, HARNESS_ARN) == []
    assert service.sweep_sync(THREAD, HARNESS_ARN) == []
    assert denied.calls == 1, "a missing IAM grant must be reported once, not per turn"


def test_disabled_service_never_calls_the_sandbox():
    commands = StubCommands(listing_json([("/home/보고서.docx", 100, 5)]))
    service = HarnessOutputService(
        command_client=commands,
        artifact_service=StubArtifacts(),
        sweep_enabled=False,
        roots=["/home"],
        extensions=["docx"],
        max_file_bytes=1024,
        max_files=10,
        command_timeout=30,
    )
    assert not service.enabled
    assert service.sweep_sync(THREAD, HARNESS_ARN) == []
    assert commands.commands == []


@pytest.mark.asyncio
async def test_async_sweep_matches_the_sync_one():
    commands = StubCommands(listing_json([("/home/보고서.docx", 100, 5)]))
    registered = await build(commands, StubArtifacts()).sweep(THREAD, HARNESS_ARN)
    assert [r.filename for r in registered] == ["보고서.docx"]
