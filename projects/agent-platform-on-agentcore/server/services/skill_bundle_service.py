"""
Validation and S3 storage for agent skill bundles.

A skill is a directory (`SKILL.md` plus optional `scripts/`, `references/`,
`assets/`), and the AgentCore harness fetches one from an S3 prefix. The registry
cannot store the directory — its AGENT_SKILLS descriptor is two inline strings,
and AWS documents the markdown as discovery metadata only — so this service owns
the bundle and the registry record only points at it.

Uploads arrive as a single `.md` file or a `.zip`, are validated against the
AgentSkills specification before anything is written, and are published to
`s3://SKILLS_BUCKET/skills/<name>/` — the prefix the harness skill source names.
"""
import io
import logging
import mimetypes
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3
import yaml

from core.config import AWS_REGION, SKILLS_BUCKET
from models.skill import (
    DiscoveredSkill,
    CONVENTIONAL_DIRS,
    KNOWN_FRONTMATTER_KEYS,
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_ENTRIES,
    RECOMMENDED_SKILL_MD_LINES,
    SKILL_MD_FILENAME,
    InvalidSkillBundle,
    SkillBundleInfo,
    SkillFile,
    SkillSource,
    SkillStoreNotConfigured,
    frontmatter_errors,
)

logger = logging.getLogger(__name__)

# Every bundle lands under this prefix, one directory per skill name.
SKILLS_PREFIX = "skills"

