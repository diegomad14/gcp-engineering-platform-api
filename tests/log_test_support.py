"""Synthetic clocks and versioned Firestore for offline runtime-log tests."""

from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from types import SimpleNamespace

from google.api_core.exceptions import (
    AlreadyExists,
    FailedPrecondition,
    DeadlineExceeded,
)

from eng_platform_api.services import log_budget as budget


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FirestoreDouble:
    def __init__(self):
        self.now = datetime(2026, 10, 4, tzinfo=timezone.utc)
        self.records = {}
        self.versions = {}
        self.conflicts = 0
        self.ambiguous = False
        self.lock = RLock()
        self.reads = 0
        self.writes = 0
        self.fail = False
        self.clock = None
        self.latency = 0
        self.timeouts = []

    def delay(self, timeout):
        assert 0 < timeout <= budget.COORDINATION_TIMEOUT_SECONDS
        self.timeouts.append(timeout)
        if self.clock:
            self.clock.advance(min(self.latency, timeout))
            if self.latency > timeout:
                raise DeadlineExceeded("synthetic coordination latency")

    def collection(self, name):
        assert name == "eng_platform_log_budget"
        return self

    def document(self, name):
        store = self

        class Document:
            def get(self, *, retry, timeout):
                assert retry is None
                store.delay(timeout)
                with store.lock:
                    if store.fail:
                        raise RuntimeError("Firestore unavailable")
                    store.reads += 1
                    value = deepcopy(store.records.get(name))
                    version = store.versions.get(name)
                    return SimpleNamespace(
                        read_time=store.now,
                        exists=name in store.records,
                        update_time=version,
                        to_dict=lambda: value,
                    )

            def create(self, record, *, retry, timeout):
                assert retry is None
                store.delay(timeout)
                with store.lock:
                    if name in store.records:
                        raise AlreadyExists("raced")
                    self.save(record)

            def update(self, record, *, option, retry, timeout):
                assert retry is None
                store.delay(timeout)
                with store.lock:
                    if store.conflicts:
                        store.conflicts -= 1
                        raise FailedPrecondition("raced")
                    if option != store.versions.get(name):
                        raise FailedPrecondition("raced")
                    self.save(record)

            def save(self, record):
                store.writes += 1
                store.records[name] = deepcopy(record)
                store.versions[name] = store.writes
                if store.ambiguous:
                    raise DeadlineExceeded("ambiguous committed write")

        return Document()

    def write_option(self, *, last_update_time):
        return last_update_time


class SharedFirestoreDouble:
    """Versioned CAS shared by independent interpreters through local SQLite.

    This is a Firestore test adapter, NOT a production coordination fallback.
    SQLite-backed records, versions, server time and metrics are genuine shared
    state. No fork-inherited dictionaries or sockets are needed by this test.
    """

    def __init__(self, path, *, initialize=False, processes=1):
        self.path = str(path)
        self.processes = processes
        self._first_write = True
        if initialize:
            with self.connection() as db:
                db.execute(
                    "CREATE TABLE records (name TEXT PRIMARY KEY, value TEXT, version INTEGER)"
                )
                db.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
                db.executemany(
                    "INSERT INTO metadata VALUES (?, ?)",
                    [
                        ("now", datetime(2026, 10, 4, tzinfo=timezone.utc).isoformat()),
                        ("writes", "0"),
                        ("conflicts", "0"),
                        ("ready", "0"),
                    ],
                )

    def connection(self):
        # Return a context that commits AND closes the local connection.
        from contextlib import contextmanager
        import sqlite3

        @contextmanager
        def opened():
            db = sqlite3.connect(self.path, timeout=30)
            try:
                with db:
                    yield db
            finally:
                db.close()

        return opened()

    @staticmethod
    def decode(value):
        import json

        def dates(obj):
            if set(obj) == {"__datetime__"}:
                return datetime.fromisoformat(obj["__datetime__"])
            return obj

        return json.loads(value, object_hook=dates)

    def metric(self, name):
        with self.connection() as db:
            return int(
                db.execute(
                    "SELECT value FROM metadata WHERE name = ?", (name,)
                ).fetchone()[0]
            )

    @property
    def records(self):
        with self.connection() as db:
            return {
                name: self.decode(value)
                for name, value in db.execute("SELECT name, value FROM records")
            }

    def synchronize_write(self):
        from time import monotonic, sleep

        if not self._first_write:
            return
        self._first_write = False
        with self.connection() as db:
            db.execute("UPDATE metadata SET value = value + 1 WHERE name = 'ready'")
        expires = monotonic() + 30
        while self.metric("ready") < self.processes:
            if monotonic() > expires:
                raise TimeoutError("Shared CAS test barrier timed out")
            sleep(0.005)

    def collection(self, name):
        assert name == "eng_platform_log_budget"
        return self

    def document(self, name):
        import json
        import sqlite3

        store = self

        class Document:
            def get(self, *, retry, timeout):
                assert (
                    retry is None and 0 < timeout <= budget.COORDINATION_TIMEOUT_SECONDS
                )
                with store.connection() as db:
                    db.execute("BEGIN")
                    row = db.execute(
                        "SELECT value, version FROM records WHERE name = ?", (name,)
                    ).fetchone()
                    now = datetime.fromisoformat(
                        db.execute(
                            "SELECT value FROM metadata WHERE name = 'now'"
                        ).fetchone()[0]
                    )
                value = store.decode(row[0]) if row else None
                return SimpleNamespace(
                    read_time=now,
                    exists=row is not None,
                    update_time=row[1] if row else None,
                    to_dict=lambda: value,
                )

            def create(self, record, *, retry, timeout):
                assert (
                    retry is None and 0 < timeout <= budget.COORDINATION_TIMEOUT_SECONDS
                )
                self.save(record, None)

            def update(self, record, *, option, retry, timeout):
                assert (
                    retry is None and 0 < timeout <= budget.COORDINATION_TIMEOUT_SECONDS
                )
                self.save(record, option)

            def save(self, record, version):
                store.synchronize_write()
                value = json.dumps(
                    record, default=lambda item: {"__datetime__": item.isoformat()}
                )
                conflict = False
                with store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if version is None:
                        try:
                            db.execute(
                                "INSERT INTO records VALUES (?, ?, 1)", (name, value)
                            )
                        except sqlite3.IntegrityError:
                            conflict = True
                    else:
                        result = db.execute(
                            "UPDATE records SET value = ?, version = version + 1 WHERE name = ? AND version = ?",
                            (value, name, version),
                        )
                        conflict = result.rowcount != 1
                    metric = "conflicts" if conflict else "writes"
                    db.execute(
                        "UPDATE metadata SET value = value + 1 WHERE name = ?",
                        (metric,),
                    )
                if conflict:
                    error = AlreadyExists if version is None else FailedPrecondition
                    raise error("shared CAS race")

        return Document()

    def write_option(self, *, last_update_time):
        return last_update_time
