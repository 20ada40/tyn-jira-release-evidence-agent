"""Thin Jira Cloud REST v3 client with retries. No business logic lives here."""
from __future__ import annotations

import threading
import time
from typing import Any

import requests


class JiraError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def jql_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str,
                 timeout: float = 30.0, max_retries: int = 6):
        self.base_url = base_url.rstrip("/")
        self._auth = (email, api_token)
        self._timeout = timeout
        self._max_retries = max_retries
        self._local = threading.local()
        self._lock = threading.Lock()
        self.api_calls = 0

    # -- transport ---------------------------------------------------------
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.auth = self._auth
            s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
            self._local.session = s
        return s

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        last_error = "unknown error"
        for attempt in range(self._max_retries):
            with self._lock:
                self.api_calls += 1
            try:
                resp = self._session().request(method, url, params=params, json=body,
                                               timeout=self._timeout)
            except requests.RequestException as exc:  # network failure: retry
                last_error = f"network error: {exc}"
                time.sleep(min(2 ** attempt, 20))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 20)
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise JiraError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:600]}",
                                status=resp.status_code)
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise JiraError(f"{method} {path} failed after {self._max_retries} attempts ({last_error})")

    # -- metadata ----------------------------------------------------------
    def myself(self) -> dict:
        return self._request("GET", "/rest/api/3/myself")

    def project_issue_types(self, project: str) -> list[dict]:
        data = self._request("GET", f"/rest/api/3/project/{project}")
        return data.get("issueTypes", [])

    def link_type_names(self) -> list[str]:
        data = self._request("GET", "/rest/api/3/issueLinkType")
        return [t["name"] for t in data.get("issueLinkTypes", [])]

    # -- search ------------------------------------------------------------
    def scoped_search(self, project: str, labels: list[str], *, text: str | None = None,
                      fields: list[str] | None = None, max_results: int | None = None) -> list[dict]:
        """Search issues that are in `project` AND carry every label in `labels`."""
        clauses = [f"project = {jql_quote(project)}"]
        clauses += [f"labels = {jql_quote(label)}" for label in labels]
        if text:
            clauses.append(f"text ~ {jql_quote(text)}")
        jql = " AND ".join(clauses) + " ORDER BY key ASC"
        issues: list[dict] = []
        token: str | None = None
        while True:
            page_size = 100 if max_results is None else max(1, min(100, max_results - len(issues)))
            body: dict[str, Any] = {"jql": jql, "maxResults": page_size,
                                    "fields": fields or ["summary", "status", "labels"]}
            if token:
                body["nextPageToken"] = token
            data = self._request("POST", "/rest/api/3/search/jql", body=body)
            issues.extend(data.get("issues", []))
            token = data.get("nextPageToken")
            if not token or data.get("isLast") or (max_results is not None and len(issues) >= max_results):
                return issues[:max_results] if max_results is not None else issues

    # -- issues ------------------------------------------------------------
    def bulk_create(self, issue_fields: list[dict]) -> dict:
        body = {"issueUpdates": [{"fields": f} for f in issue_fields]}
        return self._request("POST", "/rest/api/3/issue/bulk", body=body)

    def get_issue(self, key: str, fields: list[str]) -> dict:
        return self._request("GET", f"/rest/api/3/issue/{key}", params={"fields": ",".join(fields)})

    def get_comments(self, key: str, start_at: int = 0, max_results: int = 100) -> dict:
        return self._request("GET", f"/rest/api/3/issue/{key}/comment",
                             params={"startAt": start_at, "maxResults": max_results,
                                     "orderBy": "created"})

    def add_comment(self, key: str, adf_body: dict) -> dict:
        return self._request("POST", f"/rest/api/3/issue/{key}/comment", body={"body": adf_body})

    def create_link(self, type_name: str, from_key: str, to_key: str) -> None:
        """Create a directed link: reading `from_key` shows `to_key` as its outwardIssue."""
        self._request("POST", "/rest/api/3/issueLink",
                      body={"type": {"name": type_name},
                            "inwardIssue": {"key": from_key},
                            "outwardIssue": {"key": to_key}})

    def transitions(self, key: str) -> list[dict]:
        return self._request("GET", f"/rest/api/3/issue/{key}/transitions").get("transitions", [])

    def do_transition(self, key: str, transition_id: str) -> None:
        self._request("POST", f"/rest/api/3/issue/{key}/transitions",
                      body={"transition": {"id": transition_id}})
