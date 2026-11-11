"""Jira evidence-gathering tools exposed to the model.

Every tool is hard-scoped by code to one project and one dataset label: the
model cannot widen the scope, and an issue outside the scope is refused even
when asked for by key. Results are compact, plain-text views of Jira records.
Native Jira authors/timestamps are deliberately not returned: they belong to
the importer, not to the scenario actors written in the record text.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .adf import adf_to_text
from .importer import KIND_PREFIX, RELEASE_PREFIX, SRC_PREFIX
from .jira_client import JiraError

MAX_SEARCH = 25
MAX_COMMENT_PAGE = 40
ISSUE_FIELDS = ["summary", "description", "status", "labels", "issuelinks", "project"]

SEARCH_TOOL = {
    "name": "search_issues",
    "description": (
        "Search Jira issues inside the fixed project + dataset-label scope. Use it to locate "
        "records (for example the release manifest, requirement records, or tickets that mention "
        "a run/defect identifier). `text` is a Jira full-text query over summary, description and "
        "comments. `kind` filters by imported record kind. `source_id` finds the issue imported "
        "from that dataset source identifier (e.g. REQ-03, REC-1234). Returns key, source_id, "
        "summary, kind, recorded release label and Jira status only; call get_issue / "
        "get_comments to read a record."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Full-text query, e.g. 'ISO-DEF-12'."},
            "kind": {"type": "string", "enum": ["manifest", "requirement", "evidence", "work"]},
            "source_id": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH},
        },
        "additionalProperties": False,
    },
}

GET_ISSUE_TOOL = {
    "name": "get_issue",
    "description": (
        "Read one Jira issue by key: summary, full description, Jira status, labels, the issues "
        "it links to (`links_to`, i.e. records this record references/depends on), the issues "
        "that link to it (`linked_from`), and its comment count. Comments are NOT included; use "
        "get_comments. Jira status is workflow state, not proof of anything."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"issue_key": {"type": "string", "description": "Jira key, e.g. TYN-42."}},
        "required": ["issue_key"],
        "additionalProperties": False,
    },
}

GET_COMMENTS_TOOL = {
    "name": "get_comments",
    "description": (
        "Read comments of one Jira issue in stored order, paginated. Each comment has a Jira "
        "`comment_id` (cite it as evidence), its 1-based `ordinal`, and the verbatim text, which "
        "starts with the recorded scenario event time, actor and role. `has_more` tells you "
        "whether more pages exist: an issue is not fully read until has_more is false. Optional "
        "`contains` returns only comments containing ANY of the given case-insensitive keywords, "
        "searched across ALL comments of the issue (useful to skim long threads; it reports how "
        "many comments were scanned and matched)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_key": {"type": "string"},
            "start_at": {"type": "integer", "minimum": 0, "description": "0-based offset."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_COMMENT_PAGE},
            "contains": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["issue_key"],
        "additionalProperties": False,
    },
}

EVIDENCE_TOOLS = [SEARCH_TOOL, GET_ISSUE_TOOL, GET_COMMENTS_TOOL]


class ToolError(Exception):
    """An error the model can see and react to (bad input, out of scope, Jira failure)."""


@dataclass
class Visited:
    """What one investigation has actually read from Jira (used to validate citations)."""
    issues: dict[str, dict] = field(default_factory=dict)          # key -> issue view
    comments: dict[str, dict[str, dict]] = field(default_factory=dict)  # key -> id -> comment


class JiraToolbox:
    def __init__(self, client: Any, project: str, dataset_label: str, base_url: str = ""):
        self.client = client
        self.project = project
        self.label = dataset_label
        self.base_url = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._issue_cache: dict[str, dict] = {}
        self._comment_cache: dict[str, list[dict]] = {}

    # -- dispatch ----------------------------------------------------------
    def call(self, name: str, args: dict, visited: Visited) -> dict:
        try:
            if name == "search_issues":
                return self.search_issues(args)
            if name == "get_issue":
                return self.get_issue(_req_str(args, "issue_key"), visited)
            if name == "get_comments":
                return self.get_comments(args, visited)
        except JiraError as exc:
            raise ToolError(f"Jira request failed: {exc}") from exc
        raise ToolError(f"Unknown tool {name!r}")

    # -- views -------------------------------------------------------------
    def _brief(self, issue: dict) -> dict:
        f = issue["fields"]
        labels = f.get("labels", [])
        return {
            "key": issue["key"],
            "source_id": _label_value(labels, SRC_PREFIX),
            "summary": f.get("summary"),
            "kind": _label_value(labels, KIND_PREFIX),
            "recorded_release_label": _label_value(labels, RELEASE_PREFIX),
            "jira_status": (f.get("status") or {}).get("name"),
        }

    def _load_issue(self, key: str) -> dict:
        key = key.strip().upper()
        with self._lock:
            cached = self._issue_cache.get(key)
        if cached:
            return cached
        try:
            raw = self.client.get_issue(key, ISSUE_FIELDS)
        except JiraError as exc:
            if exc.status == 404:
                raise ToolError(f"Issue {key} does not exist or is not visible.") from exc
            raise
        f = raw["fields"]
        labels = f.get("labels", [])
        project_key = (f.get("project") or {}).get("key", raw["key"].split("-")[0])
        if project_key != self.project or self.label not in labels:
            raise ToolError(f"Issue {key} is outside the assessment scope "
                            f"(project {self.project}, label {self.label}); it cannot be used.")
        links_to, linked_from = [], []
        for link in f.get("issuelinks", []):
            other, bucket = (link["outwardIssue"], links_to) if "outwardIssue" in link \
                else (link.get("inwardIssue"), linked_from)
            if other:
                bucket.append({"key": other["key"],
                               "summary": other.get("fields", {}).get("summary"),
                               "jira_status": other.get("fields", {}).get("status", {}).get("name")})
        view = {**self._brief(raw),
                "labels": labels,
                "description": adf_to_text(f.get("description")),
                "links_to": links_to,
                "linked_from": linked_from}
        with self._lock:
            self._issue_cache[raw["key"]] = view
        return view

    def _load_comments(self, key: str) -> list[dict]:
        with self._lock:
            cached = self._comment_cache.get(key)
        if cached is not None:
            return cached
        out: list[dict] = []
        while True:
            page = self.client.get_comments(key, len(out), 100)
            batch = page.get("comments", [])
            for c in batch:
                out.append({"comment_id": str(c["id"]), "ordinal": len(out) + 1,
                            "text": adf_to_text(c.get("body"))})
            if not batch or len(out) >= page.get("total", 0):
                break
        with self._lock:
            self._comment_cache[key] = out
        return out

    # -- tools -------------------------------------------------------------
    def search_issues(self, args: dict) -> dict:
        labels = [self.label]
        if args.get("kind"):
            labels.append(f"{KIND_PREFIX}{args['kind']}")
        if args.get("source_id"):
            labels.append(f"{SRC_PREFIX}{str(args['source_id']).strip()}")
        limit = max(1, min(MAX_SEARCH, int(args.get("max_results") or 15)))
        text = (args.get("text") or "").strip() or None
        if len(labels) == 1 and not text:
            raise ToolError("Provide at least one of text, kind or source_id.")
        found = self.client.scoped_search(self.project, labels, text=text,
                                          fields=["summary", "status", "labels"],
                                          max_results=limit + 1)
        return {"scope": {"project": self.project, "label": self.label},
                "issues": [self._brief(i) for i in found[:limit]],
                "truncated": len(found) > limit}

    def get_issue(self, key: str, visited: Visited) -> dict:
        view = self._load_issue(key)
        comments = self._load_comments(view["key"])
        visited.issues[view["key"]] = view
        return {**view, "comment_count": len(comments)}

    def get_comments(self, args: dict, visited: Visited) -> dict:
        view = self._load_issue(_req_str(args, "issue_key"))
        key = view["key"]
        visited.issues[key] = view
        comments = self._load_comments(key)
        keywords = [k.lower() for k in (args.get("contains") or []) if k and k.strip()]
        if keywords:
            hits = [c for c in comments if any(k in c["text"].lower() for k in keywords)]
            shown = hits[:MAX_COMMENT_PAGE]
            result = {"issue_key": key, "total_comments": len(comments), "scanned": len(comments),
                      "matched": len(hits), "comments": shown,
                      "has_more": len(hits) > len(shown), "filtered_by": keywords}
        else:
            start = max(0, int(args.get("start_at") or 0))
            size = max(1, min(MAX_COMMENT_PAGE, int(args.get("max_results") or 20)))
            shown = comments[start:start + size]
            result = {"issue_key": key, "total_comments": len(comments), "start_at": start,
                      "comments": shown, "has_more": start + len(shown) < len(comments)}
        seen = visited.comments.setdefault(key, {})
        for c in shown:
            seen[c["comment_id"]] = c
        return result

    def browse_url(self, key: str, comment_id: str | None = None) -> str:
        url = f"{self.base_url}/browse/{key}"
        return f"{url}?focusedCommentId={comment_id}" if comment_id else url


def _label_value(labels: list[str], prefix: str) -> str | None:
    return next((l[len(prefix):] for l in labels if l.startswith(prefix)), None)


def _req_str(args: dict, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"Missing required string parameter {name!r}.")
    return value
