"""
Tests for knowledge-base ownership, documents and deletion.

Ownership is the whole point of the feature, so most of these are access tests:
someone else's private knowledge base must be indistinguishable from one that
does not exist, shared ones must be readable but not writable, and `shared` must
not be settable by a non-admin.
"""
import os
import sys

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.auth import AuthUser  # noqa: E402
from models.knowledge import (  # noqa: E402
    SOURCE_S3,
    SOURCE_UPLOAD,
    STATUS_CREATING,
    STATUS_DELETE_FAILED,
    STATUS_READY,
    CreateKnowledgeBaseRequest,
    KnowledgeBaseRecord,
    now_iso,
)
from services.knowledge_service import (  # noqa: E402
    REVIVE_AFTER_SECONDS,
    KnowledgeForbidden,
    KnowledgeNotConfigured,
    KnowledgeNotFound,
    KnowledgeService,
)

ALICE = AuthUser(sub="user-1", username="alice", groups=[])
BOB = AuthUser(sub="user-2", username="bob", groups=[])
ADMIN = AuthUser(sub="user-9", username="root", groups=["admin"])

BUCKET = "ap-knowledge-us-east-1"
PLATFORM_BUCKET = "ap-kb-source-us-east-1"


def client_error(code, operation="Call"):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeRepository:
    def __init__(self):
        self.items = {}

    def put(self, record):
        self.items[record.kb_key] = record.model_copy()
        return record

    def get(self, kb_key):
        record = self.items.get(kb_key)
        return record.model_copy() if record else None

    def update(self, kb_key, **fields):
        record = self.items[kb_key]
        updated = record.model_copy(update={**fields, "updated_at": now_iso()})
        for field, value in fields.items():
            if value == "":
                setattr(updated, field, None)
        self.items[kb_key] = updated
        return updated.model_copy()

    def visible_to(self, owner_id):
        return [
            r.model_copy() for r in self.items.values()
            if r.owner_id == owner_id or r.shared
        ]

    def delete(self, kb_key):
        self.items.pop(kb_key, None)


class FakeProvisioner:
    def __init__(self, repository):
        self.repository = repository
        self.advanced = []
        self.tore_down = []
        self.teardown_error = None
        self.agent = FakeAgent()
        self.enabled = True

    def advance(self, kb_key):
        self.advanced.append(kb_key)
        record = self.repository.get(kb_key)
        return self.repository.update(kb_key, status=STATUS_READY) if record else None

    def teardown(self, record):
        self.tore_down.append(record.kb_key)
        if self.teardown_error:
            raise self.teardown_error
        return self.repository.update(
            record.kb_key, target_id="", gateway_id="", gateway_arn="",
            data_source_id="", kb_id="",
        )


class FakeAgent:
    """The document half of bedrock-agent."""

    def __init__(self):
        self.documents = {}
        self.ingested = []
        self.deleted = []
        self.list_error = None
        # Ingestion jobs, newest last. Empty means a source that was never synced.
        self.jobs = []
        self.started_jobs = []
        self.start_error = None
        self.list_jobs_error = None

    def ingest_knowledge_base_documents(self, **kwargs):
        self.ingested.append(kwargs)
        details = []
        for document in kwargs["documents"]:
            doc_id = document["content"]["custom"]["customDocumentIdentifier"]["id"]
            self.documents[doc_id] = {
                "identifier": {"custom": {"id": doc_id}},
                "status": "IN_PROGRESS",
                "updatedAt": None,
            }
            details.append(self.documents[doc_id])
        return {"documentDetails": details}

    def list_knowledge_base_documents(self, **kwargs):
        if self.list_error:
            raise self.list_error
        return {"documentDetails": list(self.documents.values())}

    def delete_knowledge_base_documents(self, **kwargs):
        self.deleted.append(kwargs)
        for identifier in kwargs["documentIdentifiers"]:
            self.documents.pop(identifier["custom"]["id"], None)
        return {"documentDetails": []}

    def list_ingestion_jobs(self, **kwargs):
        if self.list_jobs_error:
            raise self.list_jobs_error
        # The real API sorts on request; every caller here wants newest first.
        return {"ingestionJobSummaries": list(reversed(self.jobs))}

    def start_ingestion_job(self, **kwargs):
        if self.start_error:
            raise self.start_error
        job = {
            "ingestionJobId": f"job{len(self.jobs) + 1}",
            "status": "STARTING",
            "statistics": {},
        }
        self.jobs.append(job)
        self.started_jobs.append(kwargs)
        return {"ingestionJob": job}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = (Body, ContentType)

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, **kwargs):
        keys = [k for k in self.objects if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys]} if keys else {}

    def delete_objects(self, Bucket, Delete):
        for entry in Delete["Objects"]:
            self.deleted.append(entry["Key"])
            self.objects.pop(entry["Key"], None)
        return {}


