#!/usr/bin/env python3
"""Import existing crawler JSON/JSONL outputs into the SQLite catalog."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple

from catalog_store import CatalogStore, SQLITE_BATCH_SIZE


@dataclass
class ImportReport:
    source: str
    path: Path
    records: int = 0
    valid: int = 0
    invalid: int = 0
    before: int = 0
    after: int = 0

    @property
    def unique_added(self) -> int:
        return self.after - self.before


def _ijson() -> Any:
    try:
        import ijson  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "导入大型 JSON 数组需要 ijson，请先运行: python3 -m pip install -r requirements.txt"
        ) from exc
    return ijson


def iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if isinstance(value, dict):
                yield value


class BdtJSONRepairReader:
    """Repair truncated content strings while streaming without changing the source."""

    def __init__(self, handle: Any) -> None:
        self.handle = handle
        self.buffer = b""
        self.repaired_count = 0

    def _next_line(self) -> bytes:
        line = self.handle.readline()
        if not line:
            return b""
        stripped = line.rstrip(b"\r\n")
        if b'"content":' in stripped and not (
            stripped.endswith(b'",') or stripped.endswith(b'"')
        ):
            indentation = line[: len(line) - len(line.lstrip())]
            self.repaired_count += 1
            return indentation + b'"content": "",\n'
        return line

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if size < 0:
            chunks = [self.buffer]
            self.buffer = b""
            while True:
                line = self._next_line()
                if not line:
                    break
                chunks.append(line)
            return b"".join(chunks)

        while len(self.buffer) < size:
            line = self._next_line()
            if not line:
                break
            self.buffer += line
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result


def iter_json_array(path: Path, repair_bdt: bool = False) -> Iterator[Dict[str, Any]]:
    parser = _ijson()
    with path.open("rb") as handle:
        source = BdtJSONRepairReader(handle) if repair_bdt else handle
        for value in parser.items(source, "item", use_float=True):
            if isinstance(value, dict):
                yield value
        if repair_bdt and source.repaired_count:
            print(
                f"bdt: 已修复 {source.repaired_count} 个截断的 content 字段（原文件未修改）",
                flush=True,
            )


def iter_lcgt(path: Path) -> Iterator[Tuple[Dict[str, Any], Optional[str]]]:
    parser = _ijson()
    with path.open("rb") as handle:
        for category_name, subcategories in parser.kvitems(handle, "", use_float=True):
            if not isinstance(subcategories, dict):
                continue
            for subcategory_name, section in subcategories.items():
                if not isinstance(section, dict):
                    continue
                category_id = str(section.get("subcategory_id") or f"{category_name}/{subcategory_name}")
                for product in section.get("products") or []:
                    if isinstance(product, dict):
                        yield product, category_id


def extract_record(
    source: str,
    payload: Dict[str, Any],
    category_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any], Optional[str]]:
    if source == "sjyx":
        product = payload.get("product") or {}
        product_id = product.get("sku_sys_no") or payload.get("sku_sys_no")
        category = payload.get("category") or {}
        category_id = category_id or category.get("cascade_id")
    else:
        product_id = payload.get("id")
    return str(product_id or ""), payload, category_id


def import_source(store: CatalogStore, source: str, path: Path) -> ImportReport:
    if not path.exists():
        raise FileNotFoundError(path)
    report = ImportReport(source=source, path=path, before=store.source_count(source))

    if source == "lcgt":
        values: Iterable[Tuple[Dict[str, Any], Optional[str]]] = iter_lcgt(path)
    else:
        payloads = (
            iter_jsonl(path)
            if path.suffix.lower() == ".jsonl"
            else iter_json_array(path, repair_bdt=source == "bdt")
        )
        values = ((payload, None) for payload in payloads)

    batch = []
    for payload, category_id in values:
        report.records += 1
        product_id, payload, category_id = extract_record(source, payload, category_id)
        if not product_id:
            report.invalid += 1
            continue
        report.valid += 1
        batch.append((product_id, payload, category_id))
        if len(batch) >= SQLITE_BATCH_SIZE:
            store.bulk_import_success(source, batch)
            batch.clear()
        if report.records % 10000 == 0:
            print(
                f"{source}: 已扫描 {report.records:,} 条，有效 {report.valid:,} 条，"
                f"无ID {report.invalid:,} 条",
                flush=True,
            )
    if batch:
        store.bulk_import_success(source, batch)

    report.after = store.source_count(source)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把已有商品 JSON/JSONL 导入 SQLite 增量索引")
    parser.add_argument("--database", type=Path, default=Path("data/catalog.sqlite3"))
    parser.add_argument("--sjyx", type=Path, help="sjyx products.jsonl 或 products.json")
    parser.add_argument("--bdt", type=Path, help="bdt goods_details_all.json")
    parser.add_argument("--lcgt", type=Path, help="lcgt lcgt_products.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected = [(name, getattr(args, name)) for name in ("sjyx", "bdt", "lcgt") if getattr(args, name)]
    if not selected:
        raise SystemExit("至少指定 --sjyx、--bdt 或 --lcgt 中的一项")

    with CatalogStore(args.database) as store:
        for source, path in selected:
            report = import_source(store, source, path)
            print(
                f"{source}: records={report.records} valid={report.valid} invalid={report.invalid} "
                f"unique_added={report.unique_added} total={report.after}"
            )
    print(f"数据库: {args.database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
