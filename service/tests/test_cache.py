"""The Firestore cache, including the Firestore path itself via a fake client."""

import pytest

from service.cache import (
    FirestoreFactStore,
    MemoryFactStore,
    cache_key,
)


def test_cache_key_normalises_case_and_slashes():
    assert cache_key("AMS1117-3.3") == "ams1117-3.3"
    assert cache_key("LM317/NOPB") == "lm317_nopb"
    assert cache_key("  STM32F103  ") == "stm32f103"


def test_cache_key_rejects_empty():
    with pytest.raises(ValueError, match="cannot be empty"):
        cache_key("   ")


def test_same_part_different_case_is_one_entry():
    store = MemoryFactStore()
    store.put("STM32F103", {"pins": 48})
    assert store.get("stm32f103") == {"pins": 48}


def test_memory_store_counts_hits_and_misses():
    store = MemoryFactStore()
    assert store.get("nope") is None
    store.put("a", {"x": 1})
    assert store.get("a") == {"x": 1}
    assert (store.hits, store.misses) == (1, 1)


def test_stored_facts_are_copied_not_aliased():
    store = MemoryFactStore()
    facts = {"pins": 3}
    store.put("p", facts)
    facts["pins"] = 999
    assert store.get("p") == {"pins": 3}


class FakeDoc:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class FakeRef:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def get(self):
        return FakeDoc(self.store.get(self.key))

    def set(self, payload):
        self.store[self.key] = payload


class FakeCollection:
    def __init__(self, store):
        self.store = store

    def document(self, key):
        return FakeRef(self.store, key)


class FakeClient:
    def __init__(self):
        self.data = {}
        self.collections = []

    def collection(self, name):
        self.collections.append(name)
        return FakeCollection(self.data)


def test_firestore_store_round_trips():
    client = FakeClient()
    store = FirestoreFactStore(client=client)
    assert store.get("AMS1117-3.3") is None

    store.put("AMS1117-3.3", {"pins": {"1": "GND"}})
    assert store.get("ams1117-3.3") == {"pins": {"1": "GND"}}
    assert client.collections[0] == "datasheet_facts"


def test_firestore_store_records_metadata():
    client = FakeClient()
    FirestoreFactStore(client=client).put("LM317", {"a": 1})
    doc = client.data["lm317"]
    assert doc["part"] == "LM317" and doc["cached_at"] > 0