class FakeHarness:
    """The attachment check's two calls: list, then describe each result.

    ListHarnesses carries no `tools` in the real API — mirrored here by stripping
    it from the summaries — so an implementation that reads attachments off the
    listing finds none and lets every delete through unguarded.
    """

    def __init__(self, harnesses=None):
        self.harnesses = harnesses or []

    def list_harnesses(self):
        return [h.as_summary() for h in self.harnesses]

    def get_harness(self, harness_id):
        for harness in self.harnesses:
            if harness.harness_id == harness_id:
                return harness
        raise KeyError(harness_id)


class Harness:
    def __init__(self, name, gateway_arn):
        self.harness_id = f"{name}-abcdefghij"
        self.harness_name = name
        self.tools = [
            {"agentCoreGateway": {"gatewayArn": gateway_arn}}
        ] if gateway_arn else []

    def as_summary(self):
        """The same harness as ListHarnesses reports it: no tools."""
        return Harness(self.harness_name, None)


def build(harnesses=None, source_buckets=("corp-docs",),
          platform_source_bucket=PLATFORM_BUCKET):
    repository = FakeRepository()
    provisioner = FakeProvisioner(repository)
    service = KnowledgeService(
        repository=repository,
        provisioner=provisioner,
        bucket=BUCKET,
        region="us-east-1",
        s3=FakeS3(),
        harness=FakeHarness(harnesses),
        source_buckets=list(source_buckets),
        platform_source_bucket=platform_source_bucket,
    )
    return service, repository, provisioner


def seed(repository, kb_key="kb_docs_a1b2c3d4", owner=ALICE, shared=False,
         status=STATUS_READY, updated_at=None, source_type=SOURCE_UPLOAD,
         source_config=None):
    return repository.put(KnowledgeBaseRecord(
        kb_key=kb_key,
        owner_id=owner.sub,
        owner_name=owner.username,
        shared=shared,
        name="Docs",
        source_type=source_type,
        source_config=source_config or {},
        status=status,
        kb_id="KB00000001",
        data_source_id="DS00000001",
        gateway_id="gw-1",
        gateway_arn=f"arn:aws:bedrock-agentcore:us-east-1:1:gateway/{kb_key}",
        target_id="tg-1",
        created_at=now_iso(),
        updated_at=updated_at or now_iso(),
    ))


def seed_s3(repository, bucket="corp-docs", prefix="exports/", **kwargs):
    return seed(
        repository,
        source_type=SOURCE_S3,
        source_config={"bucket_name": bucket, "prefix": prefix},
        **kwargs,
    )


def seed_managed(repository, **kwargs):
    return seed(
        repository,
        source_type=SOURCE_S3,
        source_config={
            "bucket_name": "ap-kb-source-us-east-1",
            "prefix": "users/user-1/kb_docs_a1b2c3d4/",
            "managed": True,
        },
        **kwargs,
    )


# --- creation ------------------------------------------------------------


def test_create_records_the_owner_and_returns_before_provisioning():
    """Provisioning takes minutes; the POST must not wait for it."""
    service, repository, provisioner = build()

    record = service.create(ALICE, CreateKnowledgeBaseRequest(name="Product Docs"))

    assert record.owner_id == ALICE.sub
    assert record.owner_name == "alice"
    assert record.status == STATUS_CREATING
    assert record.kb_id is None
    assert provisioner.advanced == []


def test_the_record_exists_before_any_aws_call_so_nothing_is_orphaned():
    service, repository, _ = build()

    record = service.create(ALICE, CreateKnowledgeBaseRequest(name="Product Docs"))

    assert repository.get(record.kb_key) is not None


def test_a_non_admin_cannot_create_a_shared_knowledge_base():
    service, _, _ = build()

    with pytest.raises(KnowledgeForbidden):
        service.create(ALICE, CreateKnowledgeBaseRequest(name="Docs", shared=True))


def test_an_admin_can():
    service, _, _ = build()

    record = service.create(ADMIN, CreateKnowledgeBaseRequest(name="Docs", shared=True))

    assert record.shared is True


def test_an_empty_name_is_rejected():
    service, _, _ = build()

    with pytest.raises(ValueError):
        service.create(ALICE, CreateKnowledgeBaseRequest(name="   "))


# --- listing and revival -------------------------------------------------


def test_the_listing_shows_mine_and_shared_but_not_someone_elses():
    service, repository, _ = build()
    seed(repository, "kb_mine_00000001", owner=ALICE)
    seed(repository, "kb_shared_00000001", owner=BOB, shared=True)
    seed(repository, "kb_theirs_0000001", owner=BOB)

    keys = {r.kb_key for r in service.list_visible(ALICE)}

    assert keys == {"kb_mine_00000001", "kb_shared_00000001"}


def test_the_listing_can_be_narrowed_to_attachable_knowledge_bases():
    service, repository, _ = build()
    seed(repository, "kb_ready_00000001", status=STATUS_READY)
    seed(repository, "kb_building_000001", status=STATUS_CREATING)

    keys = {r.kb_key for r in service.list_visible(ALICE, status=STATUS_READY)}

    assert keys == {"kb_ready_00000001"}


