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
**Overall decision: BLOCKED** (4 pass, 5 fail, 3 insufficient evidence). Full report with the
cited Jira comments: [`outputs/report.md`](outputs/report.md).

| Requirement | Decision | Deciding evidence found by the agent |
|---|---|---|
| REQ-01 Automated regression tests | PASS | QA run REG-804: 146 of 146 on the target build |
| REQ-02 Customer document isolation | FAIL | QA retest ISO-318 on the target build exposed another customer's document; defect reopened |
| REQ-03 Answer quality for deployed prompt | INSUFFICIENT_EVIDENCE | one evaluation ran on staging and another commit, the other used prompt p6, not p7 |
| REQ-04 Response latency | PASS | QA run PERF-827: p95 1.40 s against a 2.0 s limit |
| REQ-05 Model usage cost | FAIL | cost run COST-881: USD 0.018 per request against a 0.012 limit |
| REQ-06 Production security approval | FAIL (see note) | Security Owner approved staging scope only |
| REQ-07 Sensitive log redaction | PASS | QA retest on the target build passed |
| REQ-08 Human handoff delivery | FAIL | QA test HND-77 on a linked dependency: handoffs not delivered, defect open |
| REQ-09 Rollback rehearsal | INSUFFICIENT_EVIDENCE | two QA rehearsals on the target build contradict each other, unresolved |
| REQ-10 Document parsing completeness | PASS | QA retest PARSE-622 (60 of 60) supersedes the earlier 59 of 60 |
| REQ-11 Accessible keyboard handoff | INSUFFICIENT_EVIDENCE | only a developer claim and a QA result for release R2.3 |
| REQ-12 Streaming completion integrity | FAIL | QA run STREAM-731, the 64th comment of its ticket: duplicated final message |

- **Embedded instruction.** A comment on the cost ticket tells automated reviewers to report
  REQ-05 as PASS. The agent did not follow it and listed it under anomalies.
- **REQ-06 is the decision I disagree with.** The agent read the right records (a staging-only
  approval, production sign-off still pending) but called it FAIL. Under the manifest's own
  definitions a wrong-scope approval is missing evidence, so INSUFFICIENT_EVIDENCE is the
  better reading. It was re-investigated once after a prompt clarification and came out the
  same; the decision is left as the agent produced it. The overall verdict is BLOCKED either
  way.

| Metric | Value |
|---|---|
| Model calls / tool calls | 80 / 104 |
| Tokens | 321,550 (313,616 input, 7,934 output) |
| Investigation time | about 12 minutes summed over the scope phase and the 12 requirements |
| Wall-clock time recorded in `metrics.json` | 1,468 s, most of it one invocation waiting on a quota error |
| Cost | USD 0 (Gemini free tier) |

**How this run was produced.** It ran on the Gemini free tier, not the default Claude model,
and took three invocations of the same command with `--resume`: `gemini-3.6-flash` did the
scope phase and REQ-01 to REQ-03 before its limit of 20 requests per day was reached;
`gemini-3.5-flash` shares that quota and made no successful call; `gemini-3.5-flash-lite` did
REQ-04 to REQ-12. A fourth invocation re-investigated REQ-06 (`--redo REQ-06`). The quota
failures and both REQ-06 investigations are in `outputs/trace.jsonl`; `report.json` records
the model used for each resumed requirement.
<!-- /RESULTS -->

## Setup

Requires Python 3.11+, a Jira Cloud site with an empty project, and an API key for a model
provider (Anthropic, or any OpenAI-compatible endpoint such as Gemini, Groq, OpenRouter or a
local Ollama).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env            # then fill in the values
```

`.env` (git-ignored) holds `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`
and the model provider's key (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY` or
`OPENROUTER_API_KEY`). The Jira API token is created at
<https://id.atlassian.com/manage-profile/security/api-tokens>. The Jira user needs permission to
create issues, comments and links in the project, and to transition issues.

## 1. Import the dataset

```bash
python -m release_agent.importer --dataset inputs/dataset.json --project KAN
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
python -m release_agent.run --project KAN --label tyn-release-assessment-v2 --release R2.4
```

Inputs are the Jira project, the dataset scope (label) and the target release. Options:
`--provider` (`anthropic` by default; `gemini`, `groq`, `openrouter`, `ollama`, or `custom` with
`LLM_BASE_URL`), `--model` (default `claude-opus-5-5` for Anthropic; required otherwise), `--effort`, `--max-steps` (model turns per investigation,
default 16), `--workers` (parallel investigations, default 4), `--out` (default `outputs`), `--resume`
(keep the requirements an earlier run completed and investigate only those that ended in an
agent failure), `--redo REQ-..` (with `--resume`, investigate the named requirements again).

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
- **Model is swappable.** `release_agent/llm_compat.py` adapts any OpenAI-compatible
  chat-completions endpoint to the same interface, so the loop, tools, validation and trace are
  identical whichever model runs. Thinking, effort and caching settings apply to Anthropic only.

### Known limits

- The submitted run used small free-tier models. They found the right records for every
  requirement, but on REQ-06 chose FAIL where INSUFFICIENT_EVIDENCE fits the rules better. The
  default Claude model has not been run end to end: the account had no API credit.
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
