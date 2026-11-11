"""In-memory stand-ins for Jira Cloud and the model API, shaped like the real responses."""
from __future__ import annotations

from types import SimpleNamespace

from release_agent.jira_client import JiraError


class FakeJira:
    def __init__(self, project: str = "TST"):
        self.project = project
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[dict]] = {}
        self.links: list[tuple[str, str]] = []
        self.status: dict[str, str] = {}
        self._n = 0
        self._c = 10000
        self.api_calls = 0

    # metadata
    def project_issue_types(self, project):
        return [{"name": "Task", "subtask": False}, {"name": "Subtask", "subtask": True}]

    def link_type_names(self):
        return ["Blocks", "Relates"]

    # issues
    def bulk_create(self, issue_fields):
        out = []
        for fields in issue_fields:
            self._n += 1
            key = f"{fields['project']['key']}-{self._n}"
            self.issues[key] = fields
            self.comments[key] = []
            self.status[key] = "To Do"
            out.append({"id": str(self._n), "key": key})
        return {"issues": out, "errors": []}

    def add_foreign_issue(self, key, labels, project=None):
        self.issues[key] = {"project": {"key": project or key.split("-")[0]}, "summary": "foreign",
                            "description": None, "labels": labels}
        self.comments[key] = []
        self.status[key] = "To Do"

    def _render(self, key, fields=None):
        f = self.issues[key]
        links = []
        for a, b in self.links:
            if a == key:
                links.append({"outwardIssue": self._link_view(b)})
            if b == key:
                links.append({"inwardIssue": self._link_view(a)})
        return {"key": key, "fields": {
            "summary": f["summary"], "description": f["description"], "labels": f["labels"],
            "project": {"key": f["project"]["key"]}, "status": {"name": self.status[key]},
            "issuelinks": links, "comment": {"total": len(self.comments[key])}}}

    def _link_view(self, key):
        return {"key": key, "fields": {"summary": self.issues[key]["summary"],
                                       "status": {"name": self.status[key]}}}

    def get_issue(self, key, fields):
        if key not in self.issues:
            raise JiraError("not found", status=404)
        return self._render(key)

    def scoped_search(self, project, labels, *, text=None, fields=None, max_results=None):
        hits = []
        for key, f in self.issues.items():
            if f["project"]["key"] != project or not all(l in f["labels"] for l in labels):
                continue
            if text:
                blob = (f["summary"] + " " + str(f["description"]) + " "
                        + " ".join(str(c["body"]) for c in self.comments[key])).lower()
                if text.lower() not in blob:
                    continue
            hits.append(self._render(key))
        hits.sort(key=lambda i: int(i["key"].split("-")[1]))
        return hits[:max_results] if max_results else hits

    def get_comments(self, key, start_at=0, max_results=100):
        all_ = self.comments[key]
        return {"comments": all_[start_at:start_at + max_results], "total": len(all_),
                "startAt": start_at, "maxResults": max_results}

    def add_comment(self, key, adf_body):
        self._c += 1
        self.comments[key].append({"id": str(self._c), "body": adf_body})
        return {"id": str(self._c)}

    def create_link(self, type_name, from_key, to_key):
        self.links.append((from_key, to_key))

    def transitions(self, key):
        return [{"id": str(i), "to": {"name": n}} for i, n in enumerate(["To Do", "In Progress", "Done"])]

    def do_transition(self, key, transition_id):
        self.status[key] = ["To Do", "In Progress", "Done"][int(transition_id)]


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_use(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def response(blocks, stop_reason="tool_use"):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=usage, model="fake-model")


class ScriptedLLM:
    """Plays back a list of responses; a callable entry receives the request kwargs."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.messages = self
        self.beta = SimpleNamespace(messages=self)

    def create(self, **kwargs):
        kwargs["messages"] = list(kwargs["messages"])  # snapshot: the agent appends in place
        self.requests.append(kwargs)
        item = self.script.pop(0)
        return item(kwargs) if callable(item) else item