def test_a_stalled_provisioning_record_is_reported_as_needing_revival():
    """
    The recovery mechanism for a container that died mid-provisioning: the UI
    polls this listing anyway, so the poll doubles as the retry trigger.
    """
    service, repository, _ = build()
    stale = "2026-08-05T00:00:00+00:00"
    seed(repository, "kb_stalled_000001", status=STATUS_CREATING, updated_at=stale)
    seed(repository, "kb_moving_0000001", status=STATUS_CREATING)
    seed(repository, "kb_done_00000001", status=STATUS_READY, updated_at=stale)

    records = service.list_visible(ALICE)

    assert service.stale_keys(records) == ["kb_stalled_000001"]


def test_a_record_with_an_unparseable_timestamp_is_revived_rather_than_stuck():
    service, repository, _ = build()
    record = seed(repository, "kb_odd_00000001", status=STATUS_CREATING)
    repository.items[record.kb_key] = record.model_copy(update={"updated_at": ""})

    assert service.stale_keys(service.list_visible(ALICE)) == ["kb_odd_00000001"]


def test_the_revive_threshold_is_two_minutes():
    assert REVIVE_AFTER_SECONDS == 120


def test_advance_never_raises_because_it_runs_detached_in_a_background_task():
    service, repository, provisioner = build()
    provisioner.advance = lambda kb_key: (_ for _ in ()).throw(RuntimeError("boom"))

    service.advance("kb_docs_a1b2c3d4")  # must not raise


# --- detail --------------------------------------------------------------


def test_detail_lists_documents_from_aws_not_from_dynamodb():
    """AWS owns document status; mirroring it would guarantee divergence."""
    service, repository, provisioner = build()
    record = seed(repository)
    provisioner.agent.documents = {
        "report.pdf_9f8e7d6c": {
            "identifier": {"custom": {"id": "report.pdf_9f8e7d6c"}},
            "status": "INDEXED",
            "updatedAt": None,
        }
    }

    detail = service.detail(ALICE, record.kb_key)

    assert [d.doc_id for d in detail.documents] == ["report.pdf_9f8e7d6c"]
    assert detail.documents[0].filename == "report.pdf"
    assert detail.documents[0].status == "INDEXED"


def test_deleted_documents_are_not_listed():
    """AWS keeps returning a deleted identifier as NOT_FOUND, apparently forever."""
    service, repository, provisioner = build()
    record = seed(repository)
    provisioner.agent.documents = {
        "kept.pdf_9f8e7d6c": {
            "identifier": {"custom": {"id": "kept.pdf_9f8e7d6c"}},
            "status": "INDEXED",
            "updatedAt": None,
        },
        "gone.pdf_1a2b3c4d": {
            "identifier": {"custom": {"id": "gone.pdf_1a2b3c4d"}},
            "status": "NOT_FOUND",
            "updatedAt": None,
        },
    }

    detail = service.detail(ALICE, record.kb_key)

    assert [d.doc_id for d in detail.documents] == ["kept.pdf_9f8e7d6c"]


def test_a_delete_still_in_flight_is_listed():
    """The user asked for it and should see it finish; only the tombstone hides."""
    service, repository, provisioner = build()
    record = seed(repository)
    provisioner.agent.documents = {
        "going.pdf_1a2b3c4d": {
            "identifier": {"custom": {"id": "going.pdf_1a2b3c4d"}},
            "status": "DELETE_IN_PROGRESS",
            "updatedAt": None,
        }
    }

    detail = service.detail(ALICE, record.kb_key)

    assert [d.status for d in detail.documents] == ["DELETE_IN_PROGRESS"]


def test_detail_of_someone_elses_private_knowledge_base_is_a_404_not_a_403():
    """403 would confirm the knowledge base exists."""
    service, repository, _ = build()
    record = seed(repository, owner=ALICE)

    with pytest.raises(KnowledgeNotFound):
        service.detail(BOB, record.kb_key)


def test_a_shared_knowledge_base_is_readable_by_anyone():
    service, repository, _ = build()
    record = seed(repository, owner=ALICE, shared=True)

    assert service.detail(BOB, record.kb_key).knowledge_base.kb_key == record.kb_key


def test_an_admin_can_read_a_private_knowledge_base():
    service, repository, _ = build()
    record = seed(repository, owner=ALICE)

    assert service.detail(ADMIN, record.kb_key).knowledge_base.kb_key == record.kb_key


def test_detail_of_a_knowledge_base_still_provisioning_has_no_documents():
    """There is no data source to query yet; the panel should still render."""
    service, repository, _ = build()
    record = repository.put(KnowledgeBaseRecord(
        kb_key="kb_new_00000001", owner_id=ALICE.sub, name="New",
        status=STATUS_CREATING, created_at=now_iso(), updated_at=now_iso(),
    ))

    detail = service.detail(ALICE, record.kb_key)

    assert detail.documents == []


def test_a_document_listing_failure_does_not_hide_the_knowledge_base():
    service, repository, provisioner = build()
    record = seed(repository)
    provisioner.agent.list_error = client_error("ThrottlingException")

    detail = service.detail(ALICE, record.kb_key)

    assert detail.knowledge_base.kb_key == record.kb_key
    assert detail.documents == []


