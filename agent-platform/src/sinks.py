"""Bounded record sinks for exportable service records."""

from __future__ import annotations

import itertools
from collections import OrderedDict
from collections.abc import Hashable
from typing import Protocol


class RecordSink(Protocol):
    """Destination for records that should leave business-owned memory."""

    def publish(self, record: object, *, key: Hashable | None = None) -> None:
        """Accept a record for export."""


class NoopBoundedSink:
    """No-op sink with bounded in-memory retention.

    Future implementations can publish to Kafka, a database, object storage, or
    an observability collector while preserving this small interface.
    """

    def __init__(self, max_entries: int):
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._sequence = itertools.count()
        self._records: OrderedDict[Hashable, object] = OrderedDict()

    def publish(self, record: object, *, key: Hashable | None = None) -> None:
        record_key = next(self._sequence) if key is None else key
        self._records[record_key] = record
        self._records.move_to_end(record_key)
        while len(self._records) > self._max_entries:
            self._records.popitem(last=False)

    def get(self, key: Hashable) -> object | None:
        return self._records.get(key)

    def clear(self) -> None:
        self._records.clear()

    def snapshot(self) -> list[object]:
        return list(self._records.values())

    def __len__(self) -> int:
        return len(self._records)
