"""Strands tools for producing artifacts rendered in the chat side panel.

Like the UI tools, these return only a short confirmation: the content is
reconstructed from the accumulated tool input in `ArtifactHandler` and injected
into the stream. Echoing the document back as a tool result would re-feed the
whole thing into the model's context.

The model chooses `artifact_id` itself so that `update_artifact` can target the
same artifact. The tool body runs after the injected event is emitted, so it
cannot be the one to mint the id.
"""
from strands import tool


@tool
def create_artifact(
    artifact_id: str, title: str, kind: str, content: str, language: str = ""
) -> str:
    """
    Create a standalone artifact shown in a panel next to the chat.

    Call this — do not write the content in your reply — whenever you produce a
    substantial self-contained document: a full source file, code longer than
    ~15 lines, a report or spec, a diagram, an HTML page, a dataset. If the user
    asked for an "artifact", calling this tool is mandatory.

    Args:
        artifact_id: A short kebab-case id you invent for this artifact, e.g.
            "q3-revenue-report" or "fib-py". Reuse it with update_artifact.
        title: Short human-readable title, e.g. "Q3 Revenue Report"
        kind: One of markdown, code, html, svg, mermaid, csv, json, text.
            Use "mermaid" for any diagram — the panel renders it as a picture.
        content: The full artifact body. Do not truncate or summarize it.
        language: For kind="code", the programming language, e.g. "python"

    Returns:
        Confirmation that the artifact is displayed to the user.
    """
    return (
        f"Created artifact '{artifact_id}' ({title}, {kind}). It is displayed to "
        f"the user in the artifact panel. To revise it, call update_artifact with "
        f"artifact_id='{artifact_id}'. Do not repeat its content in your reply — "
        f"briefly describe it instead."
    )


@tool
def update_artifact(
    artifact_id: str, title: str, kind: str, content: str, language: str = ""
) -> str:
    """
    Save a new version of an existing artifact.

    Use this when the user asks for changes to an artifact you already created,
    so the panel keeps a version history instead of showing a duplicate.

    Args:
        artifact_id: The id you passed to create_artifact
        title: Title for this version (may be unchanged)
        kind: One of markdown, code, html, svg, mermaid, csv, json, text.
            Use "mermaid" for any diagram — the panel renders it as a picture.
        content: The complete revised body. Always send the whole document,
            never a diff or a fragment.
        language: For kind="code", the programming language, e.g. "python"

    Returns:
        Confirmation that a new version was saved.
    """
    return (
        f"Saved a new version of artifact '{artifact_id}' ({title}, {kind}). The "
        f"user sees it in the artifact panel. Do not repeat its content in your "
        f"reply — briefly describe what changed instead."
    )
