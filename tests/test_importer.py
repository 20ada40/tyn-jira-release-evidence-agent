from conftest import LABEL, MINI
from fakes import FakeJira
from release_agent.adf import adf_to_text, text_to_adf
from release_agent.importer import run_import, verify_import


def test_adf_round_trip_preserves_text():
    for text in ["one line", "two\nlines", "blank\n\nline", 'json {"a": "b"} | pipes', ""]:
        assert adf_to_text(text_to_adf(text)) == text


def test_import_preserves_ids_text_links_and_status(jira):
    by_src = {f["labels"][2]: key for key, f in jira.issues.items()}
    assert set(by_src) == {"src-MANIFEST", "src-REQ-01", "src-REC-1"}
    req = by_src["src-REQ-01"]
    assert jira.issues[req]["summary"] == "[REQ-01] Regression"
    assert jira.issues[req]["labels"] == [LABEL, "rr-requirement", "src-REQ-01", "rel-R9"]
    description = adf_to_text(jira.issues[req]["description"])
    assert "Source ID: REQ-01" in description and "Recorded status: Done" in description
    assert description.endswith("Acceptance criterion: all cases pass.")
    # comments verbatim and in order
    rec = by_src["src-REC-1"]
    stored = [adf_to_text(c["body"]) for c in jira.comments[rec]]
    assert stored == MINI["records"][2]["comments"]
    # directed links and mirrored status
    assert (by_src["src-MANIFEST"], req) in jira.links and (req, rec) in jira.links
    assert jira.status[req] == "Done" and jira.status[rec] == "In Progress"


def test_import_is_idempotent(jira, dataset_path):
    before = (len(jira.issues), sum(map(len, jira.comments.values())), len(jira.links))
    summary = run_import(jira, dataset_path, "TST", log=lambda *_: None)
    after = (len(jira.issues), sum(map(len, jira.comments.values())), len(jira.links))
    assert before == after
    assert summary["issues_created_this_run"] == 0
    assert summary["comments_added_this_run"] == 0
    assert summary["links_created_this_run"] == 0
    assert summary["status_transitions_this_run"] == 0


def test_import_resumes_partial_comments(dataset_path):
    fake = FakeJira("TST")
    run_import(fake, dataset_path, "TST", log=lambda *_: None)
    rec = next(k for k, f in fake.issues.items() if "src-REC-1" in f["labels"])
    del fake.comments[rec][20:]  # simulate an interrupted run
    summary = run_import(fake, dataset_path, "TST", log=lambda *_: None)
    assert summary["comments_added_this_run"] == 26
    assert [adf_to_text(c["body"]) for c in fake.comments[rec]] == MINI["records"][2]["comments"]


def test_verify_counts(jira, dataset_path):
    v = verify_import(jira, dataset_path, "TST")
    assert v["issues_in_jira"] == v["issues_expected"] == 3
    assert v["comments_in_jira"] == v["comments_expected"] == 48
    assert v["links_in_jira"] == v["links_expected"] == 2
