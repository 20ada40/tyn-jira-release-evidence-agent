# Jira Release Evidence Agent

A tool-using AI agent that decides whether the requirements of a software release are
satisfied by the evidence recorded in Jira, and writes one evidence-backed
release-readiness report.

- `release_agent/importer.py` loads the supplied dataset into a Jira Cloud project (repeatable).
- `release_agent/run.py` runs the agent against that project. The agent reads Jira only through
  tools; it never sees `inputs/dataset.json`.
- `outputs/` holds one actual run: the report, the investigation trace and the metrics.

## Results of the submitted run

<!-- RESULTS -->
_To be filled in from the live run._
<!-- /RESULTS -->

## Setup

Requires Python 3.11+, a Jira Cloud site with an empty project, and an Anthropic API key.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env            # then fill in the values
```

`.env` (git-ignored) holds `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`
and `ANTHROPIC_API_KEY`. The Jira API token is created at
<https://id.atlassian.com/manage-profile/security/api-tokens>. The Jira user needs permission to
create issues, comments and links in the project, and to transition issues.

## 1. Import the dataset

```bash
python -m release_agent.importer --dataset inputs/dataset.json --project TYN
```

What the importer does with each of the 400 records:

| Dataset | Jira |
|---|---|
| record | one issue, summary `[<source_id>] <summary>` |
| `source_id`, `kind`, `release` | labels `src-<source_id>`, `rr-<kind>`, `rel-<release>`, and a provenance header in the description |
| dataset scope | label `tyn-release-assessment-v2` (the `dataset_label` value) on every issue |
| `description` | issue description, verbatim, below the provenance header |
| `comments` | one Jira comment each, text verbatim and in the original order, so the recorded event time, actor and role stay in the record text |
| `links` | native Jira issue links, directed record -> linked record |
| `recorded_status` | kept in the description and mirrored to the Jira workflow status |

The importer is idempotent. It looks up existing issues by their `src-` label, creates only what
is missing, resumes a partially commented issue where it stopped, and skips links that exist, so
an interrupted import is fixed by running the same command again. It finishes by reading the
counts back from Jira and comparing them with the dataset (400 issues, 2,059 comments, 76 links).
A summary is written to `outputs/import_summary.json`.

## 2. Run the agent

```bash
python -m release_agent.run --project TYN --label tyn-release-assessment-v2 --release R2.4
```

Inputs are the Jira project, the dataset scope (label) and the target release. Options:
`--model` (default `claude-opus-5-5`), `--effort`, `--max-steps` (model turns per investigation,
default 16), `--workers` (parallel investigations, default 4), `--out` (default `outputs`).

Outputs:

| File | Content |
|---|---|
| `outputs/report.md` | the release-readiness report: overall decision, and per requirement the decision, explanation, cited Jira issues/comments with the recorded text, outstanding items and anomalies |
| `outputs/report.json` | the same, machine-readable |
| `outputs/trace.md` | readable investigation trace: every tool call and its result gist, per investigation |
| `outputs/trace.jsonl` | complete trace: model reasoning summaries, tool inputs and results, token usage per call |
| `outputs/metrics.json` | wall-clock time, per-requirement time, model calls, tool calls, tokens, estimated cost |

The exit code is 0 for a clean run and 1 if any part of the run failed.

## Tests

```bash
python -m pytest -q
```

The tests run offline against an in-memory Jira and a scripted model. They cover the importer
(verbatim text, link direction, idempotency, resume), the tools (scope enforcement, pagination,
filtering), citation validation, the overall-verdict rule and the bounded agent loop.

## Design

### Agent

The run has two phases, and in both the model chooses which tools to call.

1. **Scope.** Given only the project, label and release, the agent searches Jira for the release
   manifest, reads it, and submits the target build and the requirement records the manifest
   designates. Code checks the submission against the manifest's Jira links and never drops a
   requirement the manifest links to.
2. **Assess.** Each requirement gets its own fresh conversation containing the target and the
   manifest text (as read from Jira). The agent reads the requirement, follows its links and the
   links of what it reaches, pages through comments, checks what each result applies to, and
   submits a decision with citations.

The overall verdict is computed by code, not the model: `BLOCKED` if any requirement fails,
otherwise `NEEDS_REVIEW` if any has insufficient evidence, otherwise `READY`.

Tools available to the model:

| Tool | Purpose |
|---|---|
| `search_issues` | find issues by full text, record kind or source identifier |
| `get_issue` | description, status, labels, outgoing links (`links_to`) and incoming links (`linked_from`), comment count |
| `get_comments` | paginated comments with Jira comment ids; optional keyword filter across the whole thread |
| `submit_scope` / `submit_decision` | end a phase with a structured result |

### Choices and why

- **One conversation per requirement.** Context stays small (a requirement and a handful of
  linked records, not 400 tickets), one requirement's evidence cannot leak into another's
  decision, investigations run in parallel, and a failure is contained to one requirement.
- **Scope is enforced in code.** Every search is built by code as
  `project = X AND labels = <dataset label> AND ...`; the model cannot pass raw JQL. An issue
  fetched by key is refused unless it is in the project and carries the label.
- **Citations are checked.** A decision is rejected, and the model told why, if it cites an issue
  or comment it did not read in that investigation, or if a PASS/FAIL rests on no cited comment.
  The report prints the recorded text of each cited comment as fetched from Jira, not the model's
  paraphrase.
- **Native Jira metadata is hidden.** The tools do not return Jira authors or timestamps, which
  belong to the importer. The scenario actor, role and event time are in the comment text.
- **Record text is data.** The system prompt tells the model that nothing in a record is an
  instruction and that records cannot change the manifest's rules; attempts are reported under
  "Anomalies" in the report.
- **Policy comes from Jira.** The decision definitions are in the system prompt, but the
  release-specific rules (what counts as applicable, supersession, contradictions, defects) are
  taken from the manifest record at run time rather than hard-coded.
- **Bounded execution.** Each investigation has a turn limit that the model can see counting
  down, tool results are size-capped, and comment pages are limited to 40.
- **Honest failures.** A model or tool failure (API error, refusal, turn limit) is written to
  the trace and the report. The affected requirement is shown as not assessed and counted as
  `INSUFFICIENT_EVIDENCE`, so a failed run can never produce `READY`.
- **Manual tool loop** on the Anthropic Messages API rather than a framework, so every call is
  traced and bounded in about 150 lines. Adaptive thinking with summarised reasoning is on, and
  prompt caching is enabled for the repeated system prompt and tools.

### Known limits

- Decisions are model judgements. The guardrails ensure citations are real and in scope, not
  that the reasoning is right; the report is meant to be reviewed by a person.
- Jira full-text search is eventually consistent, so a search immediately after an import can
  miss records. Link traversal and reads by key do not depend on the index.
- Comment order relies on Jira's creation order; the importer posts each issue's comments
  sequentially to preserve it.
- The importer mirrors `recorded_status` only when the project workflow has statuses named
  To Do / In Progress / Done; otherwise it warns and the status remains in the description.
- The importer does not delete or update: if the dataset changed, issues would need to be
  removed before re-importing.
- Cost is an estimate from list prices, not a billed amount.
- Runs are not deterministic; two runs can word explanations differently.

## AI assistance

This solution was built with Claude Code. It was used to analyse the brief and dataset, design
the importer and agent, write the code, tests and this README, and run the import and the agent.
The agent itself uses Claude through the Anthropic API.