# --- documents -----------------------------------------------------------


def test_an_upload_goes_to_s3_and_is_ingested_from_there():
    service, repository, provisioner = build()
    record = seed(repository)

    document = service.add_document(
        ALICE, record.kb_key, "Q3 report.pdf", "application/pdf", b"%PDF-1.7"
    )

    key = f"knowledge/{record.kb_key}/{document.doc_id}"
    assert service.s3.objects[key] == (b"%PDF-1.7", "application/pdf")
    content = provisioner.agent.ingested[0]["documents"][0]["content"]
    assert content["dataSourceType"] == "CUSTOM"
    assert content["custom"]["sourceType"] == "S3_LOCATION"
    assert content["custom"]["s3Location"] == {"uri": f"s3://{BUCKET}/{key}"}


def test_the_filename_is_also_sent_as_metadata_for_search_citations():
    service, repository, provisioner = build()
    record = seed(repository)

    service.add_document(ALICE, record.kb_key, "Q3 report.pdf", "text/plain", b"hi")

    metadata = provisioner.agent.ingested[0]["documents"][0]["metadata"]
    assert metadata["type"] == "IN_LINE_ATTRIBUTE"
    assert metadata["inlineAttributes"] == [
        {"key": "filename", "value": {"type": "STRING", "stringValue": "Q3 report.pdf"}}
    ]


def test_the_returned_document_carries_the_display_filename():
    service, repository, _ = build()
    record = seed(repository)

    document = service.add_document(
        ALICE, record.kb_key, "Q3 report.pdf", "application/pdf", b"%PDF"
    )

    assert document.filename == "Q3-report.pdf"


def test_an_unsupported_file_type_is_rejected_before_anything_is_stored():
    """Rejecting up front beats an opaque asynchronous ingestion failure."""
    service, repository, provisioner = build()
    record = seed(repository)

    with pytest.raises(ValueError) as caught:
        service.add_document(ALICE, record.kb_key, "clip.mp4", "video/mp4", b"\x00")

    assert "video/mp4" in str(caught.value)
    assert service.s3.objects == {}
    assert provisioner.agent.ingested == []


def test_an_empty_file_is_rejected():
    service, repository, _ = build()
    record = seed(repository)

    with pytest.raises(ValueError):
        service.add_document(ALICE, record.kb_key, "empty.txt", "text/plain", b"")


def test_uploading_to_a_knowledge_base_that_is_not_ready_is_refused():
    service, repository, _ = build()
    record = repository.put(KnowledgeBaseRecord(
        kb_key="kb_new_00000001", owner_id=ALICE.sub, name="New",
        status=STATUS_CREATING, created_at=now_iso(), updated_at=now_iso(),
    ))

    with pytest.raises(ValueError):
        service.add_document(ALICE, record.kb_key, "a.txt", "text/plain", b"hi")


def test_a_reader_of_a_shared_knowledge_base_cannot_upload_to_it():
    service, repository, _ = build()
    record = seed(repository, owner=ALICE, shared=True)

    with pytest.raises(KnowledgeForbidden):
        service.add_document(BOB, record.kb_key, "a.txt", "text/plain", b"hi")


def test_an_admin_can_upload_to_someone_elses_knowledge_base():
    service, repository, _ = build()
    record = seed(repository, owner=ALICE)

    assert service.add_document(ADMIN, record.kb_key, "a.txt", "text/plain", b"hi")


def test_deleting_a_document_removes_it_from_aws_and_from_s3():
    service, repository, provisioner = build()
    record = seed(repository)
    document = service.add_document(
        ALICE, record.kb_key, "a.txt", "text/plain", b"hi"
    )

    service.delete_document(ALICE, record.kb_key, document.doc_id)

    assert provisioner.agent.deleted[0]["documentIdentifiers"] == [
        {"dataSourceType": "CUSTOM", "custom": {"id": document.doc_id}}
    ]
    assert service.s3.deleted == [f"knowledge/{record.kb_key}/{document.doc_id}"]


def test_deleting_someone_elses_document_is_refused():
    service, repository, _ = build()
    record = seed(repository, owner=ALICE, shared=True)

    with pytest.raises(KnowledgeForbidden):
        service.delete_document(BOB, record.kb_key, "a.txt_11111111")


# --- deletion ------------------------------------------------------------


def test_delete_tears_down_aws_then_s3_then_the_record():
    service, repository, provisioner = build()
    record = seed(repository)
    service.add_document(ALICE, record.kb_key, "a.txt", "text/plain", b"hi")

    service.delete(ALICE, record.kb_key)

    assert provisioner.tore_down == [record.kb_key]
    assert service.s3.objects == {}
    assert repository.get(record.kb_key) is None


def test_a_failed_teardown_keeps_the_record_so_it_can_be_retried():
    """Deleting the record first would orphan the AWS resources untraceably."""
    service, repository, provisioner = build()
    record = seed(repository)
    provisioner.teardown_error = client_error("ConflictException")

    with pytest.raises(ClientError):
        service.delete(ALICE, record.kb_key)

    remaining = repository.get(record.kb_key)
    assert remaining is not None
    assert remaining.status == STATUS_DELETE_FAILED
    assert "ConflictException" in remaining.failure_reason


