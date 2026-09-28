import requests
import json
import time
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import crawler_control as control
from catalog_store import CatalogStore, SQLITE_BATCH_SIZE, resolve_database_path
from crawler_performance import RequestRateLimiter, settings_from_environment

BASE_URL = "https://self.gdbadatong.com/shopapi"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PAGE_SIZE = 50
_performance = settings_from_environment("bdt")
_request_limiter = RequestRateLimiter(_performance.request_interval_ms)

os.makedirs(OUTPUT_DIR, exist_ok=True)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 11_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E217 MicroMessenger/6.8.0",
    "Accept": "*/*",
    "Referer": "https://servicewechat.com/wxeadc8963624ed731/169/page-frame.html",
    "version": "1.8.0",
    "token": "",
})


def get_json(url, params=None, retries=3):
    for i in range(retries):
        control.checkpoint()
        _request_limiter.wait(control.checkpoint)
        control.checkpoint()
        try:
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 1:
                return data["data"]
            print(f"  API返回错误: {data.get('msg', '未知错误')}")
            return None
        except Exception as e:
            print(f"  请求失败(第{i+1}次): {e}")
            if i < retries - 1:
                time.sleep(2)
    return None


def save_json(filename, data):
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)
    print(f"  已保存: {path}")


def step1_get_categories():
    print("=== 步骤1: 获取一级分类 ===")
    data = get_json(f"{BASE_URL}/goodsCategory/lists", {"levelType": 1})
    if not data:
        print("获取一级分类失败!")
        return []
    categories = data["lists"]
    total = data["count"]
    print(f"商品总数: {total}, 一级分类数: {len(categories)}")
    save_json("categories_level1.json", categories)
    return categories


def step2_get_subcategories(level1_id, level1_name):
    print(f"\n--- 获取二级分类: {level1_name}(id={level1_id}) ---")
    data = get_json(f"{BASE_URL}/goodsCategory/lists", {"levelType": 2, "id": level1_id})
    if not data:
        return []
    subcategories = data["lists"]
    print(f"  二级分类数: {len(subcategories)}")
    return subcategories


def category_mapping_rows(level1, level2_categories):
    """Flatten the live category tree for report exports without re-fetching it."""
    rows = []
    for level2 in level2_categories:
        if not isinstance(level2, dict):
            continue
        for level3 in level2.get("sons", []):
            if not isinstance(level3, dict):
                continue
            values = (
                level3.get("id"),
                level1.get("id"),
                level1.get("name"),
                level2.get("id"),
                level2.get("name"),
                level3.get("name"),
            )
            if all(value is not None and str(value) for value in values):
                rows.append(values)
    return rows


def step3_get_goods_list(category_id, category_name):
    print(f"\n  --- 获取商品列表: {category_name}(id={category_id}) ---")
    all_goods = []
    page = 1
    while True:
        data = get_json(f"{BASE_URL}/goods/lists", {
            "category_id": category_id,
            "page_no": page,
            "page_size": PAGE_SIZE,
            "is_brand": 1,
        })
        if not data:
            break
        goods = data.get("lists", {}).get("goods", [])
        if not goods:
            break
        total = data["count"]
        all_goods.extend(goods)
        print(f"    第{page}页: 获取{len(goods)}个商品 (累计{len(all_goods)}/{total})")
        if len(all_goods) >= total:
            break
        page += 1
    return all_goods


def step4_get_goods_detail(goods_id):
    data = get_json(f"{BASE_URL}/goods/detail", {"id": goods_id, "visit": 1})
    if data and "other_goods" in data:
        del data["other_goods"]
    return data


def load_existing_details():
    for filename in ("goods_details_partial.json", "goods_details_all.json"):
        path = os.path.join(OUTPUT_DIR, filename)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        fetched_ids = {item["id"] for item in data}
        print(f"加载已有数据: {len(data)} 个商品")
        return data, fetched_ids
    return [], set()


