"""Repeatable importer: dataset.json -> Jira Cloud project.

Idempotent: every imported issue carries a `src-<source_id>` label, so a re-run
finds what already exists and only creates missing issues, comments and links.

Mapping
  record            -> one Jira issue, summary "[<source_id>] <summary>"
  labels            -> <dataset_label>, rr-<kind>, src-<source_id>, rel-<release>
  description       -> provenance header + the original description text
  comments          -> one Jira comment each, text verbatim, original order
  links             -> native Jira issue links, direction record -> linked record
  recorded_status   -> kept in the description and mirrored to the Jira status

Usage:  python -m release_agent.importer --dataset inputs/dataset.json --project KEY
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .adf import text_to_adf
from .config import ConfigError, default_project, load_jira_settings
from .jira_client import JiraClient, JiraError

SRC_PREFIX = "src-"
KIND_PREFIX = "rr-"
RELEASE_PREFIX = "rel-"
BATCH = 50


def record_labels(rec: dict, dataset_label: str) -> list[str]:
    return [dataset_label, f"{KIND_PREFIX}{rec['kind']}", f"{SRC_PREFIX}{rec['source_id']}",
            f"{RELEASE_PREFIX}{rec['release']}"]


def record_description(rec: dict, dataset_label: str) -> str:
    header = [
        f"Source ID: {rec['source_id']}",
        f"Record kind: {rec['kind']}",
        f"Recorded release: {rec['release']}",
        f"Recorded status: {rec['recorded_status']}",
        f"Import note: imported from dataset {dataset_label}. The native Jira reporter, comment "
        "authors and timestamps belong to the importer; scenario actors, roles and event times "
        "are the ones written in the record text.",
        "----",
    ]
    return "\n".join(header) + "\n" + rec["description"]


def record_fields(rec: dict, project: str, issue_type: str, dataset_label: str) -> dict:
    return {
        "project": {"key": project},
        "issuetype": {"name": issue_type},
        "summary": f"[{rec['source_id']}] {rec['summary']}"[:255],
        "description": text_to_adf(record_description(rec, dataset_label)),
        "labels": record_labels(rec, dataset_label),
    }


def source_id_of(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith(SRC_PREFIX):
            return label[len(SRC_PREFIX):]
    return None


def pick_issue_type(client: Any, project: str, requested: str | None) -> str:
    types = [t for t in client.project_issue_types(project) if not t.get("subtask")]
    names = [t["name"] for t in types]
    if requested:
        if requested not in names:
            raise JiraError(f"Issue type {requested!r} not in project {project}; available: {names}")
        return requested
    for preferred in ("Task", "Story"):
        if preferred in names:
            return preferred
    if not names:
        raise JiraError(f"Project {project} has no usable issue types")
    return names[0]


def pick_link_type(client: Any) -> str:
    names = client.link_type_names()
    for preferred in ("Relates", "Related"):
        if preferred in names:
            return preferred
    if not names:
        raise JiraError("No issue link types exist on this Jira site; enable issue linking.")
    return names[0]


def run_import(client: Any, dataset_path: str | Path, project: str, *,
               issue_type: str | None = None, workers: int = 4,
               log: Callable[[str], None] = print) -> dict:
    started = time.time()
    dataset = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    label = dataset["dataset_label"]
    records = dataset["records"]
    by_id = {r["source_id"]: r for r in records}
    if len(by_id) != len(records):
        raise ValueError("Duplicate source_id in dataset")

    issue_type = pick_issue_type(client, project, issue_type)
    link_type = pick_link_type(client)
    log(f"Project {project}: issue type {issue_type!r}, link type {link_type!r}, label {label!r}")

    # 1. What already exists from a previous run?
    key_of: dict[str, str] = {}
    existing_status: dict[str, str] = {}
    existing_links: dict[str, set[str]] = {}
    for issue in client.scoped_search(project, [label],
                                      fields=["labels", "status", "issuelinks"]):
        sid = source_id_of(issue["fields"].get("labels", []))
        if sid in by_id:
            key_of[sid] = issue["key"]
            existing_status[sid] = issue["fields"]["status"]["name"]
            existing_links[sid] = {l["outwardIssue"]["key"]
                                   for l in issue["fields"].get("issuelinks", [])
                                   if "outwardIssue" in l}
    log(f"Found {len(key_of)} previously imported issues")

    # 2. Create the missing issues in bulk.
    missing = [r for r in records if r["source_id"] not in key_of]
    created = 0
    for i in range(0, len(missing), BATCH):
        chunk = missing[i:i + BATCH]
        result = client.bulk_create([record_fields(r, project, issue_type, label) for r in chunk])
        failed = {e["failedElementNumber"]: e for e in result.get("errors", [])}
        if failed:
            raise JiraError(f"Bulk create failed for {len(failed)} issues: "
                            f"{json.dumps(list(failed.values())[:2])[:800]}")
        ok = [r for n, r in enumerate(chunk) if n not in failed]
        for rec, issue in zip(ok, result["issues"]):
            key_of[rec["source_id"]] = issue["key"]
        created += len(ok)
        log(f"Created {created}/{len(missing)} issues")

    # 3. Comments (in original order) and recorded status, per issue.
    def sync_issue(rec: dict) -> tuple[int, bool, str | None]:
        key = key_of[rec["source_id"]]
        warning = None
        have = client.get_comments(key, 0, 1).get("total", 0)
        if have > len(rec["comments"]):
            warning = f"{key} has {have} comments, dataset has {len(rec['comments'])}"
        added = 0
        for body in rec["comments"][have:]:
            client.add_comment(key, text_to_adf(body))
            added += 1
        moved = False
        want = rec["recorded_status"]
        current = existing_status.get(rec["source_id"])
        if current is None or current.lower() != want.lower():
            options = client.transitions(key)
            match = next((t for t in options if t["to"]["name"].lower() == want.lower()), None)
            if match and (current is None or match["to"]["name"].lower() != current.lower()):
                client.do_transition(key, match["id"])
                moved = True
            elif not match and current is not None:
                warning = f"{key}: no workflow transition to status {want!r}"
        return added, moved, warning

    comments_added = transitions = 0
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, (added, moved, warning) in enumerate(pool.map(sync_issue, records), 1):
            comments_added += added
            transitions += moved
            if warning:
                warnings.append(warning)
            if n % 50 == 0 or n == len(records):
                log(f"Synced comments/status for {n}/{len(records)} issues")

    # 4. Links: record -> each record it references.
    links_created = 0
    for rec in records:
        for target in rec["links"]:
            if target not in key_of:
                warnings.append(f"{rec['source_id']} links to unknown record {target}")
                continue
            if key_of[target] in existing_links.get(rec["source_id"], set()):
                continue
            client.create_link(link_type, key_of[rec["source_id"]], key_of[target])
            links_created += 1
    log(f"Created {links_created} links")

    summary = {
        "project": project,
        "dataset_label": label,
        "issue_type": issue_type,
        "link_type": link_type,
        "records_in_dataset": len(records),
        "comments_in_dataset": sum(len(r["comments"]) for r in records),
        "links_in_dataset": sum(len(r["links"]) for r in records),
        "issues_created_this_run": created,
        "comments_added_this_run": comments_added,
        "status_transitions_this_run": transitions,
        "links_created_this_run": links_created,
        "warnings": warnings,
        "duration_seconds": round(time.time() - started, 1),
        "issue_keys": dict(sorted(key_of.items())),
    }
    return summary


def verify_import(client: Any, dataset_path: str | Path, project: str) -> dict:
    """Read back from Jira and compare counts with the dataset."""
    dataset = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    label = dataset["dataset_label"]
    issues = client.scoped_search(project, [label], fields=["labels", "issuelinks", "comment"])
    comments = links = 0
    for issue in issues:
        comments += issue["fields"].get("comment", {}).get("total", 0)
        links += sum(1 for l in issue["fields"].get("issuelinks", []) if "outwardIssue" in l)
    return {
        "issues_in_jira": len(issues), "issues_expected": len(dataset["records"]),
        "comments_in_jira": comments,
        "comments_expected": sum(len(r["comments"]) for r in dataset["records"]),
        "links_in_jira": links,
        "links_expected": sum(len(r["links"]) for r in dataset["records"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the assessment dataset into Jira Cloud.")
    parser.add_argument("--dataset", default="inputs/dataset.json")
    parser.add_argument("--project", default=default_project(), help="Jira project key")
    parser.add_argument("--issue-type", default=None, help="Issue type name (default: Task)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", default="outputs/import_summary.json")
    args = parser.parse_args(argv)
    if not args.project:
        parser.error("--project is required (or set JIRA_PROJECT_KEY)")
    try:
        settings = load_jira_settings()
        client = JiraClient(settings.base_url, settings.email, settings.api_token)
        summary = run_import(client, args.dataset, args.project,
                             issue_type=args.issue_type, workers=args.workers)
        summary["verification"] = verify_import(client, args.dataset, args.project)
    except (ConfigError, JiraError) as exc:
        print(f"IMPORT FAILED: {exc}", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "issue_keys"}, indent=2))
    v = summary["verification"]
    ok = (v["issues_in_jira"] == v["issues_expected"] and v["comments_in_jira"] == v["comments_expected"]
          and v["links_in_jira"] == v["links_expected"])
    print("Import verified: counts match." if ok else "WARNING: Jira counts differ from dataset (search index may lag; re-run to check).")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
