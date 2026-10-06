"""
Validation of uploaded skill bundles against the AgentSkills specification.

The rules come from https://agentskills.io/specification, which is what both the
AgentCore harness and this platform treat as the format of a skill. Two classes of
check live here and are deliberately kept apart:

- **Spec rules** are reported, not raised. An upload should tell the user every
  broken rule at once, so a bad `name` and a missing `description` come back
  together rather than one 400 at a time.
- **Archive safety** is fatal. A path escaping the prefix or a symlink is not a
  formatting mistake, and validation stops rather than reporting alongside it.
"""
import io
import os
import stat
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.skill import (  # noqa: E402
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_ENTRIES,
    frontmatter_errors,
)
from services.skill_bundle_service import (  # noqa: E402
    SkillBundleService,
    _entry_name,
    body_of,
    parse_frontmatter,
)

SERVICE = SkillBundleService(bucket="ap-skills", region="us-east-1")

GOOD_MD = """---
name: pdf-processing
description: Extract text and tables from PDFs. Use when handling PDF documents.
---

# PDF processing

Run `scripts/extract.py`.
"""


def skill_md(**overrides):
    """A SKILL.md with the given frontmatter, one key per line."""
    lines = {
        "name": "pdf-processing",
        "description": "Extract text from PDFs. Use when handling PDFs.",
    }
    lines.update(overrides)
    body = "\n".join(f"{k}: {v}" for k, v in lines.items() if v is not None)
    return f"---\n{body}\n---\n\n# Skill\n\nDo the thing.\n"


