"""Attachment formats, limits, and the model-facing name.

The format tables are exactly Bedrock's `ImageFormat` and `DocumentFormat` enums.
Anything outside them is rejected here rather than surfacing as an opaque
validation error from the model call.
"""
import re
from typing import Dict, Tuple

from pydantic import BaseModel

# extension -> Bedrock format name. `jpg` and `jpeg` both map to `jpeg`, and
# `htm` to `html`, because the enum spells only one of each.
IMAGE_FORMATS: Dict[str, str] = {
    "png": "png",
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "gif": "gif",
    "webp": "webp",
}

DOCUMENT_FORMATS: Dict[str, str] = {
    "pdf": "pdf",
    "csv": "csv",
    "doc": "doc",
    "docx": "docx",
    "xls": "xls",
    "xlsx": "xlsx",
    "html": "html",
    "htm": "html",
    "txt": "txt",
    "md": "md",
}

# 4.5 MB. Bedrock accepts more, but a chat composer that lets someone attach a
# 50 MB scan just moves the failure later.
MAX_BYTES = 4_718_592
MAX_FILES_PER_MESSAGE = 5

# Leading bytes that identify a binary format. Used to catch a mismatch between
# the extension and the actual content, not to guess an unknown format.
_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpeg",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"%PDF": "pdf",
}

# Bedrock's document `name`: alphanumerics, single spaces, hyphens, parens,
# square brackets. No dots, no underscores.
_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9 \-()\[\]]")


class UnsupportedAttachment(Exception):
    """Raised for a format Bedrock cannot accept, or a content/extension mismatch."""


class Attachment(BaseModel):
    attachment_id: str
    filename: str
    media_type: str
    size_bytes: int
    kind: str  # "image" | "document"


def _sniff(body: bytes) -> str:
    for magic, fmt in _MAGIC.items():
        if body.startswith(magic):
            return fmt
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "webp"
    return ""


def kind_and_format(filename: str, body: bytes) -> Tuple[str, str]:
    """Classify an upload, or raise UnsupportedAttachment.

    The extension decides the format; sniffed content only has to not contradict
    it. Text formats have no magic bytes, so absence of a signature is fine —
    a signature that names a *different* format is not.
    """
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if extension in IMAGE_FORMATS:
        kind, fmt = "image", IMAGE_FORMATS[extension]
    elif extension in DOCUMENT_FORMATS:
        kind, fmt = "document", DOCUMENT_FORMATS[extension]
    else:
        raise UnsupportedAttachment(
            f"Unsupported file type '{extension or filename}'. "
            f"Allowed: {', '.join(sorted(set(IMAGE_FORMATS) | set(DOCUMENT_FORMATS)))}."
        )

    sniffed = _sniff(body)
    if sniffed and sniffed != fmt:
        raise UnsupportedAttachment(
            f"'{filename}' looks like a {sniffed} file, not {fmt}."
        )

    return kind, fmt


def sanitise_model_name(filename: str) -> str:
    """A document name Bedrock will accept.

    Also a prompt-injection surface — AWS recommends a neutral name — so the
    original filename is kept for display and only this reaches the model.
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    cleaned = _NAME_ALLOWED.sub("", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "document"
