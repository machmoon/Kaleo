"""Firestore-backed cache for extracted datasheet facts.

Reading a 300-page datasheet is the slowest and most expensive step in the
pipeline, and its result is a pure function of the part number. So the second
run on a part should be free -- across processes, across instances, and across
users, which rules out an in-process dict on a Cloud Run container that gets
torn down between requests.

Firestore is behind a :class:`FactStore` protocol for the same reason the model
is: :class:`MemoryFactStore` makes every caller testable with no network and no
GCP project.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = ["FactStore", "FirestoreFactStore", "MemoryFactStore", "cache_key"]

COLLECTION = "datasheet_facts"


def cache_key(part_number: str) -> str:
    """Normalise a part number into a document id.

    Firestore ids cannot contain '/', and part numbers routinely do
    ("AMS1117-3.3", "LM317/NOPB"). Case is folded because a datasheet lookup
    for 'stm32f103' and 'STM32F103' is the same lookup.
    """
    cleaned = part_number.strip().lower().replace("/", "_")
    if not cleaned:
        raise ValueError("part_number cannot be empty")
    return cleaned


class FactStore(Protocol):
    """Anything that can remember what was read about a part."""

    def get(self, part_number: str) -> dict[str, Any] | None: ...

    def put(self, part_number: str, facts: dict[str, Any]) -> None: ...


@dataclass
class MemoryFactStore:
    """In-process store. The offline path, and the one tests use."""

    data: dict[str, dict[str, Any]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get(self, part_number: str) -> dict[str, Any] | None:
        found = self.data.get(cache_key(part_number))
        if found is None:
            self.misses += 1
        else:
            self.hits += 1
        return found

    def put(self, part_number: str, facts: dict[str, Any]) -> None:
        self.data[cache_key(part_number)] = dict(facts)


class FirestoreFactStore:
    """The deployed path. Requires a Firestore database in the project."""

    def __init__(self, collection: str = COLLECTION, *, client: Any = None,
                 project: str | None = None):
        if client is None:
            try:
                from google.cloud import firestore
            except ImportError as exc:  # pragma: no cover - import guard
                raise RuntimeError(
                    "google-cloud-firestore is not installed. "
                    'pip install -e ".[cloud]"'
                ) from exc
            client = firestore.Client(project=project)
        self._client = client
        self.collection = collection

    def get(self, part_number: str) -> dict[str, Any] | None:
        doc = self._client.collection(self.collection).document(
            cache_key(part_number)
        ).get()
        if not getattr(doc, "exists", False):
            return None
        payload = doc.to_dict() or {}
        return payload.get("facts")

    def put(self, part_number: str, facts: dict[str, Any]) -> None:
        self._client.collection(self.collection).document(
            cache_key(part_number)
        ).set({"facts": facts, "cached_at": time.time(), "part": part_number})
