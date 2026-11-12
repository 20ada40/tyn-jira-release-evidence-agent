"""Render the release-readiness report (Markdown + JSON) and a readable trace."""
from __future__ import annotations

import json
from typing import Any

ICON = {"PASS": "PASS", "FAIL": "FAIL", "INSUFFICIENT_EVIDENCE": "INSUFFICIENT_EVIDENCE"}
ROLE_TITLE = {
    "supports_decision": "Evidence the decision rests on",
    "superseded": "Superseded results",
    "not_applicable": "Evidence examined and set aside (not applicable to the target)",
    "context": "Context",
}


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0].strip()


def _quote(text: str, limit: int = 600) -> str:
    text = text.strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + " [...]"
    return "\n".join("  > " + line for line in text.split("\n"))


def render_markdown(report: dict) -> str:
    run, scope = report["run"], report["scope"]
    out: list[str] = []
    out.append(f"# Release readiness report: {scope['release']}")
    out.append("")
    out.append(f"**Overall decision: {report['overall_decision']}**")
    out.append("")
    out.append(report["overall_explanation"])
    out.append("")
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| Jira project | `{scope['project']}` |")
    out.append(f"| Dataset scope (label) | `{scope['dataset_label']}` |")
    out.append(f"| Target release | `{scope['release']}` |")
    target = scope.get("target") or {}
    if target:
        out.append("| Target build (from manifest) | "
                   + ", ".join(f"{k} `{v}`" for k, v in sorted(target.items())) + " |")
    if scope.get("manifest_key"):
        out.append(f"| Release manifest | [{scope['manifest_key']}]({scope['manifest_url']}) |")
    out.append(f"| Generated (UTC) | {run['generated_at']} |")
    models = run.get("models_requested") or [run["model_requested"]]
    out.append("| Model | " + ", ".join(f"`{m}`" for m in models) + " |")
    out.append("")
    out.append("All evidence below was read from Jira through the agent's tools during this run. "
               "Actor names, roles and event times are quoted from the record text; native Jira "
               "authors and timestamps belong to the import job and are not used.")
    out.append("")

    if report["failures"]:
        out.append("## Run failures")
        out.append("")
        out.append("The following problems occurred during this run and limit the result:")
        out.append("")
        for failure in report["failures"]:
            out.append(f"- **{failure['scope']}**: {failure['reason']}")
        out.append("")

    reqs = report["requirements"]
    if reqs:
        out.append("## Summary")
        out.append("")
        out.append("| Requirement | Jira | Title | Decision |")
        out.append("|---|---|---|---|")
        for r in reqs:
            out.append(f"| {r['requirement_id']} | [{r['jira_key']}]({r['url']}) | {r['title']} | "
                       f"**{ICON.get(r['decision'], r['decision'])}** |")
        out.append("")
        counts = report["decision_counts"]
        out.append(f"{counts.get('PASS', 0)} pass, {counts.get('FAIL', 0)} fail, "
                   f"{counts.get('INSUFFICIENT_EVIDENCE', 0)} insufficient evidence.")
        out.append("")

        out.append("## Requirement decisions")
        out.append("")
        for r in reqs:
            out.append(f"### {r['requirement_id']} - {r['title']}: {r['decision']}")
            out.append("")
            out.append(f"Jira: [{r['jira_key']}]({r['url']})")
            out.append("")
            if r.get("agent_error"):
                out.append(f"**Not assessed: the agent failed on this requirement** "
                           f"({r['agent_error']}). It is reported as INSUFFICIENT_EVIDENCE because "
                           "no decision was reached, not because the Jira evidence was examined "
                           "and found lacking.")
                out.append("")
                continue
            out.append(f"**Decision:** {r['summary']}")
            out.append("")
            out.append(r["rationale"])
            out.append("")
            for role, title in ROLE_TITLE.items():
                items = [e for e in r["evidence"] if e["role"] == role]
                if not items:
                    continue
                out.append(f"**{title}**")
                out.append("")
                for e in items:
                    ref = f"[{e['issue_key']}]({e['url']})"
                    if e.get("comment_id"):
                        ref += f" comment `{e['comment_id']}` (#{e.get('comment_ordinal')})"
                    else:
                        ref += " (issue description)"
                    label = f" - {e['source_id']}" if e.get("source_id") else ""
                    out.append(f"- {ref}{label}: {e['note']}")
                    if e.get("recorded_text"):
                        out.append(_quote(e["recorded_text"]))
                out.append("")
            out.append("**Outstanding verification or clarification**")
            out.append("")
            out.extend([f"- {x}" for x in r["outstanding"]] or ["- None."])
            out.append("")
            if r["anomalies"]:
                out.append("**Anomalies in the records**")
                out.append("")
                out.extend(f"- {x}" for x in r["anomalies"])
                out.append("")

    out.append("## Run statistics")
    out.append("")
    usage = run["usage"]
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| Wall-clock time | {run['duration_seconds']} s |")
    out.append(f"| Model calls | {usage['model_calls']} |")
    out.append(f"| Jira tool calls by the model | {usage['tool_calls']} |")
    out.append(f"| Jira REST requests | {run['jira_api_calls']} |")
    out.append(f"| Input tokens (uncached / cache write / cache read) | {usage['input_tokens']} / "
               f"{usage['cache_creation_input_tokens']} / {usage['cache_read_input_tokens']} |")
    out.append(f"| Output tokens | {usage['output_tokens']} |")
    cost = usage.get("estimated_cost_usd")
    out.append(f"| Estimated model cost | {'USD ' + str(cost) if cost is not None else 'n/a'} "
               "(estimate from list prices) |")
    out.append(f"| Models that served requests | {', '.join(f'`{m}`' for m in usage['by_model']) or 'n/a'} |")
    out.append("")
    out.append("The full tool-call trace is in `trace.md` (readable) and `trace.jsonl` (complete).")
    out.append("")
    return "\n".join(out)


