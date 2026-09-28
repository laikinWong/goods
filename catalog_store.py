"""SQLite-backed product index shared by the crawler implementations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union


VALID_STATUSES = {"pending", "partial", "success", "failed"}
SQLITE_BATCH_SIZE = 500


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_database_path(default: Path) -> Path:
    """Return the orchestrator-provided DB path, or a crawler-local default."""
    configured = os.environ.get("GOODS_CATALOG_DB")
    return Path(configured) if configured else default


def _chunks(values: Sequence[str], size: int = SQLITE_BATCH_SIZE) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


class CatalogStore:
    """Durable product status and payload repository.

    Network operations must happen outside this class' transactions. Methods use
    short transactions so the three crawler processes can safely share one WAL DB.
    """

    def __init__(self, path: Union[Path, str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CatalogStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    source TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    category_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'partial', 'success', 'failed')),
                    payload_json TEXT,
                    content_hash TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    fetched_at TEXT,
                    first_seen_run_id TEXT,
                    last_seen_run_id TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    PRIMARY KEY (source, product_id)
                );

                CREATE INDEX IF NOT EXISTS idx_products_source_status
                    ON products(source, status);
                CREATE INDEX IF NOT EXISTS idx_products_source_category
                    ON products(source, category_id);
                CREATE INDEX IF NOT EXISTS idx_products_first_seen_run
                    ON products(source, first_seen_run_id);

                CREATE TABLE IF NOT EXISTS crawl_runs (
                    run_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    scanned_count INTEGER NOT NULL DEFAULT 0,
                    new_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_crawl_runs_source_started
                    ON crawl_runs(source, started_at);

                CREATE TABLE IF NOT EXISTS crawl_run_products (
                    run_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    processed_job_id TEXT,
                    processed_at TEXT,
                    PRIMARY KEY (run_id, source, product_id)
                );

                CREATE INDEX IF NOT EXISTS idx_crawl_run_products_pending
                    ON crawl_run_products(source, processed_job_id, product_id);
                """
            )

    def start_run(self, source: str) -> str:
        run_id = f"{source}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        with self.connection:
            self.connection.execute(
                "INSERT INTO crawl_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'running')",
                (run_id, source, utc_now()),
            )
        return run_id

    def finish_run(self, run_id: str, status: str = "success") -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE crawl_runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, utc_now(), run_id),
            )

    def increment_run(self, run_id: str, **counts: int) -> None:
        allowed = {"scanned_count", "new_count", "skipped_count", "failed_count"}
        updates = [(name, int(value)) for name, value in counts.items() if name in allowed and value]
        if not updates:
            return
        assignments = ", ".join(f"{name} = {name} + ?" for name, _ in updates)
        values = [value for _, value in updates]
        with self.connection:
            self.connection.execute(
                f"UPDATE crawl_runs SET {assignments} WHERE run_id = ?",  # noqa: S608 - fixed allowlist.
                (*values, run_id),
            )

    def source_count(self, source: str, status: Optional[str] = None) -> int:
        if status is None:
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM products WHERE source = ?", (source,)
            ).fetchone()
        else:
            if status not in VALID_STATUSES:
                raise ValueError(f"Invalid product status: {status}")
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM products WHERE source = ? AND status = ?",
                (source, status),
            ).fetchone()
        return int(row["count"])

    def ids_with_statuses(
        self,
        source: str,
        product_ids: Iterable[Any],
        statuses: Sequence[str] = ("success",),
    ) -> set[str]:
        normalized = list(
            dict.fromkeys(
                str(value) for value in product_ids if value is not None and str(value)
            )
        )
        if not normalized:
            return set()
        if not statuses or any(status not in VALID_STATUSES for status in statuses):
            raise ValueError("statuses must contain valid product statuses")

        found: set[str] = set()
        status_placeholders = ",".join("?" for _ in statuses)
        for chunk in _chunks(normalized):
            id_placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"""
                SELECT product_id FROM products
                WHERE source = ?
                  AND status IN ({status_placeholders})
                  AND product_id IN ({id_placeholders})
                """,  # noqa: S608 - placeholders only.
                (source, *statuses, *chunk),
            )
            found.update(str(row["product_id"]) for row in rows)
        return found

    def register_discovered(
        self,
        source: str,
        records: Iterable[Tuple[Any, Optional[Any]]],
        run_id: str,
    ) -> None:
        now = utc_now()
        rows = [
            (source, str(product_id), None if category_id is None else str(category_id), now, now, run_id, run_id)
            for product_id, category_id in records
            if product_id is not None and str(product_id)
        ]
        if not rows:
            return
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO products(
                    source, product_id, category_id, status,
                    first_seen_at, last_seen_at, first_seen_run_id, last_seen_run_id
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
                ON CONFLICT(source, product_id) DO UPDATE SET
                    category_id = COALESCE(excluded.category_id, products.category_id),
                    last_seen_at = excluded.last_seen_at,
                    last_seen_run_id = excluded.last_seen_run_id
                """,
                rows,
            )
        self.increment_run(run_id, scanned_count=len(rows))

    def mark_success(
        self,
        source: str,
        product_id: Any,
        payload: Dict[str, Any],
        run_id: str,
        category_id: Optional[Any] = None,
    ) -> None:
        self._save_payload(source, product_id, payload, run_id, "success", category_id)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO crawl_run_products(run_id, source, product_id, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, source, product_id) DO UPDATE SET
                    fetched_at = excluded.fetched_at
                """,
                (run_id, source, str(product_id), utc_now()),
            )
        self.increment_run(run_id, new_count=1)

    def mark_partial(
        self,
        source: str,
        product_id: Any,
        payload: Dict[str, Any],
        run_id: str,
        category_id: Optional[Any] = None,
    ) -> None:
        self._save_payload(source, product_id, payload, run_id, "partial", category_id)

    def _save_payload(
        self,
        source: str,
        product_id: Any,
        payload: Dict[str, Any],
        run_id: str,
        status: str,
        category_id: Optional[Any],
    ) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO products(
                    source, product_id, category_id, status, payload_json,
                    content_hash, first_seen_at, last_seen_at, fetched_at,
                    first_seen_run_id, last_seen_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, product_id) DO UPDATE SET
                    category_id = COALESCE(excluded.category_id, products.category_id),
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    content_hash = excluded.content_hash,
                    last_seen_at = excluded.last_seen_at,
                    fetched_at = excluded.fetched_at,
                    last_seen_run_id = excluded.last_seen_run_id,
                    last_error = NULL
                """,
                (
                    source,
                    str(product_id),
                    None if category_id is None else str(category_id),
                    status,
                    serialized,
                    digest,
                    now,
                    now,
                    now,
                    run_id,
                    run_id,
                ),
            )

    def mark_failed(
        self,
        source: str,
        product_id: Any,
        error: Union[BaseException, str],
        run_id: str,
    ) -> None:
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO products(
                    source, product_id, status, first_seen_at, last_seen_at,
                    first_seen_run_id, last_seen_run_id, retry_count, last_error
                ) VALUES (?, ?, 'failed', ?, ?, ?, ?, 1, ?)
                ON CONFLICT(source, product_id) DO UPDATE SET
                    status = CASE WHEN products.status = 'success' THEN 'success' ELSE 'failed' END,
                    last_seen_at = excluded.last_seen_at,
                    last_seen_run_id = excluded.last_seen_run_id,
                    retry_count = products.retry_count + 1,
                    last_error = excluded.last_error
                """,
                (source, str(product_id), now, now, run_id, run_id, str(error)),
            )
        self.increment_run(run_id, failed_count=1)

    def bulk_import_success(
        self,
        source: str,
        records: Iterable[Tuple[Any, Dict[str, Any], Optional[Any]]],
        imported_at: Optional[str] = None,
    ) -> int:
        now = imported_at or utc_now()
        rows: List[Tuple[Any, ...]] = []
        for product_id, payload, category_id in records:
            normalized_id = str(product_id or "")
            if not normalized_id:
                continue
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            rows.append(
                (
                    source,
                    normalized_id,
                    None if category_id is None else str(category_id),
                    serialized,
                    hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                    now,
                    now,
                    now,
                )
            )
        if not rows:
            return 0
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO products(
                    source, product_id, category_id, status, payload_json,
                    content_hash, first_seen_at, last_seen_at, fetched_at
                ) VALUES (?, ?, ?, 'success', ?, ?, ?, ?, ?)
                ON CONFLICT(source, product_id) DO UPDATE SET
                    category_id = COALESCE(excluded.category_id, products.category_id),
                    status = 'success',
                    payload_json = excluded.payload_json,
                    content_hash = excluded.content_hash,
                    last_seen_at = excluded.last_seen_at,
                    fetched_at = COALESCE(products.fetched_at, excluded.fetched_at),
                    last_error = NULL
                """,
                rows,
            )
        return len(rows)

    def iter_payloads(self, source: str, run_id: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        sql = "SELECT payload_json FROM products WHERE source = ? AND status = 'success'"
        parameters: List[Any] = [source]
        if run_id is not None:
            sql += " AND first_seen_run_id = ?"
            parameters.append(run_id)
        sql += " ORDER BY product_id"
        for row in self.connection.execute(sql, parameters):
            if row["payload_json"]:
                yield json.loads(row["payload_json"])

    def processing_count(self, source: str, mode: str) -> int:
        if mode == "full":
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM products WHERE source = ? AND status = 'success' AND payload_json IS NOT NULL",
                (source,),
            ).fetchone()
        elif mode == "incremental":
            row = self.connection.execute(
                """
                SELECT COUNT(DISTINCT pending.product_id) AS count
                FROM crawl_run_products AS pending
                JOIN products AS product
                  ON product.source = pending.source AND product.product_id = pending.product_id
                WHERE pending.source = ?
                  AND pending.processed_job_id IS NULL
                  AND product.status = 'success'
                  AND product.payload_json IS NOT NULL
                """,
                (source,),
            ).fetchone()
        else:
            raise ValueError("mode must be full or incremental")
        return int(row["count"])

    def iter_processing_payloads(self, source: str, mode: str) -> Iterator[Dict[str, Any]]:
        if mode == "full":
            rows = self.connection.execute(
                """
                SELECT payload_json FROM products
                WHERE source = ? AND status = 'success' AND payload_json IS NOT NULL
                ORDER BY product_id
                """,
                (source,),
            )
        elif mode == "incremental":
            rows = self.connection.execute(
                """
                SELECT product.payload_json
                FROM products AS product
                WHERE product.source = ?
                  AND product.status = 'success'
                  AND product.payload_json IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM crawl_run_products AS pending
                      WHERE pending.source = product.source
                        AND pending.product_id = product.product_id
                        AND pending.processed_job_id IS NULL
                  )
                ORDER BY product.product_id
                """,
                (source,),
            )
        else:
            raise ValueError("mode must be full or incremental")
        for row in rows:
            yield json.loads(row["payload_json"])

    def mark_processing_completed(self, sources: Sequence[str], job_id: str) -> None:
        normalized = list(dict.fromkeys(str(source) for source in sources if source))
        if not normalized:
            return
        placeholders = ",".join("?" for _ in normalized)
        with self.connection:
            self.connection.execute(
                f"""
                UPDATE crawl_run_products
                SET processed_job_id = ?, processed_at = ?
                WHERE processed_job_id IS NULL AND source IN ({placeholders})
                """,  # noqa: S608 - placeholders are generated from a validated list length.
                (job_id, utc_now(), *normalized),
            )

    def run_stats(self, run_id: str) -> Dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM crawl_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else {}
