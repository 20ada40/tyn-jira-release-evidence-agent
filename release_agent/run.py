"""CLI: run the release evidence agent against a Jira project.

  python -m release_agent.run --project KEY --label tyn-release-assessment-v2 --release R2.4

Writes report.md, report.json, trace.jsonl, trace.md and metrics.json to --out.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from .agent import AgentFailure, ReleaseAgent, overall_decision
from .config import (DEFAULT_MODEL, ConfigError, agent_model, agent_provider, default_project,
                     load_jira_settings)
from .jira_client import JiraClient, JiraError
from .llm_compat import PROVIDERS, LLMError, build_compat_llm
from .report import render_markdown, render_trace_markdown
from .tools import JiraToolbox, ToolError
from .trace import Tracer, Usage


def _title(summary: str | None) -> str:
    return re.sub(r"^\[[^\]]+\]\s*", "", summary or "")


def _explain(overall: str, reqs: list[dict], failures: list[dict]) -> str:
    def ids(decision: str) -> str:
        return ", ".join(r["requirement_id"] for r in reqs if r["decision"] == decision)
    if not reqs:
        return ("No requirement could be assessed in this run (see Run failures), so the release "
                "cannot be called ready.")
    parts = []
    if overall == "BLOCKED":
        parts.append(f"The release is blocked because applicable evidence shows a failure for: {ids('FAIL')}.")
        if ids("INSUFFICIENT_EVIDENCE"):
            parts.append(f"In addition, evidence is insufficient for: {ids('INSUFFICIENT_EVIDENCE')}.")
    elif overall == "NEEDS_REVIEW":
        parts.append("No requirement has a demonstrated failure, but evidence is insufficient for: "
                     f"{ids('INSUFFICIENT_EVIDENCE')}.")
    else:
        parts.append("Every mandatory requirement is established by applicable evidence.")
    if failures:
        parts.append("Parts of this run failed and are listed under Run failures.")
    return " ".join(parts)


def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = load_jira_settings()
    jira = JiraClient(settings.base_url, settings.email, settings.api_token)
    toolbox = JiraToolbox(jira, args.project, args.label, settings.base_url)
    tracer = Tracer(out_dir / "trace.jsonl")
    if args.provider == "anthropic":
        llm: Any = anthropic.Anthropic(max_retries=4, timeout=600.0)
    else:
        llm = build_compat_llm(args.provider)
    agent = ReleaseAgent(llm, toolbox, tracer, model=args.model, effort=args.effort,
                         max_steps=args.max_steps,
                         use_fallbacks=args.provider == "anthropic" and not args.no_fallbacks)

    started = time.time()
    usage = Usage()
    failures: list[dict] = []
    requirements: list[dict] = []
    scope: dict[str, Any] = {"project": args.project, "dataset_label": args.label,
                             "release": args.release}
    timings: dict[str, float] = {}

    # ---- phase 1: the agent finds the manifest and the requirement set ----
    print(f"[scope] locating the release manifest for {args.release} ...", flush=True)
    t0 = time.time()
    manifest = None
    requirement_views: list[dict] = []
    try:
        found = agent.find_scope(args.release)
        usage.merge(found.usage)
        manifest_key = found.payload["manifest_key"].strip().upper()
        manifest = found.visited.issues[manifest_key]
        comments = found.visited.comments.get(manifest_key, {})
        manifest_text = manifest["description"]
        if comments:
            manifest_text += "\n\nManifest comments:\n" + "\n".join(c["text"] for c in comments.values())
        manifest = {**manifest, "description": manifest_text}
        scope.update(manifest_key=manifest_key, manifest_url=toolbox.browse_url(manifest_key),
                     target=found.payload["target"], scope_notes=found.payload.get("notes"))
        linked = {l["key"] for l in manifest["links_to"]}
        chosen = []
        for key in found.payload["requirement_keys"]:
            key = str(key).strip().upper()
            if key not in chosen:
                chosen.append(key)
        if linked and set(chosen) != linked:
            tracer.event("scope", "orchestrator",
                         message=f"requirement set differs from manifest links: "
                                 f"extra={sorted(set(chosen) - linked)}, missing={sorted(linked - set(chosen))}")
            for key in sorted(linked - set(chosen)):
                chosen.append(key)  # never silently drop a requirement the manifest links
        for key in chosen:
            try:
                requirement_views.append(toolbox.get_issue(key, found.visited))
            except (ToolError, JiraError) as exc:
                failures.append({"scope": key, "reason": f"requirement record unusable: {exc}"})
        tracer.event("scope", "orchestrator",
                     message=f"manifest {manifest_key}; {len(requirement_views)} requirements to assess")
    except AgentFailure as exc:
        usage.merge(getattr(exc, "usage", Usage()))
        failures.append({"scope": "scope", "reason": str(exc)})
    timings["scope"] = round(time.time() - t0, 1)

    # ---- phase 2: one bounded investigation per requirement ---------------
    def assess(view: dict) -> dict:
        t = time.time()
        entry: dict[str, Any] = {
            "requirement_id": view.get("source_id") or view["key"], "jira_key": view["key"],
            "title": _title(view.get("summary")), "url": toolbox.browse_url(view["key"]),
        }
        try:
            result = agent.assess_requirement(view, scope["target"], manifest)
        except AgentFailure as exc:
            entry.update(decision="INSUFFICIENT_EVIDENCE", agent_error=str(exc), summary="",
                         rationale="", evidence=[], outstanding=["Re-run the assessment for this requirement."],
                         anomalies=[], steps=None)
            entry["_usage"] = getattr(exc, "usage", Usage())
        else:
            p = result.payload
            evidence = []
            for item in p["evidence"]:
                issue = result.visited.issues[item["issue_key"]]
                comment = result.visited.comments.get(item["issue_key"], {}).get(item.get("comment_id", ""))
                evidence.append({
                    "issue_key": item["issue_key"], "source_id": issue.get("source_id"),
                    "issue_summary": issue.get("summary"),
                    "comment_id": item.get("comment_id"),
                    "comment_ordinal": comment["ordinal"] if comment else None,
                    "role": item["role"], "note": item["note"],
                    "recorded_text": comment["text"] if comment else None,
                    "url": toolbox.browse_url(item["issue_key"], item.get("comment_id")),
                })
            entry.update(decision=p["decision"], summary=p["summary"], rationale=p["rationale"],
                         evidence=evidence, outstanding=p["outstanding"], anomalies=p["anomalies"],
                         steps=result.steps, issues_read=sorted(result.visited.issues))
            entry["_usage"] = result.usage
        entry["duration_seconds"] = round(time.time() - t, 1)
        print(f"[{entry['requirement_id']}] {entry['decision']}"
              f"{' (agent failure)' if entry.get('agent_error') else ''} in {entry['duration_seconds']}s",
              flush=True)
        return entry

    if requirement_views:
        print(f"[assess] {len(requirement_views)} requirements, {args.workers} in parallel ...", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            requirements = list(pool.map(assess, requirement_views))
        requirements.sort(key=lambda r: r["requirement_id"])
        for r in requirements:
            u = r.pop("_usage")
            usage.merge(u)
            r["usage"] = u.as_dict()
            timings[r["requirement_id"]] = r["duration_seconds"]
            if r.get("agent_error"):
                failures.append({"scope": r["requirement_id"], "reason": r["agent_error"]})

    # ---- deterministic overall verdict + outputs --------------------------
    decisions = [r["decision"] for r in requirements]
    overall = overall_decision(decisions)
    duration = round(time.time() - started, 1)
    report = {
        "overall_decision": overall,
        "overall_explanation": _explain(overall, requirements, failures),
        "decision_counts": dict(Counter(decisions)),
        "scope": scope,
        "requirements": requirements,
        "failures": failures,
        "run": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "provider": args.provider, "model_requested": args.model, "effort": args.effort,
            "max_steps_per_investigation": args.max_steps, "parallel_investigations": args.workers,
            "duration_seconds": duration, "phase_seconds": timings,
            "jira_api_calls": jira.api_calls, "usage": usage.as_dict(),
        },
    }
    tracer.close()
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    (out_dir / "trace.md").write_text(render_trace_markdown(tracer.events), encoding="utf-8")
    (out_dir / "metrics.json").write_text(json.dumps(report["run"], indent=2), encoding="utf-8")

    print(f"\nOverall: {overall}  ({dict(Counter(decisions))})")
    print(f"Time {duration}s, model calls {usage.model_calls}, tool calls {usage.tool_calls}, "
          f"tokens {sum(usage.tokens.values())}, est. cost USD {usage.estimated_cost_usd()}")
    print(f"Report: {out_dir / 'report.md'}")
    if failures:
        print(f"{len(failures)} failure(s) during the run - see the report.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jira release evidence agent")
    parser.add_argument("--project", default=default_project(), help="Jira project key")
    parser.add_argument("--label", required=True, help="Dataset scope: Jira label on every imported issue")
    parser.add_argument("--release", required=True, help="Target release, e.g. R2.4")
    parser.add_argument("--out", default="outputs")
    parser.add_argument("--provider", default=agent_provider(),
                        choices=["anthropic", *PROVIDERS, "custom"],
                        help="Model provider (default: LLM_PROVIDER or anthropic). All but anthropic "
                             "use an OpenAI-compatible chat-completions endpoint.")
    parser.add_argument("--model", default=agent_model(),
                        help=f"Model id (default: AGENT_MODEL; {DEFAULT_MODEL} for anthropic)")
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--max-steps", type=int, default=16, help="Model turns allowed per investigation")
    parser.add_argument("--workers", type=int, default=4, help="Requirements investigated in parallel")
    parser.add_argument("--no-fallbacks", action="store_true",
                        help="Disable the server-side refusal fallback (beta) on model calls")
    args = parser.parse_args(argv)
    if not args.project:
        parser.error("--project is required (or set JIRA_PROJECT_KEY)")
    if not args.model:
        if args.provider != "anthropic":
            parser.error(f"--model is required for provider {args.provider} (or set AGENT_MODEL)")
        args.model = DEFAULT_MODEL
    try:
        return run(args)
    except (ConfigError, JiraError, LLMError, anthropic.AnthropicError) as exc:
        print(f"RUN FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