def render_trace_markdown(events: list[dict]) -> str:
    out = ["# Investigation trace", "",
           "Chronological record per investigation of what the model asked the Jira tools for. "
           "`trace.jsonl` holds the complete events, including model reasoning summaries, token "
           "usage per call and tool results.", ""]
    scopes: list[str] = []
    for e in events:
        if e["scope"] not in scopes:
            scopes.append(e["scope"])
    for scope in scopes:
        out.append(f"## {scope}")
        out.append("")
        for e in (x for x in events if x["scope"] == scope):
            kind = e["event"]
            if kind == "model":
                u = e["usage"]
                for block in e["content"]:
                    if block["type"] == "thinking" and block.get("summary"):
                        out.append(f"- step {e['step']} reasoning: {_one_line(block['summary'], 500)}")
                    elif block["type"] == "text" and block["text"].strip():
                        out.append(f"- step {e['step']} model: {_one_line(block['text'], 500)}")
                out.append(f"- step {e['step']} model call: stop `{e['stop_reason']}`, "
                           f"in {u['input_tokens'] + u['cache_read_input_tokens'] + u['cache_creation_input_tokens']} "
                           f"/ out {u['output_tokens']} tokens, {e['duration_ms']} ms")
            elif kind == "tool":
                flag = " **ERROR**" if e["is_error"] else ""
                out.append(f"  - tool `{e['tool']}` {json.dumps(e['input'], ensure_ascii=False)}{flag} "
                           f"-> {_result_gist(e['tool'], e['result'])} ({e['duration_ms']} ms)")
            elif kind == "submit":
                state = "accepted" if e["accepted"] else f"rejected: {'; '.join(e['problems'])}"
                gist = e["input"].get("decision") or f"{len(e['input'].get('requirement_keys', []))} requirements"
                out.append(f"  - submit `{e['tool']}` ({gist}) -> {state}")
            elif kind == "failure":
                out.append(f"- **FAILURE**: {e['reason']}")
            elif kind == "orchestrator":
                out.append(f"- orchestrator: {e['message']}")
        out.append("")
    return "\n".join(out)


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " [...]"


def _result_gist(tool: str, result: Any) -> str:
    if not isinstance(result, dict):
        return "result"
    if "error" in result:
        return f"error: {result['error']}"
    if result.get("truncated_for_trace"):
        return f"{result['chars']} chars"
    if tool == "search_issues":
        keys = [i["key"] for i in result.get("issues", [])]
        return f"{len(keys)} issues: {', '.join(keys[:14])}{' ...' if len(keys) > 14 else ''}"
    if tool == "get_issue":
        return (f"{result.get('key')} ({result.get('source_id')}), {result.get('comment_count')} comments, "
                f"links_to {[l['key'] for l in result.get('links_to', [])]}")
    if tool == "get_comments":
        extra = f", matched {result['matched']}" if "matched" in result else ""
        return (f"{len(result.get('comments', []))} of {result.get('total_comments')} comments{extra}, "
                f"has_more={result.get('has_more')}")
    return "ok"
