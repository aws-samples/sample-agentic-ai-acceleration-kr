"""Models for agent-produced artifacts rendered in the chat side panel."""
import mimetypes
from typing import List, Optional

from pydantic import BaseModel

# Artifact kinds the frontend panel knows how to render.
# `file` is the binary case: an office document or archive produced inside a
# harness sandbox. Unlike every other kind it has no inline body and its
# extension and content type come from the original filename, not from the kind.
ARTIFACT_KINDS = ["markdown", "code", "html", "svg", "mermaid", "csv", "json", "text", "file"]

# The file extension and content type each kind is stored with in S3. Sharing
# hands out presigned URLs, so the stored content type is what the browser sees.
KIND_EXTENSIONS = {
    "markdown": "md",
    "code": "txt",
    "html": "html",
    "svg": "svg",
    "mermaid": "mmd",
    "csv": "csv",
    "json": "json",
    "text": "txt",
    "file": "bin",
}

KIND_CONTENT_TYPES = {
    "markdown": "text/markdown; charset=utf-8",
    "code": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "svg": "image/svg+xml",
    "mermaid": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "text": "text/plain; charset=utf-8",
    "file": "application/octet-stream",
}

# Extensions for `kind: code`, keyed by the language the agent reported.
LANGUAGE_EXTENSIONS = {
    "python": "py",
    "javascript": "js",
    "typescript": "ts",
    "tsx": "tsx",
    "jsx": "jsx",
    "java": "java",
    "go": "go",
    "rust": "rs",
    "c": "c",
    "cpp": "cpp",
    "csharp": "cs",
    "ruby": "rb",
    "php": "php",
    "swift": "swift",
    "kotlin": "kt",
    "sql": "sql",
    "bash": "sh",
    "shell": "sh",
    "yaml": "yaml",
    "toml": "toml",
    "hcl": "tf",
    "dockerfile": "dockerfile",
}

# `python:3.13-slim` ships no /etc/mime.types and Python's builtin map has no
# OOXML entries — measured inside the deployed image: guess_type("a.docx") is
# (None, None). Without this table every office file is stored and served as
# application/octet-stream, which also stops a .pdf from rendering in the
# panel's iframe and makes it download instead.
EXTENSION_CONTENT_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
    "zip": "application/zip",
    "csv": "text/csv; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "json": "application/json",
    "txt": "text/plain; charset=utf-8",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "svg": "image/svg+xml",
}


def normalize_kind(kind: Optional[str]) -> str:
    """Coerce an agent-supplied kind into a renderable one."""
    value = (kind or "").strip().lower()
    return value if value in ARTIFACT_KINDS else "text"


def is_binary_kind(kind: str) -> bool:
    """Whether this kind's body is bytes rather than text.

    Callers use it instead of comparing to `"file"` so that adding another binary
    kind later does not mean auditing every read path.
    """
    return kind == "file"


def extension_for(
    kind: str, language: Optional[str] = None, filename: Optional[str] = None
) -> str:
    if kind == "code" and language:
        return LANGUAGE_EXTENSIONS.get(language.strip().lower(), "txt")
    if is_binary_kind(kind) and filename and "." in filename:
        # Lowercased and stripped of anything that cannot go in an S3 key: the
        # extension is the one part of a user-supplied filename that reaches the
        # key, and keys stay ASCII.
        extension = filename.rsplit(".", 1)[-1].lower()
        cleaned = "".join(c for c in extension if c.isascii() and c.isalnum())
        return cleaned[:12] or "bin"
    return KIND_EXTENSIONS.get(kind, "txt")


def content_type_for(kind: str, filename: Optional[str] = None) -> str:
    if is_binary_kind(kind):
        # Check the explicit table first — the deployed image has no
        # /etc/mime.types, so mimetypes.guess_type is not reliable.
        if filename and "." in filename:
            extension = filename.rsplit(".", 1)[-1].lower()
            if extension in EXTENSION_CONTENT_TYPES:
                return EXTENSION_CONTENT_TYPES[extension]
        # Fallback to mimetypes for extensions not in the table.
        guessed, _ = mimetypes.guess_type(filename or "")
        return guessed or "application/octet-stream"
    return KIND_CONTENT_TYPES.get(kind, "text/plain; charset=utf-8")


class ArtifactVersion(BaseModel):
    artifact_id: str
    version: int
    thread_id: str
    title: str
    kind: str
    language: Optional[str] = None
    s3_key: str
    size_bytes: int
    created_at: str
    tool_call_id: Optional[str] = None
    message_id: Optional[str] = None
    # Binary artifacts only. `filename` keeps the agent's original name, Korean
    # included — it never enters the S3 key, only the DDB row and the RFC 5987
    # part of Content-Disposition.
    filename: Optional[str] = None
    content_type: Optional[str] = None
    # Where the file was in the sandbox, and its mtime there. Together with
    # size_bytes this is what makes a re-sweep idempotent.
    source_path: Optional[str] = None
    source_mtime: Optional[int] = None
    preview_key: Optional[str] = None


class ArtifactDetail(BaseModel):
    """Latest version of an artifact plus the versions available for it."""
    latest: ArtifactVersion
    versions: List[int]


class ArtifactContent(BaseModel):
    artifact_id: str
    version: int
    kind: str
    language: Optional[str] = None
    title: str
    content: str


class ArtifactShareResponse(BaseModel):
    artifact_id: str
    version: int
    url: str
    expires_in: int