def test_deleting_a_knowledge_base_a_harness_uses_is_refused_by_default():
    """Removing the gateway silently breaks a tool the harness already lists."""
    service, repository, _ = build()
    record = seed(repository)
    service.harness.harnesses = [Harness("support_bot", record.gateway_arn)]

    with pytest.raises(ValueError) as caught:
        service.delete(ALICE, record.kb_key)

    assert "support_bot" in str(caught.value)
    assert repository.get(record.kb_key) is not None


def test_force_deletes_it_anyway():
    service, repository, provisioner = build()
    record = seed(repository)
    service.harness.harnesses = [Harness("support_bot", record.gateway_arn)]

    service.delete(ALICE, record.kb_key, force=True)

    assert repository.get(record.kb_key) is None


def test_a_harness_using_a_different_gateway_does_not_block_the_delete():
    service, repository, _ = build()
    record = seed(repository)
    service.harness.harnesses = [
        Harness("other_bot", "arn:aws:bedrock-agentcore:us-east-1:1:gateway/unrelated")
    ]

    service.delete(ALICE, record.kb_key)

    assert repository.get(record.kb_key) is None


def test_an_unreadable_harness_detail_does_not_block_the_delete():
    """One harness that cannot be described must not veto the whole delete."""
    service, repository, _ = build()
    record = seed(repository)
    service.harness.harnesses = [Harness("support_bot", record.gateway_arn)]
    service.harness.get_harness = lambda _id: (_ for _ in ()).throw(
        client_error("AccessDeniedException")
    )

    service.delete(ALICE, record.kb_key)

    assert repository.get(record.kb_key) is None


def test_an_unreadable_harness_listing_does_not_block_the_delete():
    """A best-effort warning must not become a hard dependency."""
    service, repository, _ = build()
    record = seed(repository)
    service.harness.list_harnesses = lambda: (_ for _ in ()).throw(
        client_error("AccessDeniedException")
    )

    service.delete(ALICE, record.kb_key)

    assert repository.get(record.kb_key) is None


def test_a_reader_of_a_shared_knowledge_base_cannot_delete_it():
    service, repository, _ = build()
    record = seed(repository, owner=ALICE, shared=True)

    with pytest.raises(KnowledgeForbidden):
        service.delete(BOB, record.kb_key)


def test_deleting_a_missing_knowledge_base_is_a_404():
    service, _, _ = build()

    with pytest.raises(KnowledgeNotFound):
        service.delete(ALICE, "kb_gone_00000001")


# --- source types --------------------------------------------------------


def test_a_knowledge_base_is_upload_backed_unless_asked_otherwise():
    """Every caller written before source types existed meant UPLOAD."""
    service, _repository, _provisioner = build()

    record = service.create(ALICE, CreateKnowledgeBaseRequest(name="Docs"))

    assert record.source_type == SOURCE_UPLOAD
    assert record.source_config == {}


def test_a_record_stored_without_a_source_type_reads_as_upload():
    """Records predate the field, so DynamoDB has no attribute to read."""
    stored = KnowledgeBaseRecord(
        kb_key="kb_old_a1b2c3d4", owner_id=ALICE.sub, name="Old",
        created_at=now_iso(), updated_at=now_iso(),
    ).model_dump()
    del stored["source_type"]
    del stored["source_config"]

    assert KnowledgeBaseRecord(**stored).source_type == SOURCE_UPLOAD


def test_an_s3_source_records_its_bucket_and_prefix():
    service, _repository, _provisioner = build()

    record = service.create(ADMIN, CreateKnowledgeBaseRequest(
        name="Corp", source_type=SOURCE_S3,
        source_config={"bucket_name": "corp-docs", "prefix": "exports/"},
    ))

    assert record.source_type == SOURCE_S3
    assert record.source_config == {"bucket_name": "corp-docs", "prefix": "exports/"}


def test_a_bucket_outside_the_allowlist_is_refused_before_provisioning():
    """IAM would refuse it too, but only as a CREATE_FAILED minutes later."""
    service, repository, _provisioner = build(source_buckets=["corp-docs"])

    with pytest.raises(ValueError, match="not available"):
        service.create(ADMIN, CreateKnowledgeBaseRequest(
            name="Sneaky", source_type=SOURCE_S3,
            source_config={"bucket_name": "someone-elses-bucket"},
        ))
    assert repository.items == {}


def test_an_s3_source_needs_a_bucket():
    service, _repository, _provisioner = build()

    with pytest.raises(ValueError, match="available"):
        service.create(ADMIN, CreateKnowledgeBaseRequest(
            name="Corp", source_type=SOURCE_S3,
            source_config={"prefix": "exports/"},
        ))


def test_a_users_s3_source_gets_the_platform_bucket_and_their_own_prefix():
    service, repository, _ = build()

    record = service.create(ALICE, CreateKnowledgeBaseRequest(
        name="Docs", source_type=SOURCE_S3,
    ))

    stored = repository.get(record.kb_key)
    assert stored.source_config["bucket_name"] == PLATFORM_BUCKET
    assert stored.source_config["prefix"] == f"users/{ALICE.sub}/{record.kb_key}/"
    assert stored.source_config["managed"] is True