def zipped(files, flag_utf8=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buf.getvalue()


# --- frontmatter rules ------------------------------------------------------


def test_a_minimal_valid_skill_md_passes():
    info = SERVICE.inspect("a.md", GOOD_MD.encode()).info

    assert info.errors == []
    assert info.name == "pdf-processing"
    assert info.description.startswith("Extract text and tables")
    assert [f.path for f in info.files] == ["SKILL.md"]


@pytest.mark.parametrize(
    "name, why",
    [
        ("PDF-Processing", "uppercase"),
        ("-pdf", "leading hyphen"),
        ("pdf-", "trailing hyphen"),
        ("pdf--processing", "consecutive hyphens"),
        ("pdf_processing", "underscore"),
        ("pdf processing", "space"),
        ("a" * 65, "over 64 characters"),
    ],
)
def test_names_the_spec_rejects_are_errors(name, why):
    info = SERVICE.inspect("a.md", skill_md(name=name).encode()).info

    assert info.errors, f"expected {why} to be rejected"
    assert not info.valid


@pytest.mark.parametrize("name", ["pdf-processing", "a", "a1", "data-analysis-2"])
def test_names_the_spec_allows_are_accepted(name):
    assert SERVICE.inspect("a.md", skill_md(name=name).encode()).info.errors == []


def test_a_missing_description_is_an_error():
    info = SERVICE.inspect("a.md", skill_md(description=None).encode()).info

    assert any("description" in e for e in info.errors)


def test_an_over_long_description_is_an_error():
    info = SERVICE.inspect("a.md", skill_md(description="x" * 1025).encode()).info

    assert any("1024" in e for e in info.errors)
    # One character shorter is fine, so the boundary is where the spec puts it.
    assert SERVICE.inspect("a.md", skill_md(description="x" * 1024).encode()).info.errors == []


def test_an_over_long_compatibility_is_an_error():
    info = SERVICE.inspect("a.md", skill_md(compatibility="x" * 501).encode()).info

    assert any("500" in e for e in info.errors)


def test_every_broken_rule_is_reported_at_once():
    """One upload, one complete list — not a fix-and-resubmit loop."""
    info = SERVICE.inspect("a.md", b"---\nname: Bad_Name\n---\n").info

    assert len(info.errors) == 2
    assert any("name" in e for e in info.errors)
    assert any("description" in e for e in info.errors)


def test_a_list_valued_allowed_tools_is_an_error():
    """The spec says space-separated string; a YAML list silently means nothing."""
    info = SERVICE.inspect(
        "a.md", skill_md(**{"allowed-tools": "[Read, Bash]"}).encode()
    ).info

    assert any("allowed-tools" in e for e in info.errors)


def test_non_string_metadata_values_are_an_error():
    markdown = (
        "---\nname: a-skill\ndescription: d\nmetadata:\n  version: 1.0\n---\n\nbody\n"
    )
    info = SERVICE.inspect("a.md", markdown.encode()).info

    assert any("metadata" in e for e in info.errors)


def test_a_missing_frontmatter_fence_is_an_error():
    info = SERVICE.inspect("a.md", b"# Just markdown\n").info

    assert any("frontmatter" in e for e in info.errors)
    assert info.name == ""


def test_unparseable_yaml_is_reported_rather_than_raised():
    info = SERVICE.inspect("a.md", b"---\nname: [unclosed\n---\n\nbody\n").info

    assert info.errors
    assert not info.valid


def test_optional_fields_are_carried_through():
    markdown = (
        "---\nname: a-skill\ndescription: d\nlicense: Apache-2.0\n"
        "compatibility: Requires python 3.12\nmetadata:\n  author: acme\n"
        "allowed-tools: Read Bash\n---\n\nbody\n"
    )
    info = SERVICE.inspect("a.md", markdown.encode()).info

    assert info.errors == []
    assert info.license == "Apache-2.0"
    assert info.compatibility == "Requires python 3.12"
    assert info.metadata == {"author": "acme"}
    assert info.allowed_tools == "Read Bash"


def test_an_unknown_frontmatter_key_warns_without_failing():
    """A typo'd key is ignored by the spec, so silence would hide it."""
    info = SERVICE.inspect("a.md", skill_md(descripton="oops").encode()).info

    assert info.errors == []
    assert any("descripton" in w for w in info.warnings)


def test_an_empty_body_warns():
    info = SERVICE.inspect("a.md", b"---\nname: a-skill\ndescription: d\n---\n").info

    assert info.errors == []
    assert any("본문" in w for w in info.warnings)


def test_an_over_long_skill_md_warns():
    markdown = skill_md() + "\n".join(f"line {i}" for i in range(600))
    info = SERVICE.inspect("a.md", markdown.encode()).info

    assert info.errors == []
    assert any("references/" in w for w in info.warnings)


def test_the_frontmatter_fence_survives_a_bom_and_crlf():
    markdown = "﻿---\r\nname: a-skill\r\ndescription: d\r\n---\r\n\r\nbody\r\n"
    info = SERVICE.inspect("a.md", markdown.encode("utf-8")).info

    assert info.errors == []
    assert info.name == "a-skill"


def test_frontmatter_helpers_work_on_a_mapping_alone():
    """The rules are usable without an archive around them."""
    assert frontmatter_errors({"name": "ok-name", "description": "d"}) == []
    assert frontmatter_errors({}) == [
        "frontmatter에 `name`이 필요합니다.",
        "frontmatter에 `description`이 필요합니다.",
    ]

    meta, errors = parse_frontmatter(GOOD_MD)
    assert errors == []
    assert meta["name"] == "pdf-processing"
    assert body_of(GOOD_MD).strip().startswith("# PDF processing")


# --- upload shapes ----------------------------------------------------------


def test_a_single_md_upload_becomes_skill_md_whatever_it_was_called():
    """The spec names the file; the download name is the user's business."""
    staged = SERVICE.inspect("My Notes (final).md", GOOD_MD.encode())

    assert list(staged.files) == ["SKILL.md"]


def test_an_unsupported_extension_is_rejected():
    info = SERVICE.inspect("skill.tar.gz", b"x").info

    assert any(".zip" in e for e in info.errors)


def test_a_zip_with_skill_md_at_the_root_is_read_as_is():
    staged = SERVICE.inspect(
        "b.zip",
        zipped(
            {
                "SKILL.md": GOOD_MD,
                "references/REFERENCE.md": "# ref",
                "scripts/extract.py": "print(1)",
                "assets/template.txt": "t",
            }
        ),
    )

    assert staged.info.errors == []
    assert sorted(staged.files) == [
        "SKILL.md",
        "assets/template.txt",
        "references/REFERENCE.md",
        "scripts/extract.py",
    ]
    # No wrapper was stripped, so nothing to say about directory names.
    assert not any("폴더" in w for w in staged.info.warnings)


def test_a_single_wrapper_directory_is_stripped():
    """Zipping a skill folder from its parent is the natural thing to upload."""
    staged = SERVICE.inspect(
        "b.zip",
        zipped({"pdf-processing/SKILL.md": GOOD_MD, "pdf-processing/scripts/x.py": "1"}),
    )

    assert staged.info.errors == []
    assert sorted(staged.files) == ["SKILL.md", "scripts/x.py"]


def test_a_wrapper_name_that_disagrees_with_the_frontmatter_only_warns():
    """
    The spec wants `name` to match the parent directory, but the bundle is
    republished under `skills/<name>/` — so the directory the harness unpacks
    matches regardless, and rejecting the upload would be pedantry.
    """
    staged = SERVICE.inspect(
        "b.zip", zipped({"my-skill-v2/SKILL.md": GOOD_MD, "my-skill-v2/r/a.md": "a"})
    )

    assert staged.info.errors == []
    assert staged.info.name == "pdf-processing"
    assert any("my-skill-v2" in w and "pdf-processing" in w for w in staged.info.warnings)


def test_macos_archive_noise_is_dropped():
    staged = SERVICE.inspect(
        "b.zip",
        zipped(
            {
                "SKILL.md": GOOD_MD,
                "__MACOSX/._SKILL.md": "junk",
                ".DS_Store": "junk",
                "references/.DS_Store": "junk",
            }
        ),
    )

    assert sorted(staged.files) == ["SKILL.md"]


def test_a_zip_without_skill_md_names_the_paths_the_uploader_sees():
    info = SERVICE.inspect("b.zip", zipped({"docs/readme.md": "# hi"})).info

    assert any("docs/readme.md" in e for e in info.errors)


def test_an_empty_zip_is_an_error():
    assert SERVICE.inspect("b.zip", zipped({})).info.errors


def test_a_corrupt_zip_is_an_error():
    info = SERVICE.inspect("b.zip", b"not a zip at all").info

    assert any("zip" in e for e in info.errors)


def test_an_unconventional_directory_warns_without_failing():
    staged = SERVICE.inspect("b.zip", zipped({"SKILL.md": GOOD_MD, "docs/a.md": "a"}))

    assert staged.info.errors == []
    assert any("docs" in w for w in staged.info.warnings)


def test_a_non_utf8_skill_md_is_an_error():
    info = SERVICE.inspect("b.zip", zipped({"SKILL.md": b"\xff\xfe\x00bad"})).info

    assert any("UTF-8" in e for e in info.errors)


# --- archive safety ---------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["../escape.md", "../../etc/passwd", "a/../../b.md", "/abs/path.md"]
)
def test_paths_that_escape_the_prefix_are_rejected(path):
    """
    Not a formatting problem: these would write outside `skills/<name>/`, so
    validation stops rather than reporting them next to spec warnings.
    """
    staged = SERVICE.inspect("b.zip", zipped({"SKILL.md": GOOD_MD, path: "x"}))

    assert any("안전하지 않은 경로" in e for e in staged.info.errors)
    assert staged.files == {}


