"""Investigation trace (JSONL) and usage accounting. Thread-safe."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

# USD per million tokens (input, output). Used only for a labelled cost ESTIMATE.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.00, 40.00),
}


class Tracer:
    def __init__(self, path: str | Path | None, append: bool = False):
        self._lock = threading.Lock()
        self._seq = 0
        self.events: list[dict] = []
        self._fh = None
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if append and Path(path).exists():  # resumed run: keep the earlier events
                with open(path, encoding="utf-8") as fh:
                    self.events = [json.loads(line) for line in fh if line.strip()]
                self._seq = max((e["seq"] for e in self.events), default=0)
            self._fh = open(path, "a" if append else "w", encoding="utf-8")

    def event(self, scope: str, kind: str, **data: Any) -> None:
        with self._lock:
            self._seq += 1
            record = {"seq": self._seq, "ts": round(time.time(), 3), "scope": scope,
                      "event": kind, **data}
            self.events.append(record)
            if self._fh:
                self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


class Usage:
    FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.model_calls = 0
        self.tool_calls = 0
        self.tokens = {f: 0 for f in self.FIELDS}
        self.by_model: dict[str, dict[str, int]] = {}

    @classmethod
    def from_dict(cls, data: dict) -> "Usage":
        usage = cls()
        usage.model_calls = int(data.get("model_calls", 0))
        usage.tool_calls = int(data.get("tool_calls", 0))
        usage.tokens = {f: int(data.get(f, 0)) for f in cls.FIELDS}
        usage.by_model = {m: {f: int(c.get(f, 0)) for f in cls.FIELDS}
                          for m, c in (data.get("by_model") or {}).items()}
        return usage

    def add_response(self, response: Any) -> dict:
        usage = getattr(response, "usage", None)
        counts = {f: int(getattr(usage, f, 0) or 0) for f in self.FIELDS}
        model = getattr(response, "model", "unknown") or "unknown"
        with self._lock:
            self.model_calls += 1
            per_model = self.by_model.setdefault(model, {f: 0 for f in self.FIELDS})
            for f, n in counts.items():
                self.tokens[f] += n
                per_model[f] += n
        return counts

    def add_tool_call(self) -> None:
        with self._lock:
            self.tool_calls += 1

    def merge(self, other: "Usage") -> None:
        with self._lock:
            self.model_calls += other.model_calls
            self.tool_calls += other.tool_calls
            for f in self.FIELDS:
                self.tokens[f] += other.tokens[f]
            for model, counts in other.by_model.items():
                mine = self.by_model.setdefault(model, {f: 0 for f in self.FIELDS})
                for f in self.FIELDS:
                    mine[f] += counts[f]

    def estimated_cost_usd(self) -> float | None:
        total = 0.0
        for model, c in self.by_model.items():
            price = next((p for name, p in PRICES.items() if model.startswith(name)), None)
            if price is None:
                return None
            inp, out = price
            total += (c["input_tokens"] * inp + c["cache_creation_input_tokens"] * inp * 1.25
                      + c["cache_read_input_tokens"] * inp * 0.1 + c["output_tokens"] * out) / 1e6
        return round(total, 4)

    def as_dict(self) -> dict:
        return {"model_calls": self.model_calls, "tool_calls": self.tool_calls,
                **self.tokens,
                "total_tokens": sum(self.tokens.values()),
                "by_model": self.by_model,
                "estimated_cost_usd": self.estimated_cost_usd()}
