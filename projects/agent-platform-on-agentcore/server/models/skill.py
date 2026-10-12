"""
Models for agent skill bundles.

A skill is a directory, not a file: `SKILL.md` at the root plus optional
`scripts/`, `references/` and `assets/`. That is the open AgentSkills
specification (https://agentskills.io/specification), which is also what the
AgentCore harness fetches from an S3 prefix.

The registry cannot hold a bundle — its AGENT_SKILLS descriptor has exactly two
inline string fields, and AWS documents the markdown as discovery metadata only.
So the bundle lives in S3 and the record indexes it; these models describe what
crosses that boundary.
"""
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

# --- AgentSkills specification limits ---------------------------------------

# 1-64 characters, lowercase alphanumerics and hyphens, no leading, trailing or
# consecutive hyphen. The pattern expresses every clause but the length, which is
# checked separately so the error can name the actual rule that was broken.
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_COMPATIBILITY_LENGTH = 500

# Frontmatter keys the spec defines. Anything else is reported as a warning: the
# spec allows clients to ignore unknown keys, so it is not an error, but a typo'd
# `descripton` would otherwise fail silently.
KNOWN_FRONTMATTER_KEYS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)

# Conventional subdirectories. Others are allowed and only warned about.
CONVENTIONAL_DIRS = frozenset({"scripts", "references", "assets"})

# The spec recommends keeping SKILL.md under 500 lines, because the whole file is
# loaded into context the moment the agent activates the skill.
RECOMMENDED_SKILL_MD_LINES = 500

SKILL_MD_FILENAME = "SKILL.md"

# --- upload limits ----------------------------------------------------------

# The harness allows 1 GB per S3 skill, but this arrives as one multipart POST
# through the load balancer and is held in memory while it is validated, so the
# accepted size is far smaller. A bundle of markdown and scripts is kilobytes;
# 25 MB leaves room for assets without inviting a 1 GB request.
MAX_BUNDLE_BYTES = 25 * 1024 * 1024

# A skill is a handful of files. A zip with thousands of entries is either the
# wrong archive or an attempt to make validation expensive.
MAX_BUNDLE_ENTRIES = 500


class InvalidSkillBundle(Exception):
    """
    An upload that cannot be published.

    `errors` carries every broken rule rather than just the first, so one attempt
    tells the uploader everything they need to fix.
    """

    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or [message]


class SkillStoreNotConfigured(Exception):
    """Raised when SKILLS_BUCKET is unset, so bundles have nowhere to go."""


class DiscoveredSkill(BaseModel):
    """A skill found by listing the skills bucket, without a registry record.

    The registry-free skill source: `uri` is the `s3://<bucket>/skills/<name>/`
    prefix a harness attaches directly.
    """
    name: str
    description: str = ""
    uri: str


class SkillFile(BaseModel):
    """One file in a bundle, at its path relative to the skill root."""
    path: str
    size_bytes: int


class SkillBundleInfo(BaseModel):
    """
    The result of validating an upload, publishable or not.

    Returned as-is by the dry-run endpoint, which is why problems are data rather
    than exceptions: the UI shows every error and warning at once, before
    anything is written.
    """
    # Frontmatter `name`, and therefore the S3 prefix and the record name. Empty
    # when the frontmatter could not be read at all.
    name: str = ""
    description: str = ""
    skill_md: str = ""
    files: List[SkillFile] = []
    total_bytes: int = 0
    # Broken specification rules. Non-empty means this cannot be published.
    errors: List[str] = []
    # Deviations that still work — an unconventional directory, an over-long
    # SKILL.md, an unknown frontmatter key.
    warnings: List[str] = []
    license: Optional[str] = None
    compatibility: Optional[str] = None
    metadata: Dict[str, str] = {}
    allowed_tools: Optional[str] = None

    @property
    def valid(self) -> bool:
        return not self.errors


class SkillSource(BaseModel):
    """
    Where a published bundle lives.

    Stored in the record's `skillDefinition._meta` under a reverse-DNS key, which
    is the extension point the 0.1.0 skill-definition schema reserves for
    vendor-specific data. The harness composer reads `uri` straight into a
    `{"s3": {"uri": …}}` skill source.
    """
    type: str = "s3"
    uri: str
    # Bundle contents at publish time. Advisory — S3 is the truth, and the detail
    # view lists the prefix rather than trusting this — but it keeps the record
    # self-describing for anyone reading the descriptor directly.
    files: List[str] = []
    total_bytes: int = 0
    published_at: Optional[str] = None


class SkillFilesResponse(BaseModel):
    """A published bundle's contents, read back from S3."""
    uri: Optional[str] = None
    files: List[SkillFile] = []
    total_bytes: int = 0


def frontmatter_errors(meta: Dict[str, Any]) -> List[str]:
    """
    Every AgentSkills rule the parsed frontmatter breaks.

    Split out of the service so the rules can be tested against a mapping
    without building an archive around it.
    """
    errors: List[str] = []

    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("frontmatter에 `name`이 필요합니다.")
    else:
        name = name.strip()
        if len(name) > MAX_NAME_LENGTH:
            errors.append(
                f"`name`은 {MAX_NAME_LENGTH}자 이하여야 합니다 (현재 {len(name)}자)."
            )
        if not SKILL_NAME_PATTERN.match(name):
            errors.append(
                f"`name: {name}` 은 소문자·숫자·하이픈만 쓸 수 있고, 하이픈으로 "
                "시작하거나 끝날 수 없으며 하이픈을 연달아 쓸 수 없습니다."
            )

    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append("frontmatter에 `description`이 필요합니다.")
    elif len(description) > MAX_DESCRIPTION_LENGTH:
        errors.append(
            f"`description`은 {MAX_DESCRIPTION_LENGTH}자 이하여야 합니다 "
            f"(현재 {len(description)}자)."
        )

    compatibility = meta.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, str):
            errors.append("`compatibility`는 문자열이어야 합니다.")
        elif len(compatibility) > MAX_COMPATIBILITY_LENGTH:
            errors.append(
                f"`compatibility`는 {MAX_COMPATIBILITY_LENGTH}자 이하여야 합니다 "
                f"(현재 {len(compatibility)}자)."
            )

    license_ = meta.get("license")
    if license_ is not None and not isinstance(license_, str):
        errors.append("`license`는 문자열이어야 합니다.")

    metadata = meta.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            errors.append("`metadata`는 문자열 키·값의 맵이어야 합니다.")
        elif any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in metadata.items()
        ):
            errors.append("`metadata`의 모든 키와 값은 문자열이어야 합니다.")

    allowed_tools = meta.get("allowed-tools")
    if allowed_tools is not None and not isinstance(allowed_tools, str):
        errors.append(
            "`allowed-tools`는 공백으로 구분한 문자열이어야 합니다 (리스트가 아닙니다)."
        )

    return errors