def main(database_path=None):
    global _performance, _request_limiter
    control.install()
    _performance = settings_from_environment("bdt")
    _request_limiter = RequestRateLimiter(_performance.request_interval_ms)
    print(
        f"性能设置: 请求间隔={_performance.request_interval_ms}ms "
        f"并发={_performance.concurrency}"
    )
    all_details, fetched_ids = load_existing_details()
    catalog = CatalogStore(
        database_path or resolve_database_path(Path(OUTPUT_DIR) / "catalog.sqlite3")
    )
    if not catalog.source_count("bdt") and all_details:
        imported = 0
        for start in range(0, len(all_details), SQLITE_BATCH_SIZE):
            batch = [
                (item.get("id"), item, None)
                for item in all_details[start : start + SQLITE_BATCH_SIZE]
                if item.get("id") is not None
            ]
            imported += catalog.bulk_import_success("bdt", batch)
        print(f"SQLite 增量索引初始化: bdt {imported} 条")
    run_id = catalog.start_run("bdt")
    failed_ids = []
    new_count = 0
    final_status = "failed"
    try:
        level1_categories = step1_get_categories()
        if not level1_categories:
            return

        for cat1 in level1_categories:
            level1_id = cat1["id"]
            level1_name = cat1["name"]

            subcategories = step2_get_subcategories(level1_id, level1_name)
            if not subcategories:
                continue

            cat_dir = os.path.join(OUTPUT_DIR, "categories", str(level1_id))
            os.makedirs(cat_dir, exist_ok=True)
            save_json(f"categories/{level1_id}/subcategories.json", subcategories)
            mapped_count = catalog.upsert_bdt_category_mappings(
                category_mapping_rows(cat1, subcategories)
            )
            if mapped_count:
                print(f"  已同步分类映射: {mapped_count} 个三级分类")

            for cat2 in subcategories:
                sons = cat2.get("sons", [])
                if not sons:
                    continue

                for son in sons:
                    son_id = son["id"]
                    son_name = son["name"]

                    goods_list = step3_get_goods_list(son_id, son_name)
                    if not goods_list:
                        continue

                    goods_ids = [g["id"] for g in goods_list]
                    save_json(f"categories/{level1_id}/goods_list_{son_id}.json", goods_list)

                    successful_ids = catalog.ids_with_statuses("bdt", goods_ids)
                    catalog.register_discovered(
                        "bdt", ((gid, son_id) for gid in goods_ids), run_id
                    )
                    catalog.increment_run(run_id, skipped_count=len(successful_ids))
                    skip_count = 0
                    candidate_ids = []
                    for gid in goods_ids:
                        if str(gid) in successful_ids:
                            skip_count += 1
                        else:
                            candidate_ids.append(gid)

                    def save_detail(gid, detail):
                        nonlocal new_count
                        if detail:
                            catalog.mark_success("bdt", gid, detail, run_id, son_id)
                            all_details.append(detail)
                            fetched_ids.add(gid)
                            new_count += 1
                            print(f"      成功: {detail.get('name', '')[:40]}")
                        else:
                            catalog.mark_failed("bdt", gid, "empty detail response", run_id)
                            failed_ids.append(gid)
                            print(f"      失败: id={gid}")

                    if _performance.concurrency == 1:
                        for gid in candidate_ids:
                            control.checkpoint()
                            print(f"    获取商品详情: id={gid}")
                            save_detail(gid, step4_get_goods_detail(gid))
                    else:
                        with ThreadPoolExecutor(max_workers=_performance.concurrency) as executor:
                            futures = {
                                executor.submit(step4_get_goods_detail, gid): gid
                                for gid in candidate_ids
                            }
                            for future in as_completed(futures):
                                control.checkpoint()
                                gid = futures[future]
                                print(f"    获取商品详情: id={gid}")
                                try:
                                    detail = future.result()
                                except Exception as exc:  # noqa: BLE001 - retry next run.
                                    catalog.mark_failed("bdt", gid, exc, run_id)
                                    failed_ids.append(gid)
                                    print(f"      失败: id={gid}: {exc}")
                                    continue
                                save_detail(gid, detail)

                    if skip_count:
                        print(f"    跳过已有: {skip_count}个")
        final_status = "success"
    except control.FinishRequested:
        final_status = "interrupted"
        print("结束请求：保存已采集商品", flush=True)
    finally:
        try:
            save_json("goods_details_partial.json", all_details)
            save_json("goods_details_all.json", all_details)
        finally:
            catalog.finish_run(run_id, final_status)
            stats = catalog.run_stats(run_id)
            print(
                "增量统计: "
                f"扫描={stats.get('scanned_count', 0)} "
                f"跳过={stats.get('skipped_count', 0)} "
                f"新增={stats.get('new_count', 0)} "
                f"失败={stats.get('failed_count', 0)}"
            )
            catalog.close()

    print(f"\n=== 完成! 本次新增 {new_count} 个, 总计 {len(all_details)} 个商品详情 ===")
    if failed_ids:
        save_json("failed_ids.json", failed_ids)
        print(f"失败商品数: {len(failed_ids)}")


if __name__ == "__main__":
    main()
