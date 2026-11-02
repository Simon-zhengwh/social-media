from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterator

from pymongo.collection import Collection
from pymongo.results import DeleteResult, InsertManyResult, InsertOneResult, UpdateResult


GLOBAL_COLLECTIONS = frozenset({"app_control"})
MUTATING_METHODS = frozenset({"insert_one", "insert_many", "replace_one", "update_one", "update_many", "delete_one", "delete_many"})


def _scoped_filter(query: dict[str, Any] | None, merchant_id: str) -> dict[str, Any]:
    if not query:
        return {"merchant_id": merchant_id}
    if not any(str(key).startswith("$") for key in query):
        value = deepcopy(query)
        value["merchant_id"] = merchant_id
        return value
    return {"$and": [query, {"merchant_id": merchant_id}]}


def _scoped_document(document: dict[str, Any], merchant_id: str) -> dict[str, Any]:
    value = deepcopy(document)
    value["merchant_id"] = merchant_id
    return value


def _scoped_update(update: dict[str, Any], merchant_id: str, *, replacement: bool = False) -> dict[str, Any]:
    value = deepcopy(update)
    if replacement:
        value["merchant_id"] = merchant_id
        return value
    set_values = value.get("$set")
    if isinstance(set_values, dict) and set_values.get("merchant_id") == merchant_id:
        return value
    set_on_insert = value.setdefault("$setOnInsert", {})
    if not isinstance(set_on_insert, dict):
        raise ValueError("$setOnInsert must be an object")
    set_on_insert["merchant_id"] = merchant_id
    return value


class ScopedCollection:
    """Applies merchant_id to every normal read/write without relying on browser input."""

    def __init__(self, collection: Collection, merchant_id: str) -> None:
        self.raw = collection
        self.merchant_id = merchant_id

    def find(self, query: dict[str, Any] | None = None, *args: Any, **kwargs: Any) -> Any:
        return self.raw.find(_scoped_filter(query, self.merchant_id), *args, **kwargs)

    def find_one(self, query: dict[str, Any] | None = None, *args: Any, **kwargs: Any) -> Any:
        return self.raw.find_one(_scoped_filter(query, self.merchant_id), *args, **kwargs)

    def find_one_and_update(self, query: dict[str, Any], update: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        upsert = bool(kwargs.get("upsert"))
        return self.raw.find_one_and_update(
            _scoped_filter(query, self.merchant_id),
            _scoped_update(update, self.merchant_id),
            *args,
            **kwargs,
        ) if upsert else self.raw.find_one_and_update(
            _scoped_filter(query, self.merchant_id), update, *args, **kwargs
        )

    def find_one_and_replace(self, query: dict[str, Any], replacement: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        return self.raw.find_one_and_replace(
            _scoped_filter(query, self.merchant_id),
            _scoped_document(replacement, self.merchant_id),
            *args,
            **kwargs,
        )

    def insert_one(self, document: dict[str, Any], *args: Any, **kwargs: Any) -> InsertOneResult:
        return self.raw.insert_one(_scoped_document(document, self.merchant_id), *args, **kwargs)

    def insert_many(self, documents: list[dict[str, Any]], *args: Any, **kwargs: Any) -> InsertManyResult:
        return self.raw.insert_many([_scoped_document(doc, self.merchant_id) for doc in documents], *args, **kwargs)

    def replace_one(self, query: dict[str, Any], replacement: dict[str, Any], *args: Any, **kwargs: Any) -> UpdateResult:
        return self.raw.replace_one(
            _scoped_filter(query, self.merchant_id),
            _scoped_document(replacement, self.merchant_id),
            *args,
            **kwargs,
        )

    def update_one(self, query: dict[str, Any], update: dict[str, Any], *args: Any, **kwargs: Any) -> UpdateResult:
        upsert = bool(kwargs.get("upsert"))
        return self.raw.update_one(
            _scoped_filter(query, self.merchant_id),
            _scoped_update(update, self.merchant_id) if upsert else update,
            *args,
            **kwargs,
        )

    def update_many(self, query: dict[str, Any], update: dict[str, Any], *args: Any, **kwargs: Any) -> UpdateResult:
        upsert = bool(kwargs.get("upsert"))
        return self.raw.update_many(
            _scoped_filter(query, self.merchant_id),
            _scoped_update(update, self.merchant_id) if upsert else update,
            *args,
            **kwargs,
        )

    def delete_one(self, query: dict[str, Any], *args: Any, **kwargs: Any) -> DeleteResult:
        return self.raw.delete_one(_scoped_filter(query, self.merchant_id), *args, **kwargs)

    def delete_many(self, query: dict[str, Any], *args: Any, **kwargs: Any) -> DeleteResult:
        return self.raw.delete_many(_scoped_filter(query, self.merchant_id), *args, **kwargs)

    def count_documents(self, query: dict[str, Any], *args: Any, **kwargs: Any) -> int:
        return self.raw.count_documents(_scoped_filter(query, self.merchant_id), *args, **kwargs)

    def distinct(self, key: str, query: dict[str, Any] | None = None, *args: Any, **kwargs: Any) -> list[Any]:
        return self.raw.distinct(key, _scoped_filter(query, self.merchant_id), *args, **kwargs)

    def aggregate(self, pipeline: list[dict[str, Any]], *args: Any, **kwargs: Any) -> Any:
        scoped = [{"$match": {"merchant_id": self.merchant_id}}, *pipeline]
        return self.raw.aggregate(scoped, *args, **kwargs)

    def create_index(self, *args: Any, **kwargs: Any) -> str:
        return self.raw.create_index(*args, **kwargs)

    def create_indexes(self, *args: Any, **kwargs: Any) -> list[str]:
        return self.raw.create_indexes(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)


class ScopedDatabase:
    def __init__(self, raw: Any, merchant_id: str) -> None:
        self.raw = raw
        self.merchant_id = merchant_id
        self._collections: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in GLOBAL_COLLECTIONS:
            return getattr(self.raw, name)
        return self[name]

    def __getitem__(self, name: str) -> Any:
        if name in GLOBAL_COLLECTIONS:
            return self.raw[name]
        if name not in self._collections:
            self._collections[name] = ScopedCollection(self.raw[name], self.merchant_id)
        return self._collections[name]

    def command(self, *args: Any, **kwargs: Any) -> Any:
        return self.raw.command(*args, **kwargs)


def iter_scoped_databases(raw: Any, collection_name: str, query: dict[str, Any]) -> Iterator[tuple[str, ScopedDatabase]]:
    merchant_ids = raw[collection_name].distinct("merchant_id", query)
    for merchant_id in sorted(str(value) for value in merchant_ids if value):
        yield merchant_id, ScopedDatabase(raw, merchant_id)