def test_a_user_may_not_choose_the_bucket_or_prefix():
    service, _, _ = build()

    for config in ({"bucket_name": "corp-docs"}, {"prefix": "exports/"}):
        with pytest.raises(ValueError, match="서버가"):
            service.create(ALICE, CreateKnowledgeBaseRequest(
                name="Docs", source_type=SOURCE_S3, source_config=config,
            ))


def test_an_admin_with_an_empty_config_also_gets_a_managed_source():
    service, repository, _ = build()

    record = service.create(ADMIN, CreateKnowledgeBaseRequest(
        name="Docs", source_type=SOURCE_S3,
    ))

    assert repository.get(record.kb_key).source_config["managed"] is True


def test_an_admin_naming_a_bucket_gets_the_external_path_unchanged():
    service, repository, _ = build()

    record = service.create(ADMIN, CreateKnowledgeBaseRequest(
        name="Docs", source_type=SOURCE_S3,
        source_config={"bucket_name": "corp-docs", "prefix": "exports/"},
    ))

    stored = repository.get(record.kb_key)
    assert stored.source_config == {"bucket_name": "corp-docs", "prefix": "exports/"}


def test_without_a_platform_bucket_a_users_s3_create_is_a_400_not_a_501():
    service, _, _ = build(platform_source_bucket="")

    with pytest.raises(ValueError, match="플랫폼 소스 버킷"):
        service.create(ALICE, CreateKnowledgeBaseRequest(
            name="Docs", source_type=SOURCE_S3,
        ))


def test_an_unknown_source_type_is_refused():
    service, _repository, _provisioner = build()

    with pytest.raises(ValueError, match="Unknown source type"):
        service.create(ALICE, CreateKnowledgeBaseRequest(
            name="Corp", source_type="SHAREPOINT",
        ))


def test_an_upload_source_keeps_no_connector_config():
    """A stray config would resurface later as parameters nobody asked for."""
    service, _repository, _provisioner = build()

    record = service.create(ALICE, CreateKnowledgeBaseRequest(
        name="Docs", source_config={"bucket_name": "corp-docs"},
    ))

    assert record.source_config == {}


def test_uploading_to_a_pull_source_is_refused():
    """The S3 connector rejects IngestKnowledgeBaseDocuments outright."""
    service, repository, _provisioner = build()
    record = seed_s3(repository)

    with pytest.raises(ValueError, match="cannot upload files to"):
        service.add_document(ALICE, record.kb_key, "a.txt", "text/plain", b"hi")


def test_deleting_one_document_from_a_pull_source_is_refused():
    """It only unindexes: the next sync finds the original and re-indexes it."""
    service, repository, _provisioner = build()
    record = seed_s3(repository)

    with pytest.raises(ValueError, match="cannot delete individual documents"):
        service.delete_document(ALICE, record.kb_key, "s3://corp-docs/a.txt")


def test_deleting_a_managed_document_removes_the_object_and_syncs():
    service, repository, provisioner = build()
    record = seed_managed(repository)
    key = "users/user-1/kb_docs_a1b2c3d4/report.pdf"
    service.s3.objects[key] = (b"content", "application/pdf")

    service.delete_document(
        ALICE, record.kb_key, f"s3://ap-kb-source-us-east-1/{key}"
    )

    assert key not in service.s3.objects
    assert len(provisioner.agent.started_jobs) == 1
    # The index catches up via sync; DeleteKnowledgeBaseDocuments would be
    # undone by the next sync anyway.
    assert provisioner.agent.deleted == []


def test_a_doc_id_outside_the_namespace_deletes_nothing():
    service, repository, provisioner = build()
    record = seed_managed(repository)
    foreign = "users/user-2/kb_other_99999999/secret.pdf"
    service.s3.objects[foreign] = (b"content", "application/pdf")

    with pytest.raises(ValueError, match="폴더"):
        service.delete_document(
            ALICE, record.kb_key, f"s3://ap-kb-source-us-east-1/{foreign}"
        )

    assert foreign in service.s3.objects
    assert provisioner.agent.started_jobs == []


def test_the_prefix_itself_is_not_a_deletable_document():
    service, repository, _ = build()
    record = seed_managed(repository)

    with pytest.raises(ValueError, match="폴더"):
        service.delete_document(
            ALICE, record.kb_key,
            "s3://ap-kb-source-us-east-1/users/user-1/kb_docs_a1b2c3d4/",
        )


def test_deleting_from_an_external_s3_source_is_still_refused():
    service, repository, _ = build()
    record = seed_s3(repository)

    with pytest.raises(ValueError, match="delete individual documents"):
        service.delete_document(ALICE, record.kb_key, "s3://corp-docs/exports/a.pdf")


