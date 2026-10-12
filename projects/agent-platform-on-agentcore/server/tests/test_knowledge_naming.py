"""
Tests for the names a knowledge base derives from a user-supplied title.

Naming is functional here, not cosmetic. The gateway name becomes the harness
tool name (harness_service._gateway_tool reads the ARN tail) and the MCP tool
name the agent sees is `<target>___Retrieve`, so a mangled name leaves the agent
unable to tell what the tool is for. GatewayName also forbids underscores and
caps at 48 characters, which the knowledge-base key itself does not.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.knowledge import (  # noqa: E402
    GATEWAY_NAME_RE,
    KnowledgeBaseRecord,
    SOURCE_S3,
    SOURCE_UPLOAD,
    client_token_for,
    connector_parameters_for,
    display_name_for,
    doc_id_for,
    document_name_for,
    gateway_name_for,
    is_managed_source,
    kb_key_for,
    managed_prefix_for,
    s3_key_for,
    source_object_name_for,
)

# bedrock-agent's Name pattern, which the knowledge base and data source use.
BEDROCK_NAME_RE = re.compile(r"^([0-9a-zA-Z][_-]?){1,100}$")

# ClientToken: 33-256 chars, alphanumeric with non-trailing hyphens.
CLIENT_TOKEN_RE = re.compile(r"^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}$")


def test_kb_key_carries_a_readable_slug_and_a_random_suffix():
    key = kb_key_for("Product Docs")

    assert key.startswith("kb_product-docs_")
    assert len(key.rsplit("_", 1)[1]) == 8


def test_two_knowledge_bases_with_the_same_title_get_different_keys():
    assert kb_key_for("Product Docs") != kb_key_for("Product Docs")


def test_kb_key_is_a_valid_bedrock_name():
    assert BEDROCK_NAME_RE.match(kb_key_for("Product Docs (2026!)"))


def test_a_title_with_no_usable_characters_still_yields_a_key():
    key = kb_key_for("!!!")

    assert BEDROCK_NAME_RE.match(key)


@pytest.mark.parametrize("title", ["제품 문서", "사내 규정 2026", "中文文档", "Привет"])
def test_a_non_ascii_title_still_yields_names_aws_accepts(title):
    """
    Korean titles are the norm here, and AWS names are ASCII only.

    `_slug` keeps whatever `str.isalnum()` accepts, so without the ASCII filter
    `제품 문서` becomes `kb_제품-문서_...` and CreateKnowledgeBase fails. The
    readable title lives on in the record's `name`, not in the identifier.
    """
    key = kb_key_for(title)

    assert BEDROCK_NAME_RE.match(key)
    assert GATEWAY_NAME_RE.match(gateway_name_for(key))


def test_an_all_non_ascii_title_falls_back_rather_than_producing_a_bare_key():
    key = kb_key_for("제품 문서")

    assert key.startswith("kb_kb_")


def test_the_ascii_part_of_a_mixed_title_is_kept():
    assert kb_key_for("사내 규정 2026").startswith("kb_2026_")


def test_gateway_name_is_readable_because_it_becomes_the_tool_name():
    """`product-docs-a1b2c3d4___Retrieve` is what the agent sees."""
    name = gateway_name_for("kb_product-docs_a1b2c3d4")

    assert name == "product-docs-a1b2c3d4"
    assert GATEWAY_NAME_RE.match(name)


def test_gateway_name_drops_underscores_which_GatewayName_forbids():
    name = gateway_name_for(kb_key_for("Q3 Financial Reports"))

    assert "_" not in name
    assert GATEWAY_NAME_RE.match(name)


def test_a_long_title_is_truncated_without_losing_the_random_suffix():
    """Truncating the tail instead would let two long titles collide."""
    key = kb_key_for("a" * 80)
    name = gateway_name_for(key)

    assert len(name) <= 48
    assert name.endswith(key.rsplit("_", 1)[1])
    assert GATEWAY_NAME_RE.match(name)


def test_doc_id_keeps_the_filename_so_the_document_list_can_show_it():
    """ListKnowledgeBaseDocuments returns no metadata — only the identifier."""
    doc_id = doc_id_for("Q3 report.pdf")

    assert doc_id.startswith("Q3-report.pdf_")
    assert display_name_for(doc_id) == "Q3-report.pdf"


def test_display_name_survives_a_filename_containing_underscores():
    doc_id = doc_id_for("q3_report_final.pdf")

    assert display_name_for(doc_id) == "q3_report_final.pdf"


def test_display_name_of_an_unexpected_identifier_is_left_alone():
    assert display_name_for("uploaded-by-hand") == "uploaded-by-hand"


def test_the_same_file_uploaded_twice_gets_distinct_ids():
    assert doc_id_for("report.pdf") != doc_id_for("report.pdf")


def test_s3_key_is_derived_from_the_ids_so_it_need_not_be_stored():
    assert (
        s3_key_for("kb_product-docs_a1b2c3d4", "report.pdf_9f8e7d6c")
        == "knowledge/kb_product-docs_a1b2c3d4/report.pdf_9f8e7d6c"
    )


def test_client_token_is_stable_per_step_so_a_retry_is_not_a_second_create():
    key = "kb_product-docs_a1b2c3d4"

    assert client_token_for(key, "knowledge-base") == client_token_for(key, "knowledge-base")
    assert client_token_for(key, "knowledge-base") != client_token_for(key, "gateway")


def test_client_token_matches_the_aws_pattern_despite_underscores_in_the_key():
    token = client_token_for("kb_product-docs_a1b2c3d4", "gateway")

    assert 33 <= len(token) <= 256
    assert CLIENT_TOKEN_RE.match(token)


# --- connector parameters ------------------------------------------------
#
# These are the field names CreateDataSource actually reads. Nothing else checks
# them: botocore models `connectorParameters` as a free-form Document, so a typo
# here is only visible as a CREATE_FAILED record minutes after the click.


def test_an_upload_source_asks_for_the_custom_connector():
    assert connector_parameters_for(SOURCE_UPLOAD, {}) == {
        "type": "CUSTOM",
        "version": "1",
        "aclEnabled": False,
    }


def test_an_s3_source_names_the_bucket_and_its_owner():
    parameters = connector_parameters_for(
        SOURCE_S3, {"bucket_name": "corp-docs"}, "123456789012"
    )

    assert parameters == {
        "type": "S3",
        "version": "1",
        "connectionConfiguration": {
            "bucketName": "corp-docs",
            "bucketOwnerAccountId": "123456789012",
        },
    }


def test_a_prefix_becomes_an_inclusion_filter():
    parameters = connector_parameters_for(
        SOURCE_S3, {"bucket_name": "corp-docs", "prefix": "exports/"}, "1"
    )

    assert parameters["filterConfiguration"] == {"inclusionPrefixes": ["exports/"]}


def test_no_prefix_means_no_filter_rather_than_an_empty_one():
    """An empty inclusion list would match nothing instead of everything."""
    parameters = connector_parameters_for(
        SOURCE_S3, {"bucket_name": "corp-docs", "prefix": ""}, "1"
    )

    assert "filterConfiguration" not in parameters


def test_an_s3_source_without_a_bucket_is_rejected():
    with pytest.raises(Exception):
        connector_parameters_for(SOURCE_S3, {})


def test_an_unknown_source_type_is_rejected():
    with pytest.raises(ValueError, match="Unknown source type"):
        connector_parameters_for("SHAREPOINT", {})


# --- document display names ----------------------------------------------


def test_an_uploaded_document_shows_the_filename_inside_its_id():
    assert document_name_for(SOURCE_UPLOAD, "report.pdf_9f8e7d6c") == "report.pdf"


def test_an_s3_document_shows_the_object_name_from_its_uri():
    assert (
        document_name_for(SOURCE_S3, "s3://corp-docs/exports/2026/report.pdf")
        == "report.pdf"
    )


def test_an_s3_key_that_looks_like_an_upload_id_keeps_its_whole_name():
    """The reason these are separate functions rather than one.

    `display_name_for` strips a trailing `_` plus 8 hex characters, which is a
    real thing to find in an S3 key and not a suffix this platform added.
    """
    assert (
        document_name_for(SOURCE_S3, "s3://corp-docs/backup_12345678")
        == "backup_12345678"
    )


# --- managed sources --------------------------------------------------------


def _record(source_type=SOURCE_S3, source_config=None):
    return KnowledgeBaseRecord(
        kb_key="kb_docs_a1b2c3d4", owner_id="user-1", owner_name="alice",
        name="Docs", source_type=source_type, source_config=source_config or {},
    )


def test_the_managed_prefix_is_the_owner_then_the_kb_key():
    assert managed_prefix_for("sub-123", "kb_docs_a1b2c3d4") == (
        "users/sub-123/kb_docs_a1b2c3d4/"
    )


def test_a_source_object_keeps_its_filename_without_a_random_suffix():
    assert source_object_name_for("report.pdf") == "report.pdf"


def test_a_source_object_name_is_ascii_even_for_a_korean_filename():
    name = source_object_name_for("제품 문서.pdf")
    assert name.isascii()
    assert name.endswith(".pdf")


def test_an_unusable_filename_falls_back_to_document():
    assert source_object_name_for("///") == "document"
    assert source_object_name_for("") == "document"


def test_a_managed_record_is_s3_with_the_managed_flag():
    assert is_managed_source(_record(source_config={"managed": True}))
    assert not is_managed_source(_record(source_config={}))
    assert not is_managed_source(
        _record(source_type=SOURCE_UPLOAD, source_config={"managed": True})
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
