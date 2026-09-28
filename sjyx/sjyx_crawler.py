#!/usr/bin/env python3
"""Crawler for public product data on www.3jyx.cn.

The crawler uses public JSON endpoints, writes JSONL incrementally, and removes
price-related fields before data is persisted.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import certifi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import crawler_control as control
from catalog_store import CatalogStore, SQLITE_BATCH_SIZE, resolve_database_path
from crawler_performance import RequestRateLimiter, settings_from_environment


BASE_URL = "https://www.3jyx.cn"
OSS_URL = "https://oss.3jyx.cn"
DEFAULT_AREA_SYS_NO = "90542"

CATEGORY_ENDPOINT = "/api/mall/v1/category/list"
PRODUCT_LIST_ENDPOINT = "/api/mall/v1/mallSkuList/searchMallSkuList"
PRODUCT_DETAIL_ENDPOINT = "/api/mall/v1/mallSku/getDetail"

PRICE_FIELD_NAMES = {
    "agreementPrice",
    "agreementPriceForm",
    "agreementPriceMissingReason",
    "agreementPriceNoTax",
    "bestSkuSalesPrice",
    "excludeTaxPrice",
    "highestPrice",
    "lowestPrice",
    "price",
    "priceDesc",
    "priceFlag",
    "salesPrice",
    "searchPriceFlag",
    "shopPriceFlag",
    "showPrice",
    "tradeAgreementSysNoToPrice",
    "purchaserCustomizePrice",
}

EXCLUDED_FIELD_NAMES = {
    "recommendSpuDTO",
    "recommendSpuList",
    "recommendSkuDTO",
    "recommendSkuList",
    "recommendProductList",
}


class ApiError(RuntimeError):
    """Raised when the remote API does not return usable data."""


@dataclass
class CrawlerConfig:
    output_dir: Path
    page_size: int = 200
    min_delay: float = 1.5
    max_delay: float = 4.0
    max_retries: int = 3
    timeout: float = 30.0
    area_sys_no: str = DEFAULT_AREA_SYS_NO
    max_categories: Optional[int] = None
    max_pages_per_category: Optional[int] = None
    detail_enabled: bool = True
    detail_log_every: int = 20
    database_path: Optional[Path] = None
    request_interval_ms: Optional[int] = None
    concurrency: int = 1


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, item: Dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.done_skus = set()
        self.done_categories = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.done_skus = set(data.get("done_skus", []))
        self.done_categories = set(data.get("done_categories", []))

    def is_sku_done(self, sku_sys_no: str) -> bool:
        return sku_sys_no in self.done_skus

    def is_category_done(self, cascade_id: str) -> bool:
        return cascade_id in self.done_categories

    def mark_sku_done(self, sku_sys_no: str) -> None:
        self.done_skus.add(sku_sys_no)
        self._save()

    def mark_category_done(self, cascade_id: str) -> None:
        self.done_categories.add(cascade_id)
        self._save()

    def _save(self) -> None:
        payload = {
            "done_skus": sorted(self.done_skus),
            "done_categories": sorted(self.done_categories),
            "updated_at": int(time.time()),
        }
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)


class ApiClient:
    def __init__(self, config: CrawlerConfig) -> None:
        self.config = config
        # python.org macOS installers do not always have a usable system CA
        # symlink. certifi gives every supported platform the same trusted CA
        # bundle while keeping TLS certificate verification enabled.
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.rate_limiter = (
            RequestRateLimiter(config.request_interval_ms)
            if config.request_interval_ms is not None
            else None
        )

    def post(self, endpoint: str, payload: Dict[str, Any], referer: str = "/") -> Dict[str, Any]:
        url = BASE_URL + endpoint
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": BASE_URL,
            "Referer": BASE_URL + referer,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        return self._request_json(url, body, headers)

    def _request_json(self, url: str, body: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.config.max_retries + 1):
            control.checkpoint()
            self._delay(attempt)
            control.checkpoint()
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.config.timeout,
                    context=self.ssl_context,
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if payload.get("code") != 200:
                    raise ApiError(f"API code={payload.get('code')} message={payload.get('message')}")
                return payload
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ApiError) as exc:
                last_error = exc
                if attempt == self.config.max_retries:
                    break
                time.sleep(min(30.0, 2.0**attempt + random.random()))
        raise ApiError(f"Request failed after {self.config.max_retries} attempts: {url}: {last_error}")

    def _delay(self, attempt: int) -> None:
        if self.rate_limiter is not None:
            self.rate_limiter.wait(control.checkpoint)
        elif attempt <= 1:
            time.sleep(random.uniform(self.config.min_delay, self.config.max_delay))


def flatten_categories(categories: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    leaves: List[Dict[str, Any]] = []

    def visit(node: Dict[str, Any], ids: List[str], names: List[str]) -> None:
        node_id = str(node.get("id") or "")
        node_name = str(node.get("title") or node.get("shortName") or "")
        next_ids = ids + ([node_id] if node_id else [])
        next_names = names + ([node_name] if node_name else [])
        children = node.get("children") or []
        if children:
            for child in children:
                visit(child, next_ids, next_names)
            return
        cascade_id = str(node.get("cascadeId") or ",".join(next_ids))
        leaves.append(
            {
                "ids": next_ids,
                "names": next_names,
                "cascade_id": cascade_id,
                "leaf_id": node_id,
            }
        )

    for category in categories:
        visit(category, [], [])
    return leaves


def remove_price_fields(value: Any) -> Any:
    if isinstance(value, list):
        return [remove_price_fields(item) for item in value]
    if not isinstance(value, dict):
        return value

    cleaned: Dict[str, Any] = {}
    explicit = {field.lower() for field in PRICE_FIELD_NAMES | EXCLUDED_FIELD_NAMES}
    for key, child in value.items():
        normalized_key = str(key).lower()
        if normalized_key in explicit or "price" in normalized_key:
            continue
        cleaned[key] = remove_price_fields(child)
    return cleaned


def normalize_product(
    category: Dict[str, Any],
    listing: Dict[str, Any],
    detail: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    cleaned_listing = remove_price_fields(listing)
    cleaned_detail = remove_price_fields(detail or {})
    sku_sys_no = str(listing.get("bestSkuSysNo") or cleaned_detail.get("sysNo") or "")
    spu_sys_no = str(listing.get("sysNo") or cleaned_detail.get("spuSysNo") or "")

    return {
        "category": {
            "ids": category.get("ids", []),
            "names": category.get("names", []),
            "cascade_id": category.get("cascade_id", ""),
            "leaf_id": category.get("leaf_id", ""),
        },
        "product": {
            "spu_sys_no": spu_sys_no,
            "sku_sys_no": sku_sys_no,
            "name": listing.get("name") or cleaned_detail.get("name") or "",
            "brand": listing.get("brand") or cleaned_detail.get("brandName") or "",
            "shop_name": listing.get("shopName") or "",
            "shop_sys_no": listing.get("shopSysNo") or "",
            "unit": listing.get("unit") or cleaned_detail.get("unitName") or "",
            "images": normalize_images(listing.get("images") or cleaned_detail.get("imgPaths") or []),
            "attributes": cleaned_listing.get("attributeList") or [],
            "spec_model": extract_spec_model(listing, cleaned_detail),
            "listing": cleaned_listing,
            "detail": cleaned_detail,
        },
    }


def normalize_images(images: Any) -> List[str]:
    if isinstance(images, str):
        images = [images]
    if not isinstance(images, list):
        return []
    return [absolute_asset_url(str(image)) for image in images if image]


def absolute_asset_url(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if path.startswith("/"):
        return OSS_URL + path
    return path


def extract_spec_model(listing: Dict[str, Any], detail: Dict[str, Any]) -> str:
    for value in (
        detail.get("model"),
        listing.get("bestSkuModel"),
        detail.get("name"),
        listing.get("name"),
    ):
        if value:
            return str(value)

    for attr in listing.get("attributeList") or []:
        attr_name = str(attr.get("attributeName") or "")
        if "规格" in attr_name or "型号" in attr_name:
            attr_value = attr.get("attributeValue")
            if attr_value:
                return str(attr_value)
    return ""


def should_log_detail_progress(index: int, total: int, every: int) -> bool:
    if total <= 0:
        return False
    if index == 1 or index == total:
        return True
    return every > 0 and index % every == 0


class SjyxCrawler:
    def __init__(self, config: CrawlerConfig) -> None:
        self.config = config
        self.client = ApiClient(config)
        self.products = JsonlWriter(config.output_dir / "products.jsonl")
        self.errors = JsonlWriter(config.output_dir / "errors.jsonl")
        database_path = config.database_path or resolve_database_path(config.output_dir / "catalog.sqlite3")
        self.catalog = CatalogStore(database_path)
        self._bootstrap_catalog()
        self.run_id = self.catalog.start_run("sjyx")
        interval = (
            f"{config.request_interval_ms}ms"
            if config.request_interval_ms is not None
            else f"{config.min_delay}-{config.max_delay}s"
        )
        print(f"性能设置: 请求间隔={interval} 并发={config.concurrency}", flush=True)

    def _bootstrap_catalog(self) -> None:
        """Build the initial index from the existing JSONL without loading it all."""
        if self.catalog.source_count("sjyx"):
            return
        path = self.config.output_dir / "products.jsonl"
        if not path.exists():
            return
        batch = []
        imported = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                product = payload.get("product") or {}
                product_id = str(product.get("sku_sys_no") or "")
                category_id = (payload.get("category") or {}).get("cascade_id")
                if not product_id:
                    continue
                batch.append((product_id, payload, category_id))
                if len(batch) >= SQLITE_BATCH_SIZE:
                    imported += self.catalog.bulk_import_success("sjyx", batch)
                    batch.clear()
        if batch:
            imported += self.catalog.bulk_import_success("sjyx", batch)
        if imported:
            print(f"SQLite 增量索引初始化: sjyx {imported} 条", flush=True)

    def run(self) -> None:
        final_status = "failed"
        try:
            categories = self.fetch_leaf_categories()
            if self.config.max_categories is not None:
                categories = categories[: self.config.max_categories]

            for index, category in enumerate(categories, start=1):
                control.checkpoint()
                # Categories are scanned on every run so newly listed products can be found.
                print(f"[{index}/{len(categories)}] crawl {' > '.join(category['names'])}", flush=True)
                try:
                    self.crawl_category(category)
                except Exception as exc:  # noqa: BLE001 - keep long crawls moving.
                    self.errors.append({"scope": "category", "category": category, "error": str(exc)})
            final_status = "success"
        except control.FinishRequested:
            final_status = "interrupted"
            raise
        finally:
            self.catalog.finish_run(self.run_id, final_status)
            stats = self.catalog.run_stats(self.run_id)
            print(
                "增量统计: "
                f"扫描={stats.get('scanned_count', 0)} "
                f"跳过={stats.get('skipped_count', 0)} "
                f"新增={stats.get('new_count', 0)} "
                f"失败={stats.get('failed_count', 0)}",
                flush=True,
            )
            self.catalog.close()

    def fetch_leaf_categories(self) -> List[Dict[str, Any]]:
        response = self.client.post(CATEGORY_ENDPOINT, {}, referer="/category")
        data = response.get("data") or {}
        categories = data.get("categories") or []
        leaves = flatten_categories(categories)
        if not leaves:
            raise ApiError("No leaf categories found")
        return leaves

    def crawl_category(self, category: Dict[str, Any]) -> None:
        page_no = 1
        total_pages = 1
        while page_no <= total_pages:
            if self.config.max_pages_per_category is not None:
                if page_no > self.config.max_pages_per_category:
                    break
            payload = {
                "catId": category["cascade_id"],
                "pageNo": page_no,
                "pageSize": self.config.page_size,
            }
            response = self.client.post(PRODUCT_LIST_ENDPOINT, payload, referer="/product/list")
            spu_list = (response.get("data") or {}).get("spuList") or {}
            total = int(spu_list.get("total") or 0)
            total_pages = int(spu_list.get("totalPage") or 1)
            if total >= 5000:
                self.errors.append(
                    {
                        "scope": "category_limit",
                        "category": category,
                        "total": total,
                        "message": "Category may require further splitting to avoid the 5000 result cap.",
                    }
                )
            rows = spu_list.get("datas") or []
            print(f"  page {page_no}/{total_pages}, rows={len(rows)}, total={total}", flush=True)
            page_ids = [str(row.get("bestSkuSysNo") or "") for row in rows]
            successful_ids = self.catalog.ids_with_statuses("sjyx", page_ids)
            self.catalog.register_discovered(
                "sjyx",
                ((product_id, category["cascade_id"]) for product_id in page_ids if product_id),
                self.run_id,
            )
            self.catalog.increment_run(self.run_id, skipped_count=len(successful_ids))
            candidates = []
            for row_index, listing in enumerate(rows, start=1):
                sku_sys_no = str(listing.get("bestSkuSysNo") or "")
                if not sku_sys_no:
                    self.errors.append({"scope": "listing", "listing": listing, "error": "missing bestSkuSysNo"})
                elif sku_sys_no not in successful_ids:
                    candidates.append((row_index, listing, sku_sys_no))

            if self.config.detail_enabled and self.config.concurrency > 1:
                with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
                    futures = {
                        executor.submit(self.fetch_detail, sku_sys_no): (row_index, listing, sku_sys_no)
                        for row_index, listing, sku_sys_no in candidates
                    }
                    for future in as_completed(futures):
                        control.checkpoint()
                        row_index, listing, sku_sys_no = futures[future]
                        if should_log_detail_progress(
                            row_index, len(rows), self.config.detail_log_every
                        ):
                            print(
                                f"    detail page={page_no} item={row_index}/{len(rows)} sku={sku_sys_no}",
                                flush=True,
                            )
                        try:
                            detail = future.result()
                        except Exception as exc:  # noqa: BLE001 - retry on the next run.
                            self.errors.append(
                                {"scope": "detail", "sku_sys_no": sku_sys_no, "error": str(exc)}
                            )
                            self.catalog.mark_failed("sjyx", sku_sys_no, exc, self.run_id)
                            continue
                        self.persist_listing(category, listing, detail)
            else:
                for row_index, listing, _ in candidates:
                    control.checkpoint()
                    self.handle_listing(category, listing, row_index, len(rows), page_no)
            page_no += 1

    def handle_listing(
        self,
        category: Dict[str, Any],
        listing: Dict[str, Any],
        row_index: int,
        row_total: int,
        page_no: int,
        known_success: bool = False,
    ) -> None:
        sku_sys_no = str(listing.get("bestSkuSysNo") or "")
        if not sku_sys_no:
            self.errors.append({"scope": "listing", "listing": listing, "error": "missing bestSkuSysNo"})
            return
        if known_success or sku_sys_no in self.catalog.ids_with_statuses("sjyx", [sku_sys_no]):
            return

        if should_log_detail_progress(row_index, row_total, self.config.detail_log_every):
            print(f"    detail page={page_no} item={row_index}/{row_total} sku={sku_sys_no}", flush=True)

        detail = None
        if self.config.detail_enabled:
            try:
                detail = self.fetch_detail(sku_sys_no)
            except Exception as exc:  # noqa: BLE001 - keep the listing with partial data.
                self.errors.append({"scope": "detail", "sku_sys_no": sku_sys_no, "error": str(exc)})
                self.catalog.mark_failed("sjyx", sku_sys_no, exc, self.run_id)
                return

        self.persist_listing(category, listing, detail)

    def persist_listing(
        self,
        category: Dict[str, Any],
        listing: Dict[str, Any],
        detail: Optional[Dict[str, Any]],
    ) -> None:
        sku_sys_no = str(listing.get("bestSkuSysNo") or "")
        product = normalize_product(category, listing, detail)
        if self.config.detail_enabled:
            self.catalog.mark_success("sjyx", sku_sys_no, product, self.run_id, category["cascade_id"])
        else:
            self.catalog.mark_partial("sjyx", sku_sys_no, product, self.run_id, category["cascade_id"])
        self.products.append(product)

    def fetch_detail(self, sku_sys_no: str) -> Dict[str, Any]:
        response = self.client.post(
            PRODUCT_DETAIL_ENDPOINT,
            {
                "sysNo": sku_sys_no,
                "areaSysNo": self.config.area_sys_no,
                "frontDefaultTradeMethodList": [],
                "mmuAttributeList": [],
            },
            referer=f"/product/detail?id={sku_sys_no}",
        )
        detail = response.get("data") or {}
        if not detail:
            raise ApiError(f"Empty detail response for SKU {sku_sys_no}")
        return detail


def export_json_array(jsonl_path: Path, json_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = json_path.with_suffix(json_path.suffix + ".tmp")
    with jsonl_path.open("r", encoding="utf-8") as source, temp_path.open("w", encoding="utf-8") as target:
        target.write("[\n")
        first = True
        for line in source:
            if not line.strip():
                continue
            payload = json.loads(line)
            if not first:
                target.write(",\n")
            target.write(json.dumps(payload, ensure_ascii=False, indent=2))
            first = False
        target.write("\n]\n")
    temp_path.replace(json_path)


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl public product data from www.3jyx.cn")
    parser.add_argument("--output-dir", default="outputs", type=Path)
    parser.add_argument("--page-size", default=200, type=positive_int)
    parser.add_argument("--min-delay", default=1.5, type=non_negative_float)
    parser.add_argument("--max-delay", default=4.0, type=non_negative_float)
    parser.add_argument("--max-retries", default=3, type=positive_int)
    parser.add_argument("--timeout", default=30.0, type=non_negative_float)
    parser.add_argument("--area-sys-no", default=DEFAULT_AREA_SYS_NO)
    parser.add_argument("--max-categories", type=positive_int)
    parser.add_argument("--max-pages-per-category", type=positive_int)
    parser.add_argument("--detail-log-every", default=20, type=positive_int)
    parser.add_argument("--no-detail", action="store_true", help="only save product list data")
    parser.add_argument("--export-json", action="store_true", help="also export products.json array")
    parser.add_argument("--database", type=Path, help="SQLite catalog path (defaults to output-dir/catalog.sqlite3)")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.max_delay < args.min_delay:
        raise SystemExit("--max-delay must be greater than or equal to --min-delay")

    managed_performance = None
    if "GOODS_REQUEST_INTERVAL_MS" in os.environ or "GOODS_CONCURRENCY" in os.environ:
        managed_performance = settings_from_environment("sjyx")

    config = CrawlerConfig(
        output_dir=args.output_dir,
        page_size=args.page_size,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        max_retries=args.max_retries,
        timeout=args.timeout,
        area_sys_no=args.area_sys_no,
        max_categories=args.max_categories,
        max_pages_per_category=args.max_pages_per_category,
        detail_enabled=not args.no_detail,
        detail_log_every=args.detail_log_every,
        database_path=args.database,
        request_interval_ms=(
            managed_performance.request_interval_ms if managed_performance else None
        ),
        concurrency=managed_performance.concurrency if managed_performance else 1,
    )
    crawler = SjyxCrawler(config)
    control.install()
    try:
        crawler.run()
    except control.FinishRequested:
        print("结束请求：保存已采集商品", flush=True)
    finally:
        if args.export_json:
            source = config.output_dir / "products.jsonl"
            source.touch(exist_ok=True)
            export_json_array(source, config.output_dir / "products.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
