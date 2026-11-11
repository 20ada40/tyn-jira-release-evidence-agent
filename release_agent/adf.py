"""Minimal Atlassian Document Format (ADF) helpers.

Jira Cloud REST v3 stores descriptions and comments as ADF. We only need plain
text in both directions: one paragraph per record, with hard breaks between
lines, so the original text round-trips byte-for-byte.
"""
from __future__ import annotations

from typing import Any


def text_to_adf(text: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            content.append({"type": "text", "text": line})
        if i < len(lines) - 1:
            content.append({"type": "hardBreak"})
    paragraph: dict[str, Any] = {"type": "paragraph"}
    if content:
        paragraph["content"] = content
    return {"type": "doc", "version": 1, "content": [paragraph]}


_BLOCKS = {"paragraph", "heading", "blockquote", "codeBlock", "listItem", "rule", "tableRow"}


def adf_to_text(node: Any) -> str:
    """Flatten an ADF document (or None / plain string) to text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    out: list[str] = []
    _walk(node, out)
    return "".join(out).strip("\n")


def _walk(node: dict[str, Any], out: list[str]) -> None:
    kind = node.get("type")
    if kind == "text":
        out.append(node.get("text", ""))
        return
    if kind == "hardBreak":
        out.append("\n")
        return
    if kind in ("mention", "emoji", "inlineCard"):
        attrs = node.get("attrs", {})
        out.append(str(attrs.get("text") or attrs.get("shortName") or attrs.get("url") or ""))
        return
    for child in node.get("content", []) or []:
        _walk(child, out)
    if kind in _BLOCKS and out and not out[-1].endswith("\n"):
        out.append("\n")
