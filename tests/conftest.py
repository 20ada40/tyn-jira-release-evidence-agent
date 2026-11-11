import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeJira  # noqa: E402
from release_agent.importer import run_import  # noqa: E402

LABEL = "mini-dataset"

MINI = {
    "dataset_label": LABEL,
    "records": [
        {"source_id": "MANIFEST", "kind": "manifest", "summary": "Release manifest R9", "release": "R9",
         "recorded_status": "Done", "description": "Target: {\"release\": \"R9\"}\nRules: be careful.",
         "comments": ["Recorded event 2026-01-01T10:00:00+00:00 | actor Q A | role QA Lead\nPolicy record."],
         "links": ["REQ-01"]},
        {"source_id": "REQ-01", "kind": "requirement", "summary": "Regression", "release": "R9",
         "recorded_status": "Done", "description": "Acceptance criterion: all cases pass.",
         "comments": ["Recorded event 2026-01-02T10:00:00+00:00 | actor Dev | role Developer\nDone."],
         "links": ["REC-1"]},
        {"source_id": "REC-1", "kind": "evidence", "summary": "Regression run", "release": "R9",
         "recorded_status": "In Progress", "description": "Engineering record.",
         "comments": [f"Recorded event 2026-01-03T10:{i:02d}:00+00:00 | actor N | role Coordinator\nnote {i}"
                      for i in range(45)]
                     + ["Recorded event 2026-01-04T10:00:00+00:00 | actor Q A | role QA Lead\n"
                        "Run REG-1: release R9. 10 of 10 cases pass."],
         "links": []},
    ],
}


@pytest.fixture
def dataset_path(tmp_path):
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(MINI), encoding="utf-8")
    return path


@pytest.fixture
def jira(dataset_path):
    fake = FakeJira("TST")
    run_import(fake, dataset_path, "TST", log=lambda *_: None)
    return fake
