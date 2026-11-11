import pytest

from conftest import LABEL
from fakes import ScriptedLLM, response, text_block, tool_use
from release_agent.agent import (AgentFailure, ReleaseAgent, SUBMIT_DECISION_TOOL, overall_decision,
                                 validate_decision)
from release_agent.tools import JiraToolbox, ToolError, Visited
from release_agent.trace import Tracer


def key_of(jira, source_id):
    return next(k for k, f in jira.issues.items() if f"src-{source_id}" in f["labels"])


@pytest.fixture
def toolbox(jira):
    return JiraToolbox(jira, "TST", LABEL, "https://example.atlassian.net")


# ---- tools -----------------------------------------------------------------
def test_get_issue_shows_directed_links_and_no_native_author(jira, toolbox):
    visited = Visited()
    view = toolbox.call("get_issue", {"issue_key": key_of(jira, "REQ-01")}, visited)
    assert view["source_id"] == "REQ-01" and view["kind"] == "requirement"
    assert [l["key"] for l in view["links_to"]] == [key_of(jira, "REC-1")]
    assert [l["key"] for l in view["linked_from"]] == [key_of(jira, "MANIFEST")]
    assert view["comment_count"] == 1
    assert "author" not in view and "created" not in view


def test_comments_paginate_and_filter(jira, toolbox):
    visited, key = Visited(), key_of(jira, "REC-1")
    page = toolbox.call("get_comments", {"issue_key": key}, visited)
    assert len(page["comments"]) == 20 and page["has_more"] and page["total_comments"] == 46
    last = toolbox.call("get_comments", {"issue_key": key, "start_at": 40, "max_results": 40}, visited)
    assert not last["has_more"] and "10 of 10 cases pass" in last["comments"][-1]["text"]
    hits = toolbox.call("get_comments", {"issue_key": key, "contains": ["QA Lead"]}, visited)
    assert hits["matched"] == 1 and hits["scanned"] == 46
    assert "author" not in hits["comments"][0]


def test_scope_is_enforced(jira, toolbox):
    jira.add_foreign_issue("TST-900", ["some-other-label"])          # right project, wrong label
    jira.add_foreign_issue("OTH-1", [LABEL], project="OTH")           # right label, wrong project
    for key in ("TST-900", "OTH-1", "TST-9999"):
        with pytest.raises(ToolError):
            toolbox.call("get_issue", {"issue_key": key}, Visited())
    found = toolbox.call("search_issues", {"text": "foreign"}, Visited())
    assert found["issues"] == []
    with pytest.raises(ToolError):
        toolbox.call("search_issues", {}, Visited())


def test_search_by_source_id_and_kind(jira, toolbox):
    by_id = toolbox.call("search_issues", {"source_id": "REC-1"}, Visited())
    assert [i["source_id"] for i in by_id["issues"]] == ["REC-1"]
    reqs = toolbox.call("search_issues", {"kind": "requirement"}, Visited())
    assert [i["source_id"] for i in reqs["issues"]] == ["REQ-01"]


# ---- decisions -------------------------------------------------------------
def test_overall_decision_rules():
    assert overall_decision(["PASS", "FAIL", "INSUFFICIENT_EVIDENCE"]) == "BLOCKED"
    assert overall_decision(["PASS", "INSUFFICIENT_EVIDENCE"]) == "NEEDS_REVIEW"
    assert overall_decision(["PASS", "PASS"]) == "READY"
    assert overall_decision([]) == "NEEDS_REVIEW"


def _decision(key, comment_id, decision="PASS"):
    return {"decision": decision, "summary": "s", "rationale": "r", "outstanding": [], "anomalies": [],
            "evidence": [{"issue_key": key, "comment_id": comment_id, "role": "supports_decision",
                          "note": "n"}]}


