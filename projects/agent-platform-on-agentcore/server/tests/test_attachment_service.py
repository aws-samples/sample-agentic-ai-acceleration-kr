"""
Attachment validation and the model-facing name.

Two things Bedrock will not forgive: a format outside its ImageFormat/
DocumentFormat enums, and a document `name` containing anything but
alphanumerics, single spaces, hyphens, parens and brackets. Both would fail deep
inside the model call, so they are rejected or normalised here.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.attachment import (  # noqa: E402
    MAX_BYTES,
    UnsupportedAttachment,
    kind_and_format,
    sanitise_model_name,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 32


def test_png_is_an_image():
    assert kind_and_format("shot.png", PNG) == ("image", "png")


def test_jpg_maps_to_the_bedrock_spelling():
    """Bedrock's enum says `jpeg`; the extension is usually `jpg`."""
    assert kind_and_format("photo.jpg", b"\xff\xd8\xff" + b"\x00" * 32) == (
        "image",
        "jpeg",
    )


def test_pdf_is_a_document():
    assert kind_and_format("report.pdf", PDF) == ("document", "pdf")


def test_markdown_is_a_document():
    assert kind_and_format("notes.md", b"# hi") == ("document", "md")


def test_an_unsupported_extension_is_rejected():
    with pytest.raises(UnsupportedAttachment) as exc:
        kind_and_format("payload.exe", b"MZ")
    assert "exe" in str(exc.value)


def test_a_png_renamed_to_txt_is_rejected():
    """Sniffed content must agree with the extension, or the model gets a lie."""
    with pytest.raises(UnsupportedAttachment):
        kind_and_format("sneaky.txt", PNG)


def test_the_size_ceiling_is_four_and_a_half_megabytes():
    assert MAX_BYTES == 4_718_592


def test_the_model_name_drops_the_extension_and_the_dot():
    """Dots are invalid in a Bedrock document name."""
    assert sanitise_model_name("Q3 report.pdf") == "Q3 report"


def test_the_model_name_strips_invalid_characters():
    assert sanitise_model_name("my_file@2024!.pdf") == "myfile2024"


def test_the_model_name_collapses_runs_of_whitespace():
    assert sanitise_model_name("too   many   spaces.txt") == "too many spaces"


def test_the_model_name_falls_back_when_nothing_survives():
    """An all-invalid name must not become the empty string."""
    assert sanitise_model_name("___.pdf") == "document"


from services.attachment_service import (  # noqa: E402
    AttachmentService,
    AttachmentsNotConfigured,
)


class StubS3:
    """Captures put_object and replays it from get_object."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None, Metadata=None):
        self.objects[Key] = {
            "Body": Body,
            "ContentType": ContentType,
            "Metadata": Metadata or {},
        }
        return {}

    def get_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            raise KeyError(Key)
        obj = self.objects[Key]

        class _Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {
            "Body": _Body(obj["Body"]),
            "ContentType": obj["ContentType"],
            "Metadata": obj["Metadata"],
        }


def service():
    svc = AttachmentService(bucket="test-bucket", region="us-east-1")
    svc._s3 = StubS3()
    return svc


def test_store_returns_the_reference_fields_the_ui_needs():
    result = service().store("t-1", "shot.png", PNG)

    assert result.kind == "image"
    assert result.filename == "shot.png"
    assert result.size_bytes == len(PNG)
    assert result.attachment_id.startswith("att_")


def test_store_keys_the_object_under_its_thread():
    svc = service()
    result = svc.store("t-1", "shot.png", PNG)

    assert f"attachments/t-1/{result.attachment_id}.png" in svc._s3.objects


def test_store_rejects_a_file_over_the_ceiling():
    with pytest.raises(UnsupportedAttachment) as exc:
        service().store("t-1", "big.png", PNG + b"\x00" * MAX_BYTES)
    assert "too large" in str(exc.value).lower()


def test_fetch_round_trips_the_bytes():
    svc = service()
    stored = svc.store("t-1", "shot.png", PNG)

    body, media_type, filename = svc.fetch("t-1", stored.attachment_id)

    assert body == PNG
    assert media_type == "image/png"
    assert filename == "shot.png"


def test_an_unconfigured_bucket_refuses_rather_than_half_working():
    svc = AttachmentService(bucket="", region="us-east-1")

    assert svc.enabled is False
    with pytest.raises(AttachmentsNotConfigured):
        svc.store("t-1", "shot.png", PNG)


def test_model_blocks_turns_an_image_reference_into_a_strands_block():
    svc = service()
    stored = svc.store("t-1", "shot.png", PNG)

    blocks = svc.model_blocks(
        "t-1",
        [{"type": "attachment", "attachment_id": stored.attachment_id,
          "filename": "shot.png", "kind": "image"}],
    )

    assert blocks == [{"image": {"format": "png", "source": {"bytes": PNG}}}]


def test_model_blocks_gives_a_document_a_sanitised_name():
    svc = service()
    stored = svc.store("t-1", "Q3 report.pdf", PDF)

    blocks = svc.model_blocks(
        "t-1",
        [{"type": "attachment", "attachment_id": stored.attachment_id,
          "filename": "Q3 report.pdf", "kind": "document"}],
    )

    assert blocks[0]["document"]["name"] == "Q3 report"
    assert blocks[0]["document"]["format"] == "pdf"
    assert blocks[0]["document"]["source"]["bytes"] == PDF


def test_model_blocks_skips_a_reference_whose_object_vanished():
    """A deleted object must not take the whole turn down."""
    blocks = service().model_blocks(
        "t-1",
        [{"type": "attachment", "attachment_id": "att_gone",
          "filename": "gone.png", "kind": "image"}],
    )

    assert blocks == []


def test_a_non_ascii_filename_survives_the_round_trip():
    """S3 metadata is ASCII-only.

    botocore rejects the whole put_object with a ParamValidationError if a value
    holds non-ASCII, so an un-encoded filename made "보고서.pdf" a 500 rather than
    an upload. Percent-encoding stores it; the fetch decodes it back.
    """
    svc = service()
    stored = svc.store("t-1", "보고서.pdf", PDF)

    key = f"attachments/t-1/{stored.attachment_id}.pdf"
    assert svc._s3.objects[key]["Metadata"]["filename"].isascii()

    _, _, filename = svc.fetch("t-1", stored.attachment_id)
    assert filename == "보고서.pdf"
