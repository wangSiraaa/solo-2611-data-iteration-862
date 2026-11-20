"""SQLite persistence for the semantics service and the delivery library.

Design notes
------------
* Observations are append-only records keyed by ``(sample_id, revision)``;
  the *effective* observation is simply the highest revision.  An admission
  batch has its own identity (``batch_digest``) so identical replays are
  recognised without rewriting anything.
* References are immutable per version; a derived version stores its parent
  so "copy old complete version, replace listed items" is reproducible.
* Reports, semantic products and deliveries are immutable rows with a content
  digest.  Replaying identical content returns the stored row; different
  content under the same id raises a conflict.
* Failed deliveries never touch ``deliveries``; they are only audited in
  ``delivery_attempts`` (audit rows are not a successful identity).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS obs_admissions (
    admission_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_digest  TEXT NOT NULL UNIQUE,
    accepted      INTEGER NOT NULL,
    outcome       TEXT NOT NULL,
    canonical     TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS obs_batch_member (
    admission_id  INTEGER NOT NULL REFERENCES obs_admissions(admission_id),
    sample_id     TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    bits          TEXT NOT NULL,
    disposition   TEXT NOT NULL,
    PRIMARY KEY (admission_id, sample_id, revision)
);

CREATE TABLE IF NOT EXISTS observations (
    sample_id     TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    bits          TEXT NOT NULL,
    raw           INTEGER NOT NULL,
    admission_id  INTEGER NOT NULL,
    PRIMARY KEY (sample_id, revision)
);

CREATE TABLE IF NOT EXISTS reference_version (
    version         TEXT PRIMARY KEY,
    parent_version  TEXT REFERENCES reference_version(version),
    items_json      TEXT NOT NULL,
    content_digest  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reference_item (
    version     TEXT NOT NULL REFERENCES reference_version(version),
    sample_id   TEXT NOT NULL,
    expected    TEXT NOT NULL,
    tolerance   TEXT NOT NULL,
    PRIMARY KEY (version, sample_id)
);

CREATE TABLE IF NOT EXISTS reports (
    report_id          TEXT PRIMARY KEY,
    reference_version  TEXT NOT NULL REFERENCES reference_version(version),
    content_digest     TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_item (
    report_id          TEXT NOT NULL REFERENCES reports(report_id),
    sample_id          TEXT NOT NULL,
    received_revision  INTEGER,
    original_bits      TEXT,
    status             TEXT NOT NULL,
    kind               TEXT NOT NULL,
    value_sign         INTEGER,
    exact_value        TEXT,
    expected           TEXT NOT NULL,
    tolerance          TEXT NOT NULL,
    difference         TEXT,
    abs_difference     TEXT,
    canonical_json     TEXT NOT NULL,
    PRIMARY KEY (report_id, sample_id)
);

CREATE TABLE IF NOT EXISTS semantic_products (
    product_id         TEXT PRIMARY KEY,
    reference_version  TEXT NOT NULL REFERENCES reference_version(version),
    sample_ids_json    TEXT NOT NULL,
    content_digest     TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_product_item (
    product_id         TEXT NOT NULL REFERENCES semantic_products(product_id),
    sample_id          TEXT NOT NULL,
    received_revision  INTEGER,
    original_bits      TEXT,
    status             TEXT NOT NULL,
    kind               TEXT NOT NULL,
    value_sign         INTEGER,
    exact_value        TEXT,
    expected           TEXT NOT NULL,
    tolerance          TEXT NOT NULL,
    difference         TEXT,
    abs_difference     TEXT,
    canonical_json     TEXT NOT NULL,
    PRIMARY KEY (product_id, sample_id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id        TEXT PRIMARY KEY,
    product_id         TEXT NOT NULL REFERENCES semantic_products(product_id),
    reference_version  TEXT NOT NULL,
    sample_ids_json    TEXT NOT NULL,
    contract_json      TEXT NOT NULL,
    contract_id        TEXT NOT NULL,
    request_digest     TEXT NOT NULL,
    lines_json         TEXT NOT NULL,
    content_digest     TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_line (
    delivery_id        TEXT NOT NULL REFERENCES deliveries(delivery_id),
    ordinal            INTEGER NOT NULL,
    sample_id          TEXT NOT NULL,
    original_bits      TEXT,
    kind               TEXT NOT NULL,
    value_sign         INTEGER,
    exact_value        TEXT,
    status             TEXT NOT NULL,
    delivered_status   TEXT NOT NULL,
    coefficient        INTEGER,
    scale              INTEGER,
    error              TEXT,
    reader_value       TEXT,
    evidence_json      TEXT NOT NULL,
    PRIMARY KEY (delivery_id, sample_id)
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    attempt_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id     TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    request_digest  TEXT NOT NULL,
    status          TEXT NOT NULL,
    reasons_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None  # explicit transactions
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