def test_uploading_to_a_managed_source_puts_the_object_and_starts_a_sync():
    service, repository, provisioner = build()
    record = seed_managed(repository)

    document = service.add_document(
        ALICE, record.kb_key, "report.pdf", "application/pdf", b"content"
    )

    key = "users/user-1/kb_docs_a1b2c3d4/report.pdf"
    assert key in service.s3.objects
    assert document.doc_id == f"s3://ap-kb-source-us-east-1/{key}"
    assert document.filename == "report.pdf"
    assert document.status is None
    # Pushed, not ingested: the S3 connector picks the file up on sync.
    assert provisioner.agent.ingested == []
    assert len(provisioner.agent.started_jobs) == 1


def test_a_korean_filename_lands_as_an_ascii_key():
    service, repository, _ = build()
    record = seed_managed(repository)

    service.add_document(ALICE, record.kb_key, "제품 문서.pdf",
                         "application/pdf", b"content")

    (key,) = service.s3.objects
    assert key.isascii()


def test_uploading_while_a_sync_runs_does_not_queue_a_second_job():
    service, repository, provisioner = build()
    record = seed_managed(repository)
    provisioner.agent.jobs.append(
        {"ingestionJobId": "job0", "status": "IN_PROGRESS", "statistics": {}}
    )

    service.add_document(ALICE, record.kb_key, "a.txt", "text/plain", b"x")

    assert provisioner.agent.started_jobs == []


def test_uploading_to_an_external_s3_source_is_still_refused():
    service, repository, _ = build()
    record = seed_s3(repository)

    with pytest.raises(ValueError, match="cannot upload"):
        service.add_document(ALICE, record.kb_key, "a.txt", "text/plain", b"x")


def test_a_managed_upload_still_validates_the_file_type():
    service, repository, _ = build()
    record = seed_managed(repository)

    with pytest.raises(ValueError, match="Unsupported file type"):
        service.add_document(ALICE, record.kb_key, "a.zip",
                             "application/zip", b"x")


def test_syncing_an_upload_source_is_refused():
    """AWS rejects StartIngestionJob on a CUSTOM data source."""
    service, repository, _provisioner = build()
    record = seed(repository)

    with pytest.raises(ValueError, match="nothing to synchronise"):
        service.start_sync(ALICE, record.kb_key)


def test_an_s3_document_is_named_after_its_object_not_its_uri():
    service, repository, provisioner = build()
    record = seed_s3(repository)
    provisioner.agent.documents = {
        "s3://corp-docs/exports/report.pdf": {
            "identifier": {"s3": {"uri": "s3://corp-docs/exports/report.pdf"}},
            "status": "INDEXED",
            "updatedAt": None,
        }
    }

    documents = service.detail(ALICE, record.kb_key).documents

    assert [d.doc_id for d in documents] == ["s3://corp-docs/exports/report.pdf"]
    assert documents[0].filename == "report.pdf"


def test_deleting_an_s3_knowledge_base_never_touches_object_storage():
    """The data was only ever borrowed; deleting it would be unrecoverable.

    Asserting on `deleted` alone would pass without the guard, because an S3-backed
    knowledge base puts nothing under its own prefix. So this fails the test if the
    delete path so much as *looks* at a bucket.
    """
    service, repository, _provisioner = build()
    record = seed_s3(repository)

    def refuse(**kwargs):
        raise AssertionError(f"delete must not reach S3, got {kwargs}")

    service.s3.list_objects_v2 = refuse
    service.s3.delete_objects = refuse

    service.delete(ALICE, record.kb_key)

    assert service.s3.deleted == []
    assert repository.items == {}


def test_deleting_an_upload_knowledge_base_still_removes_its_originals():
    """The counterpart: uploads *are* ours, and leaving them would be a leak."""
    service, repository, _provisioner = build()
    record = seed(repository)
    service.add_document(ALICE, record.kb_key, "a.txt", "text/plain", b"hi")

    service.delete(ALICE, record.kb_key)

    assert service.s3.objects == {}
    assert service.s3.deleted


def test_deleting_a_managed_knowledge_base_empties_its_folder():
    service, repository, _ = build()
    record = seed_managed(repository)
    mine = "users/user-1/kb_docs_a1b2c3d4/report.pdf"
    neighbour = "users/user-2/kb_other_99999999/keep.pdf"
    service.s3.objects[mine] = (b"a", "application/pdf")
    service.s3.objects[neighbour] = (b"b", "application/pdf")

    service.delete(ALICE, record.kb_key, force=True)

    assert mine not in service.s3.objects
    assert neighbour in service.s3.objects


def test_deleting_an_external_s3_knowledge_base_still_touches_nothing():
    service, repository, _ = build()
    record = seed_s3(repository)
    service.s3.objects["exports/theirs.pdf"] = (b"a", "application/pdf")

    service.delete(ALICE, record.kb_key, force=True)

    assert "exports/theirs.pdf" in service.s3.objects


# --- sync ----------------------------------------------------------------


def test_an_upload_knowledge_base_reports_no_sync():
    service, repository, _provisioner = build()
    record = seed(repository)

    assert service.detail(ALICE, record.kb_key).sync is None