# The frontmatter fence: `---` on its own line, opening the file, and a matching
# closing line. Leading whitespace and a UTF-8 BOM are tolerated because editors
# add them and the spec is about the fence, not the byte offset.
_FRONTMATTER_RE = re.compile(
    r"\A\ufeff?\s*---[ \t]*\r?\n(?P<meta>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)

# macOS archive noise. Left in, `__MACOSX/references/._REFERENCE.md` would be
# published as a real file and `.DS_Store` would count against the entry limit.
_ZIP_NOISE_PREFIXES = ("__MACOSX/", "__macosx/")
_ZIP_NOISE_NAMES = (".DS_Store", "Thumbs.db")

# S3 objects need a content type for a browser to render them; markdown and
# python are the two `mimetypes` guesses least likely to be right by default.
_EXTRA_CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".py": "text/x-python; charset=utf-8",
    ".sh": "text/x-shellscript; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}

# delete_objects takes at most 1000 keys per call.
_DELETE_BATCH = 1000


@dataclass
class StagedBundle:
    """
    A validated upload, not yet written anywhere.

    Keeps the file bytes next to the report so a caller can validate and publish
    without re-parsing the archive — and so the dry-run endpoint can throw the
    bytes away.
    """
    info: SkillBundleInfo
    files: Dict[str, bytes] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.info.valid


def _content_type(path: str) -> str:
    _, _, ext = path.rpartition(".")
    known = _EXTRA_CONTENT_TYPES.get(f".{ext.lower()}")
    if known:
        return known
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _entry_name(info: zipfile.ZipInfo) -> str:
    """
    A zip entry's path, decoded the way it was written.

    `zipfile` decodes names as cp437 unless the archive sets the UTF-8 flag
    (bit 11), so a Korean path from a non-UTF-8 zipper arrives as mojibake —
    which would then be published to S3 under a garbled key. Reversing the cp437
    round trip recovers the original bytes.
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _unsafe(path: str) -> bool:
    """
    Whether an archive path must not be extracted.

    Absolute paths and `..` segments both let an archive escape the prefix it is
    supposed to write into. Backslashes are normalised first so a Windows-style
    `..\\..\\etc` is caught too.
    """
    normalised = path.replace("\\", "/")
    if normalised.startswith("/"):
        return True
    return any(part == ".." for part in normalised.split("/"))


def _is_noise(path: str) -> bool:
    if path.startswith(_ZIP_NOISE_PREFIXES):
        return True
    return posixpath.basename(path) in _ZIP_NOISE_NAMES


def parse_frontmatter(markdown: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """
    The YAML frontmatter mapping of a SKILL.md, plus any errors reading it.

    Returns `(None, errors)` when there is nothing usable to validate, which the
    caller reports rather than raising: a missing fence is one more line in the
    report next to the other broken rules.
    """
    match = _FRONTMATTER_RE.match(markdown)
    if not match:
        return None, [
            f"{SKILL_MD_FILENAME}은 `---` 로 여는 YAML frontmatter로 시작해야 합니다."
        ]

    try:
        meta = yaml.safe_load(match.group("meta")) or {}
    except yaml.YAMLError as exc:
        return None, [f"frontmatter YAML을 읽을 수 없습니다: {exc}"]

    if not isinstance(meta, dict):
        return None, ["frontmatter는 키·값 매핑이어야 합니다."]
    return meta, []


def body_of(markdown: str) -> str:
    """The markdown after the frontmatter fence."""
    match = _FRONTMATTER_RE.match(markdown)
    return markdown[match.end():] if match else markdown


class SkillBundleService:
    def __init__(self, bucket: Optional[str] = None, region: Optional[str] = None):
        self.bucket = bucket if bucket is not None else SKILLS_BUCKET
        self.region = region or AWS_REGION
        self._s3 = None

    @property
    def s3(self):
        if self._s3 is None:
            self._s3 = boto3.client("s3", region_name=self.region)
        return self._s3

    @property
    def enabled(self) -> bool:
        return bool(self.bucket)

    def _require_configured(self) -> None:
        if not self.enabled:
            raise SkillStoreNotConfigured(
                "Skill storage is not configured on the server. Set SKILLS_BUCKET "
                "from `terraform output skills_bucket`."
            )

    # --- validation ---------------------------------------------------------

    def inspect(self, filename: str, body: bytes) -> StagedBundle:
        """
        Validate an uploaded `.md` or `.zip` against the AgentSkills spec.

        Touches no AWS. Content problems come back in `info.errors` rather than as
        exceptions, so one upload reports every broken rule at once.
        """
        lowered = (filename or "").lower()
        if len(body) > MAX_BUNDLE_BYTES:
            return StagedBundle(
                info=SkillBundleInfo(
                    total_bytes=len(body),
                    errors=[
                        f"번들이 너무 큽니다 ({len(body) // 1024} KB). 한도는 "
                        f"{MAX_BUNDLE_BYTES // (1024 * 1024)} MB입니다."
                    ],
                )
            )

        if lowered.endswith(".zip"):
            files, errors, warnings, wrapper = self._read_zip(body)
        elif lowered.endswith((".md", ".markdown")) or not lowered:
            # A single-file skill. Whatever the upload was called, it is the
            # SKILL.md — the spec names the file, not the download.
            files, errors, warnings, wrapper = {SKILL_MD_FILENAME: body}, [], [], None
        else:
            return StagedBundle(
                info=SkillBundleInfo(
                    errors=[
                        f"'{filename}' 은 지원하지 않는 형식입니다. 단일 스킬은 "
                        ".md 파일로, 폴더 구조가 있는 스킬은 .zip 으로 올려주세요."
                    ]
                )
            )

        if errors:
            return StagedBundle(info=SkillBundleInfo(errors=errors, warnings=warnings))

        return StagedBundle(
            info=self._report(files, warnings, wrapper),
            files=files,
        )

    def _read_zip(
        self, body: bytes
    ) -> Tuple[Dict[str, bytes], List[str], List[str], Optional[str]]:
        """
        Extract a zip into `{relative path: bytes}`, rooted at the skill directory.

        Fatal problems (not an archive, no SKILL.md, an unsafe path) come back as
        errors with no files, because there is nothing left to report on. The
        fourth value is the wrapper directory that was stripped, if any.
        """
        warnings: List[str] = []

        try:
            archive = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile:
            return (
                {},
                ["zip 파일을 열 수 없습니다. 손상되었거나 zip이 아닙니다."],
                warnings,
                None,
            )

        entries = archive.infolist()
        if len(entries) > MAX_BUNDLE_ENTRIES:
            return (
                {},
                [
                    f"zip에 항목이 너무 많습니다 ({len(entries)}개). 한도는 "
                    f"{MAX_BUNDLE_ENTRIES}개입니다."
                ],
                warnings,
                None,
            )

        # Guard on the declared uncompressed size before reading anything, so a
        # small archive cannot expand into memory unbounded.
        declared = sum(entry.file_size for entry in entries)
        if declared > MAX_BUNDLE_BYTES:
            return (
                {},
                [
                    f"압축을 풀면 너무 커집니다 ({declared // (1024 * 1024)} MB). "
                    f"한도는 {MAX_BUNDLE_BYTES // (1024 * 1024)} MB입니다."
                ],
                warnings,
                None,
            )

        raw: Dict[str, bytes] = {}
        errors: List[str] = []
        for entry in entries:
            name = _entry_name(entry)
            if entry.is_dir() or _is_noise(name):
                continue
            if _unsafe(name):
                errors.append(
                    f"안전하지 않은 경로가 있습니다: '{name}'. 절대 경로와 '..' 는 "
                    "허용되지 않습니다."
                )
                continue
            if _is_symlink(entry):
                errors.append(f"심볼릭 링크는 올릴 수 없습니다: '{name}'.")
                continue
            raw[name.replace("\\", "/")] = archive.read(entry)

        if errors:
            return {}, errors, warnings, None
        if not raw:
            return {}, ["zip이 비어 있습니다."], warnings, None

        files, wrapper = self._strip_wrapper_dir(raw)

        if SKILL_MD_FILENAME not in files:
            found = ", ".join(sorted(files)[:5]) or "(없음)"
            return (
                {},
                [
                    f"{SKILL_MD_FILENAME} 을 찾을 수 없습니다. 스킬 폴더의 최상위에 "
                    f"있어야 합니다. 발견된 파일: {found}"
                ],
                warnings,
                wrapper,
            )

        return files, [], warnings, wrapper

    @staticmethod
    def _strip_wrapper_dir(
        raw: Dict[str, bytes]
    ) -> Tuple[Dict[str, bytes], Optional[str]]:
        """
        Drop the single leading directory that zipping a folder adds.

        Zipping `my-skill/` from its parent yields `my-skill/SKILL.md`, which is
        the natural thing for someone to upload. Returns the stripped directory
        name so the report can compare it against the frontmatter `name`.
        """
        if SKILL_MD_FILENAME in raw:
            return raw, None

        roots = {path.split("/", 1)[0] for path in raw if "/" in path}
        bare = [path for path in raw if "/" not in path]
        if len(roots) != 1 or bare:
            return raw, None

        root = next(iter(roots))
        stripped = {path.split("/", 1)[1]: data for path, data in raw.items()}
        if SKILL_MD_FILENAME not in stripped:
            # Not a wrapper after all — a real subdirectory. Leave it alone so the
            # missing-SKILL.md error names the paths the uploader actually sees.
            return raw, None
        return stripped, root

    def _report(
        self,
        files: Dict[str, bytes],
        warnings: List[str],
        wrapper: Optional[str] = None,
    ) -> SkillBundleInfo:
        """Turn a rooted file set into the validation report."""
        errors: List[str] = []
        warnings = list(warnings)

        try:
            skill_md = files[SKILL_MD_FILENAME].decode("utf-8")
        except UnicodeDecodeError:
            return SkillBundleInfo(
                errors=[f"{SKILL_MD_FILENAME} 은 UTF-8 텍스트여야 합니다."],
                warnings=warnings,
            )

        meta, meta_errors = parse_frontmatter(skill_md)
        errors.extend(meta_errors)
        if meta is None:
            meta = {}
        else:
            errors.extend(frontmatter_errors(meta))
            unknown = sorted(set(meta) - KNOWN_FRONTMATTER_KEYS)
            if unknown:
                warnings.append(
                    "spec에 없는 frontmatter 키는 무시됩니다: " + ", ".join(unknown)
                )

        name = meta.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if wrapper:
            # The spec says `name` must match the parent directory. That is not an
            # error here because the bundle is republished under `skills/<name>/`,
            # so the directory the harness unpacks into matches regardless — but
            # the uploader should know which of the two names won.
            warnings.append(
                f"zip의 최상위 폴더 '{wrapper}' 를 벗겨내고 읽었습니다."
                if wrapper == name or not name
                else f"zip의 최상위 폴더는 '{wrapper}' 인데 frontmatter의 name은 "
                f"'{name}' 입니다. spec은 둘이 같기를 요구하므로 '{name}' 기준으로 "
                "저장합니다."
            )

        if not body_of(skill_md).strip():
            warnings.append(
                f"{SKILL_MD_FILENAME} 본문이 비어 있습니다. frontmatter만으로는 "
                "에이전트가 스킬을 어떻게 쓸지 알 수 없습니다."
            )
        line_count = skill_md.count("\n") + 1
        if line_count > RECOMMENDED_SKILL_MD_LINES:
            warnings.append(
                f"{SKILL_MD_FILENAME} 이 {line_count}줄입니다. 스킬이 활성화되면 "
                f"전문이 컨텍스트에 올라가므로 {RECOMMENDED_SKILL_MD_LINES}줄 이하로 "
                "줄이고 상세 내용은 references/ 로 옮기는 것을 권장합니다."
            )

        unconventional = sorted(
            {
                path.split("/", 1)[0]
                for path in files
                if "/" in path and path.split("/", 1)[0] not in CONVENTIONAL_DIRS
            }
        )
        if unconventional:
            warnings.append(
                "관례 밖 디렉터리입니다 (scripts/, references/, assets/ 를 권장): "
                + ", ".join(unconventional)
            )

        non_ascii = sorted(p for p in files if not p.isascii())
        if non_ascii:
            warnings.append(
                "ASCII가 아닌 파일 경로가 있습니다. 그대로 저장되지만 스킬 안에서 "
                "참조할 때 문제가 될 수 있습니다: " + ", ".join(non_ascii[:5])
            )

        listing = [
            SkillFile(path=path, size_bytes=len(data))
            for path, data in sorted(files.items())
        ]
        metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
        description = meta.get("description")

        return SkillBundleInfo(
            name=name,
            description=description.strip() if isinstance(description, str) else "",
            skill_md=skill_md,
            files=listing,
            total_bytes=sum(f.size_bytes for f in listing),
            errors=errors,
            warnings=warnings,
            license=meta.get("license") if isinstance(meta.get("license"), str) else None,
            compatibility=(
                meta.get("compatibility")
                if isinstance(meta.get("compatibility"), str)
                else None
            ),
            metadata={k: v for k, v in (metadata or {}).items() if isinstance(v, str)},
            allowed_tools=(
                meta.get("allowed-tools")
                if isinstance(meta.get("allowed-tools"), str)
                else None
            ),
        )

    # --- S3 ----------------------------------------------------------------

    def prefix_for(self, name: str) -> str:
        return f"{SKILLS_PREFIX}/{name}/"

    def uri_for(self, name: str) -> str:
        return f"s3://{self.bucket}/{self.prefix_for(name)}"

    def publish(self, staged: StagedBundle) -> SkillSource:
        """
        Write a validated bundle to `skills/<name>/`, pruning what it replaced.

        Pruning is why this lists the prefix first: replacing a bundle that used
        to have `scripts/old.py` must not leave that file behind for the harness
        to fetch alongside the new content.
        """
        self._require_configured()
        if not staged.valid:
            raise InvalidSkillBundle(
                "번들이 AgentSkills 형식을 만족하지 않습니다.", staged.info.errors
            )

        name = staged.info.name
        prefix = self.prefix_for(name)
        existing = {obj.path for obj in self._list_prefix(prefix)}

        for path, data in staged.files.items():
            self.s3.put_object(
                Bucket=self.bucket,
                Key=f"{prefix}{path}",
                Body=data,
                ContentType=_content_type(path),
            )

        stale = sorted(existing - set(staged.files))
        if stale:
            self._delete_keys([f"{prefix}{path}" for path in stale])
            logger.info("Pruned %d stale object(s) from %s", len(stale), prefix)

        return SkillSource(
            uri=self.uri_for(name),
            files=sorted(staged.files),
            total_bytes=staged.info.total_bytes,
            published_at=datetime.now(timezone.utc).isoformat(),
        )

    def list_files(self, uri: str) -> List[SkillFile]:
        """The bundle's actual contents, read from the published prefix."""
        self._require_configured()
        bucket, prefix = self._split_uri(uri)
        if bucket != self.bucket:
            # A URI from another bucket is not ours to list; the server's role has
            # no read grant there, so say so rather than surfacing an AccessDenied.
            raise ValueError(
                f"'{uri}' 는 이 서버가 관리하는 버킷({self.bucket})이 아닙니다."
            )
        return self._list_prefix(prefix)

    def delete_prefix(self, uri: str) -> int:
        """Remove a published bundle. Returns how many objects were deleted."""
        self._require_configured()
        bucket, prefix = self._split_uri(uri)
        if bucket != self.bucket or not prefix:
            return 0
        keys = [f"{prefix}{obj.path}" for obj in self._list_prefix(prefix)]
        self._delete_keys(keys)
        return len(keys)

    def _list_prefix(self, prefix: str) -> List[SkillFile]:
        """Objects under `prefix`, named relative to it."""
        files: List[SkillFile] = []
        token: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                params["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**params)
            for obj in resp.get("Contents", []):
                key = obj.get("Key", "")
                relative = key[len(prefix):]
                # A zero-length key is the prefix's own directory marker.
                if relative:
                    files.append(
                        SkillFile(path=relative, size_bytes=obj.get("Size", 0))
                    )
            token = resp.get("NextContinuationToken")
            if not token:
                break
        return sorted(files, key=lambda f: f.path)

    def list_skills(self) -> List[DiscoveredSkill]:
        """Every published skill under `skills/`, one per prefix with a SKILL.md.

        Discovery, not validation: a prefix whose SKILL.md is missing or has no
        frontmatter `name` is skipped rather than surfaced as broken. Fail-open —
        an unconfigured or unlistable bucket yields no skills, never an error, so
        the harness catalog still builds without it.
        """
        if not self.enabled:
            return []
        try:
            resp = self.s3.list_objects_v2(
                Bucket=self.bucket, Prefix=f"{SKILLS_PREFIX}/", Delimiter="/"
            )
        except Exception as exc:  # noqa: BLE001 — fail open
            logger.warning("Could not list skills in %s: %s", self.bucket, exc)
            return []

        discovered: List[DiscoveredSkill] = []
        for entry in resp.get("CommonPrefixes", []):
            prefix = entry.get("Prefix", "")
            # "skills/<name>/" -> "<name>"
            name = prefix[len(f"{SKILLS_PREFIX}/"):].rstrip("/")
            if not name:
                continue
            md = self._get_skill_md(prefix)
            if md is None:
                logger.warning("Skipping %s: no readable SKILL.md", prefix)
                continue
            frontmatter, _errors = parse_frontmatter(md.decode("utf-8", "replace"))
            if not frontmatter or not frontmatter.get("name"):
                logger.warning("Skipping %s: SKILL.md has no frontmatter name", prefix)
                continue
            discovered.append(
                DiscoveredSkill(
                    name=str(frontmatter["name"]),
                    description=str(frontmatter.get("description", "")),
                    uri=self.uri_for(name),
                )
            )
        return discovered

    def read_published_skill(self, name: str) -> Tuple[str, SkillSource]:
        """(skill_md, SkillSource) for an already-published bundle.

        Reads the existing prefix; does not re-publish. Lets a bucket-only bundle
        be registered later without uploading it again. Raises ValueError when
        the bundle has no readable SKILL.md.
        """
        self._require_configured()
        prefix = self.prefix_for(name)
        md = self._get_skill_md(prefix)
        if md is None:
            raise ValueError(f"'{name}' 스킬의 SKILL.md 를 읽을 수 없습니다.")
        entries = self._list_prefix(prefix)
        source = SkillSource(
            uri=self.uri_for(name),
            files=sorted(f.path for f in entries),
            total_bytes=sum(f.size_bytes for f in entries),
        )
        return md.decode("utf-8", "replace"), source

    def _get_skill_md(self, prefix: str) -> Optional[bytes]:
        """SKILL.md bytes under `prefix`, or None if absent/unreadable."""
        try:
            resp = self.s3.get_object(
                Bucket=self.bucket, Key=f"{prefix}{SKILL_MD_FILENAME}"
            )
            return resp["Body"].read()
        except Exception:  # noqa: BLE001 — an absent SKILL.md is "not a skill"
            return None

    def _delete_keys(self, keys: List[str]) -> None:
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = keys[start:start + _DELETE_BATCH]
            self.s3.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in batch]},
            )

    @staticmethod
    def _split_uri(uri: str) -> Tuple[str, str]:
        """`s3://bucket/prefix/` -> `("bucket", "prefix/")`."""
        if not uri.startswith("s3://"):
            raise ValueError(f"'{uri}' 는 s3:// URI가 아닙니다.")
        bucket, _, prefix = uri[len("s3://"):].partition("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        return bucket, prefix