def test_a_windows_style_traversal_is_rejected():
    staged = SERVICE.inspect(
        "b.zip", zipped({"SKILL.md": GOOD_MD, "..\\..\\etc\\passwd": "x"})
    )

    assert staged.info.errors
    assert staged.files == {}


def test_a_symlink_entry_is_rejected():
    """A link would let the harness read a file the bundle never shipped."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("SKILL.md", GOOD_MD)
        link = zipfile.ZipInfo("secret.md")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")

    staged = SERVICE.inspect("b.zip", buf.getvalue())

    assert any("심볼릭 링크" in e for e in staged.info.errors)
    assert staged.files == {}


def test_an_oversize_upload_is_rejected_before_it_is_parsed():
    info = SERVICE.inspect("b.zip", b"x" * (MAX_BUNDLE_BYTES + 1)).info

    assert any("MB" in e for e in info.errors)


def test_a_zip_that_expands_past_the_limit_is_rejected():
    """Guarded on the declared size, so a small archive can't balloon in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", GOOD_MD)
        archive.writestr("big.bin", b"\0" * (MAX_BUNDLE_BYTES + 1))

    info = SERVICE.inspect("b.zip", buf.getvalue()).info

    assert len(buf.getvalue()) < MAX_BUNDLE_BYTES, "the archive itself is small"
    assert any("압축을 풀면" in e for e in info.errors)


def test_too_many_entries_is_rejected():
    files = {"SKILL.md": GOOD_MD}
    files.update({f"references/f{i}.md": "x" for i in range(MAX_BUNDLE_ENTRIES + 1)})

    info = SERVICE.inspect("b.zip", zipped(files)).info

    assert any(str(MAX_BUNDLE_ENTRIES) in e for e in info.errors)


# --- filename encoding ------------------------------------------------------


def test_a_cp437_encoded_korean_path_is_decoded():
    """
    zipfile decodes entry names as cp437 unless the archive sets the UTF-8 flag,
    so a Korean path from a non-UTF-8 zipper arrives mojibake — and would be
    published to S3 under the garbled key.
    """
    entry = zipfile.ZipInfo("references/보고서.md")
    entry.flag_bits = 0
    entry.filename = "references/보고서.md".encode("utf-8").decode("cp437")

    assert _entry_name(entry) == "references/보고서.md"


def test_a_utf8_flagged_path_is_left_alone():
    entry = zipfile.ZipInfo("references/보고서.md")
    entry.flag_bits = 0x800

    assert _entry_name(entry) == "references/보고서.md"


def test_a_non_ascii_path_warns_but_is_kept():
    """S3 keys take UTF-8; it is metadata headers that are ASCII-only, and the
    bundle writes none. So the file is published, with a note."""
    staged = SERVICE.inspect(
        "b.zip", zipped({"SKILL.md": GOOD_MD, "references/보고서.md": "# 보고서"})
    )

    assert staged.info.errors == []
    assert "references/보고서.md" in staged.files
    assert any("ASCII" in w for w in staged.info.warnings)