def test_a_pull_source_that_was_never_synced_reports_no_sync():
    service, repository, _provisioner = build()
    record = seed_s3(repository)

    assert service.detail(ALICE, record.kb_key).sync is None


def test_sync_starts_a_job_and_reports_its_statistics():
    service, repository, provisioner = build()
    record = seed_s3(repository)

    job = service.start_sync(ALICE, record.kb_key)

    assert job.status == "STARTING"
    assert job.in_progress is True
    started = provisioner.agent.started_jobs[0]
    assert started["knowledgeBaseId"] == record.kb_id
    assert started["dataSourceId"] == record.data_source_id


def test_sync_returns_the_running_job_instead_of_starting_a_second():
    """One job per data source; the caller's intent is already being served."""
    service, repository, provisioner = build()
    record = seed_s3(repository)
    first = service.start_sync(ALICE, record.kb_key)

    second = service.start_sync(ALICE, record.kb_key)

    assert second.job_id == first.job_id
    assert len(provisioner.agent.started_jobs) == 1


def test_sync_starts_again_once_the_previous_job_finished():
    service, repository, provisioner = build()
    record = seed_s3(repository)
    service.start_sync(ALICE, record.kb_key)
    provisioner.agent.jobs[-1]["status"] = "COMPLETE"

    service.start_sync(ALICE, record.kb_key)

    assert len(provisioner.agent.started_jobs) == 2


def test_a_conflict_from_aws_reports_the_job_that_won_the_race():
    service, repository, provisioner = build()
    record = seed_s3(repository)
    provisioner.agent.jobs.append({
        "ingestionJobId": "job-elsewhere",
        "status": "IN_PROGRESS",
        "statistics": {},
    })
    provisioner.agent.start_error = client_error("ConflictException")

    # Pretend the pre-check missed it: the job is in-flight but reported COMPLETE
    # on the first read, so start_ingestion_job is what discovers the conflict.
    calls = {"n": 0}
    real = provisioner.agent.list_ingestion_jobs

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ingestionJobSummaries": [
                {"ingestionJobId": "job-elsewhere", "status": "COMPLETE",
                 "statistics": {}}
            ]}
        return real(**kwargs)

    provisioner.agent.list_ingestion_jobs = flaky

    job = service.start_sync(ALICE, record.kb_key)

    assert job.job_id == "job-elsewhere"


def test_sync_statistics_are_passed_through_for_display():
    service, repository, provisioner = build()
    record = seed_s3(repository)
    provisioner.agent.jobs.append({
        "ingestionJobId": "job1",
        "status": "COMPLETE",
        "statistics": {
            "numberOfDocumentsScanned": 12,
            "numberOfNewDocumentsIndexed": 3,
            "numberOfModifiedDocumentsIndexed": 1,
            "numberOfDocumentsDeleted": 2,
            "numberOfDocumentsFailed": 0,
            "numberOfDocumentsSkipped": 6,
        },
    })

    job = service.detail(ALICE, record.kb_key).sync

    assert (job.documents_scanned, job.documents_indexed) == (12, 3)
    assert (job.documents_modified, job.documents_deleted) == (1, 2)
    assert job.documents_skipped == 6
    assert job.in_progress is False


def test_a_sync_listing_failure_does_not_hide_the_knowledge_base():
    service, repository, provisioner = build()
    record = seed_s3(repository)
    provisioner.agent.list_jobs_error = client_error("ThrottlingException")

    detail = service.detail(ALICE, record.kb_key)

    assert detail.knowledge_base.kb_key == record.kb_key
    assert detail.sync is None


def test_syncing_a_shared_knowledge_base_needs_write_access():
    """A sync changes what everyone retrieves, so reading it is not enough."""
    service, repository, _provisioner = build()
    record = seed_s3(repository, owner=ALICE, shared=True)

    with pytest.raises(KnowledgeForbidden):
        service.start_sync(BOB, record.kb_key)


def test_sync_waits_until_the_knowledge_base_is_ready():
    service, repository, _provisioner = build()
    record = seed_s3(repository, status=STATUS_CREATING)

    with pytest.raises(ValueError, match="READY"):
        service.start_sync(ALICE, record.kb_key)


# --- configuration -------------------------------------------------------


def test_the_bucket_list_is_admin_only_but_availability_is_for_everyone():
    service, _, _ = build()

    assert service.source_bucket_info(ADMIN) == {
        "buckets": ["corp-docs"], "platform_available": True,
    }
    assert service.source_bucket_info(ALICE) == {
        "buckets": [], "platform_available": True,
    }


def test_availability_reflects_the_platform_bucket():
    service, _, _ = build(platform_source_bucket="")

    assert service.source_bucket_info(ALICE)["platform_available"] is False


def test_every_operation_refuses_when_storage_is_unconfigured():
    service = KnowledgeService(repository=None, provisioner=None, bucket="")

    assert service.enabled is False
    with pytest.raises(KnowledgeNotConfigured):
        service.list_visible(ALICE)
    with pytest.raises(KnowledgeNotConfigured):
        service.create(ALICE, CreateKnowledgeBaseRequest(name="Docs"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