def test_citations_must_have_been_read(jira, toolbox):
    visited, key = Visited(), key_of(jira, "REC-1")
    assert any("was not read" in p for p in validate_decision(_decision(key, "1"), visited))
    page = toolbox.call("get_comments", {"issue_key": key, "contains": ["QA Lead"]}, visited)
    good = page["comments"][0]["comment_id"]
    assert validate_decision(_decision(key, good), visited) == []
    assert any("was not read" in p for p in validate_decision(_decision(key, "424242"), visited))
    no_comment = _decision(key, good)
    del no_comment["evidence"][0]["comment_id"]
    assert any("at least one cited comment" in p for p in validate_decision(no_comment, visited))


# ---- loop ------------------------------------------------------------------
def make_agent(llm, toolbox, max_steps=6):
    return ReleaseAgent(llm, toolbox, Tracer(None), model="fake-model", max_steps=max_steps)


def always_valid(payload, visited):
    return validate_decision(payload, visited)


def test_loop_runs_tools_rejects_bad_citation_then_accepts(jira, toolbox):
    rec = key_of(jira, "REC-1")
    comment_id = jira.comments[rec][-1]["id"]
    llm = ScriptedLLM([
        response([tool_use("t1", "get_issue", {"issue_key": rec}),
                  tool_use("t2", "get_issue", {"issue_key": "TST-9999"})]),
        response([tool_use("t3", "submit_decision", _decision(rec, comment_id))]),   # not read yet
        response([text_block("let me read")], stop_reason="end_turn"),               # forgot to act
        response([tool_use("t4", "get_comments", {"issue_key": rec, "start_at": 40})]),
        response([tool_use("t5", "submit_decision", _decision(rec, comment_id))]),
    ])
    agent = make_agent(llm, toolbox)
    result = agent.run_loop("REQ", "task", SUBMIT_DECISION_TOOL, always_valid)
    assert result.payload["decision"] == "PASS" and result.steps == 5
    assert result.usage.model_calls == 5 and result.usage.tool_calls == 3
    # tool errors and the rejected submission were reported back to the model, not hidden
    second = llm.requests[1]["messages"][-1]["content"]
    assert [b.get("is_error") for b in second if b["type"] == "tool_result"] == [False, True]
    third = llm.requests[2]["messages"][-1]["content"]
    assert third[0]["is_error"] and "was not read" in third[0]["content"]
    events = [e["event"] for e in agent.tracer.events]
    assert events.count("tool") == 3 and events.count("submit") == 2 and events[-1] == "done"


def test_loop_is_bounded_and_reports_failure(jira, toolbox):
    rec = key_of(jira, "REC-1")
    llm = ScriptedLLM([response([tool_use(f"t{i}", "get_issue", {"issue_key": rec})]) for i in range(3)])
    agent = make_agent(llm, toolbox, max_steps=3)
    with pytest.raises(AgentFailure, match="within 3 turns"):
        agent.run_loop("REQ", "task", SUBMIT_DECISION_TOOL, always_valid)
    assert agent.tracer.events[-1]["event"] == "failure"


def test_refusal_is_a_reported_failure(toolbox):
    agent = make_agent(ScriptedLLM([response([], stop_reason="refusal")]), toolbox)
    with pytest.raises(AgentFailure, match="declined"):
        agent.run_loop("REQ", "task", SUBMIT_DECISION_TOOL, always_valid)


def test_scope_phase_requires_matching_release(jira, toolbox):
    manifest, req = key_of(jira, "MANIFEST"), key_of(jira, "REQ-01")
    submit = lambda release: tool_use("s", "submit_scope", {  # noqa: E731
        "manifest_key": manifest, "target": {"release": release}, "requirement_keys": [req]})
    llm = ScriptedLLM([
        response([tool_use("a", "search_issues", {"kind": "manifest"})]),
        response([tool_use("b", "get_issue", {"issue_key": manifest}),
                  tool_use("c", "get_comments", {"issue_key": manifest})]),
        response([submit("R8")]),
        response([submit("R9")]),
    ])
    result = make_agent(llm, toolbox).find_scope("R9")
    assert result.payload["requirement_keys"] == [req] and result.steps == 4
