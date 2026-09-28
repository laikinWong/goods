import requests
from bs4 import BeautifulSoup
import json
import time
import re
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import crawler_control as control
from catalog_store import CatalogStore, SQLITE_BATCH_SIZE, resolve_database_path
from crawler_performance import RequestRateLimiter, settings_from_environment
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import random

def log(msg):
    print(msg, flush=True)

BASE_URL = "https://www.lcgt.cn"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

MAX_WORKERS = 3
write_lock = threading.Lock()
_active_catalog = None
_active_run_id = None
_performance = settings_from_environment("lcgt")
_request_limiter = RequestRateLimiter(_performance.request_interval_ms)


def random_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": BASE_URL,
    }


def fetch(url, retries=3):
    for i in range(retries):
        control.checkpoint()
        _request_limiter.wait(control.checkpoint)
        control.checkpoint()
        try:
            resp = requests.get(url, headers=random_headers(), timeout=20)
            resp.encoding = "utf-8"
            if resp.status_code == 200:
                if "南墙安全验证" in resp.text:
                    log(f"    触发验证码, 等待60秒后重试...")
                    time.sleep(60)
                    continue
                return resp.text
            else:
                time.sleep(random.uniform(3, 6))
        except requests.exceptions.Timeout:
            time.sleep(random.uniform(3, 6))
        except Exception:
            time.sleep(random.uniform(3, 6))
    return None


def get_categories():
    log("正在抓取类别和品名列表...")
    html = fetch(f"{BASE_URL}/products.html")
    if not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    cat_map = {}
    for li in soup.select("dl.product-category li span[data-id]"):
        cat_id = li.get("data-id")
        cat_name = li.get_text(strip=True)
        if cat_id and cat_id != "0":
            cat_map[cat_id] = {"name": cat_name, "subcategories": {}}

    for li_tag in soup.select("dl.product-name li"):
        span = li_tag.select_one("span[data-id]")
        if not span:
            continue
        sub_id = span.get("data-id")
        parent_id = li_tag.get("data-parent")
        sub_name = span.get("title") or span.get_text(strip=True)
        spec_key = span.get("data-key", "")
        if sub_id and sub_id != "0" and parent_id and parent_id != "0":
            if parent_id in cat_map:
                cat_map[parent_id]["subcategories"][sub_id] = {
                    "name": sub_name,
                    "spec_key": spec_key,
                }

    log(f"  找到 {len(cat_map)} 个类别, 共 {sum(len(c['subcategories']) for c in cat_map.values())} 个品名")
    return cat_map


def build_list_url(cat_id, sub_id, page):
    return f"{BASE_URL}/products-{cat_id}%7C{sub_id}-0-0-0-0-0-3-0-0-0-0-0-p{page}-s20.html"


def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    products = []

    for item in soup.select("div.pro-list"):
        one = item.select_one(".list-item-one")
        two = item.select_one(".list-item-two")
        three = item.select_one(".list-item-three")
        four = item.select_one(".list-item-four")
        if not one:
            continue

        detail_url = one.get("data-url", "")
        product_id = ""
        if detail_url:
            m = re.search(r"/(\d+)\.html", detail_url)
            if m:
                product_id = m.group(1)

        name = ""
        one_name = one.select_one(".one-name")
        if one_name:
            name = one_name.get_text(strip=True)

        image = ""
        img_tag = one.select_one(".one-remark-show img")
        if img_tag:
            image = img_tag.get("data-zoom") or img_tag.get("src", "")

        warehouse = ""
        one_cat = one.select_one(".one-cat")
        if one_cat:
            warehouse = one_cat.get_text(strip=True).replace("仓库(地区)：", "")

        spec = ""
        material = ""
        steel_mill = ""
        if two:
            p_tag = two.select_one("p")
            if p_tag:
                spec = p_tag.get_text(strip=True)
            div_tag = two.select_one("div")
            if div_tag:
                parts = div_tag.get_text(strip=True).split("|")
                material = parts[0].strip() if len(parts) > 0 else ""
                steel_mill = parts[1].strip() if len(parts) > 1 else ""

        company = ""
        company_url = ""
        if three:
            a_tag = three.select_one(".three-company a.kuise")
            if a_tag:
                company = a_tag.get_text(strip=True)
                company_url = a_tag.get("href", "")

        contact = ""
        phone = ""
        if three:
            contact_a = three.select_one(".three-contact a")
            if contact_a:
                text = contact_a.get_text(strip=True)
                m = re.match(r"(.+?)：?(\d{11})", text)
                if m:
                    contact = m.group(1).strip("：")
                    phone = m.group(2)
                else:
                    contact = text

        price = ""
        unit = ""
        if four:
            price_span = four.select_one(".four-price")
            if price_span:
                price = price_span.get_text(strip=True).replace("¥", "").strip()
            unit_span = four.select_one(".four-unit")
            if unit_span:
                unit = unit_span.get_text(strip=True).replace("/", "").strip()

        update_time = ""
        if four:
            time_p = four.select_one(".four-time")
            if time_p:
                update_time = time_p.get_text(strip=True)

        products.append({
            "id": product_id,
            "name": name,
            "spec": spec,
            "material": material,
            "steel_mill": steel_mill,
            "warehouse": warehouse,
            "price": price,
            "unit": unit,
            "update_time": update_time,
            "company": company,
            "company_url": company_url,
            "contact": contact,
            "phone": phone,
            "image": image,
            "detail_url": detail_url,
            "related_products": []
        })

    total_pages = 1
    paging_total = soup.select_one("#pagingTotal")
    if paging_total:
        try:
            total_pages = int(paging_total.get("value", "1"))
        except:
            total_pages = 1
    return products, total_pages


def parse_detail_page(html):
    soup = BeautifulSoup(html, "html.parser")
    result = {"main_image": "", "related_products": []}

    detail_left = soup.select_one(".product-detail .left img")
    if detail_left:
        result["main_image"] = detail_left.get("src", "")

    table = soup.select_one("table.product-list tbody")
    if table:
        for tr in table.select("tr"):
            tds = tr.select("td")
            if len(tds) < 8:
                continue

            rel_name = ""
            name_a = tds[0].select_one("a span")
            if name_a:
                rel_name = name_a.get_text(strip=True)

            rel_image = ""
            name_img = tds[0].select_one("img")
            if name_img:
                rel_image = name_img.get("data-zoom") or name_img.get("src", "")

            rel_spec = ""
            spec_span = tds[1].select_one("span")
            if spec_span:
                rel_spec = spec_span.get_text(strip=True)

            rel_material = ""
            mat_span = tds[2].select_one("span")
            if mat_span:
                rel_material = mat_span.get_text(strip=True)

            rel_mill = ""
            mill_span = tds[3].select_one("span")
            if mill_span:
                rel_mill = mill_span.get_text(strip=True)

            rel_warehouse = ""
            wh_span = tds[4].select_one("span")
            if wh_span:
                rel_warehouse = wh_span.get_text(strip=True)

            rel_contact = ""
            rel_phone = ""
            contact_a = tds[5].select_one("a")
            if contact_a:
                text = contact_a.get_text(strip=True)
                m = re.match(r"(.+?)（(\d{11})）", text)
                if m:
                    rel_contact = m.group(1)
                    rel_phone = m.group(2)

            rel_remark = ""
            remark_span = tds[6].select_one("span")
            if remark_span:
                rel_remark = remark_span.get_text(strip=True)

            rel_price = ""
            rel_unit = ""
            price_span = tds[7].select_one(".jinqian-jine")
            if price_span:
                price_text = price_span.get_text(strip=True)
                m = re.search(r"(\d+)", price_text)
                if m:
                    rel_price = m.group(1)
                b_tag = price_span.select_one("b")
                if b_tag:
                    rel_unit = b_tag.get_text(strip=True).strip("/")

            rel_url = ""
            rel_link = tds[7].select_one("a[href]")
            if rel_link:
                rel_url = rel_link.get("href", "")

            rel_id = ""
            if rel_url:
                m = re.search(r"/(\d+)\.html", rel_url)
                if m:
                    rel_id = m.group(1)

            result["related_products"].append({
                "id": rel_id,
                "name": rel_name,
                "spec": rel_spec,
                "material": rel_material,
                "steel_mill": rel_mill,
                "warehouse": rel_warehouse,
                "price": rel_price,
                "unit": rel_unit,
                "contact": rel_contact,
                "phone": rel_phone,
                "remark": rel_remark,
                "image": rel_image,
                "detail_url": rel_url
            })
    return result


def fetch_detail(product):
    control.checkpoint()
    if not product["detail_url"]:
        return product
    detail_html = fetch(product["detail_url"])
    if not detail_html:
        raise RuntimeError(f"详情抓取失败: {product['detail_url']}")
    detail = parse_detail_page(detail_html)
    product["main_image"] = detail["main_image"]
    product["related_products"] = detail["related_products"]
    return product


def crawl_subcategory(cat_id, cat_name, sub_id, sub_data, collected=None):
    sub_name = sub_data["name"]
    log(f"  爬取品名: {sub_name} (cat={cat_id}, sub={sub_id})")

    all_products = collected if collected is not None else []
    url = build_list_url(cat_id, sub_id, 1)
    html = fetch(url)
    if not html:
        log(f"    无法访问列表页，跳过")
        return []

    products, total_pages = parse_list_page(html)
    if total_pages > 500:
        total_pages = 500
    log(f"    共 {total_pages} 页, 第1页 {len(products)} 条")
    all_products.extend(products)

    for page in range(2, total_pages + 1):
        try:
            url = build_list_url(cat_id, sub_id, page)
            html = fetch(url)
            if html:
                products, _ = parse_list_page(html)
                all_products.extend(products)
                if page % 50 == 0:
                    log(f"    列表第{page}/{total_pages}页, 累计 {len(all_products)} 条")
        except Exception as e:
            log(f"    列表第{page}页出错: {e}, 继续...")
            continue

    log(f"    列表共 {len(all_products)} 条, 并发抓取详情(并发数={_performance.concurrency})...")

    seen = set()
    unique_products = []
    for p in all_products:
        if p["id"] and p["id"] not in seen:
            seen.add(p["id"])
            unique_products.append(p)

    catalog = _active_catalog
    run_id = _active_run_id
    fetch_candidates = unique_products
    if catalog is not None and run_id is not None:
        product_ids = [product["id"] for product in unique_products]
        successful_ids = catalog.ids_with_statuses("lcgt", product_ids)
        catalog.register_discovered(
            "lcgt", ((product_id, sub_id) for product_id in product_ids), run_id
        )
        catalog.increment_run(run_id, skipped_count=len(successful_ids))
        fetch_candidates = [
            product for product in unique_products if str(product["id"]) not in successful_ids
        ]

    done_count = 0
    with ThreadPoolExecutor(max_workers=_performance.concurrency) as executor:
        futures = {executor.submit(fetch_detail, p): p for p in fetch_candidates}
        for future in as_completed(futures):
            product = futures[future]
            try:
                fetched = future.result()
                if catalog is not None and run_id is not None:
                    catalog.mark_success("lcgt", fetched["id"], fetched, run_id, sub_id)
                done_count += 1
                if done_count % 100 == 0:
                    log(f"    详情进度: {done_count}/{len(fetch_candidates)}")
            except Exception as e:
                done_count += 1
                if catalog is not None and run_id is not None:
                    catalog.mark_failed("lcgt", product.get("id", ""), e, run_id)
                log(f"    详情出错: {e}")

    log(
        f"    完成, 列表 {len(unique_products)} 条, "
        f"跳过详情 {len(unique_products) - len(fetch_candidates)} 条, "
        f"新增详情 {len(fetch_candidates)} 条"
    )
    return unique_products


def main(database_path=None):
    global _active_catalog, _active_run_id, _performance, _request_limiter
    _performance = settings_from_environment("lcgt")
    _request_limiter = RequestRateLimiter(_performance.request_interval_ms)
    output_dir = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "lcgt_products.json")
    log(f"数据文件: {output_file}")
    log(
        f"性能设置: 请求间隔={_performance.request_interval_ms}ms "
        f"并发={_performance.concurrency}"
    )

    existing_data = {}
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
            log(f"  已有数据: {sum(s['count'] for c in existing_data.values() for s in c.values())} 条")
        except:
            pass

    catalog = CatalogStore(
        database_path or resolve_database_path(Path(output_dir) / "catalog.sqlite3")
    )
    if not catalog.source_count("lcgt") and existing_data:
        batch = []
        imported = 0
        for categories in existing_data.values():
            if not isinstance(categories, dict):
                continue
            for section in categories.values():
                if not isinstance(section, dict):
                    continue
                category_id = section.get("subcategory_id")
                for product in section.get("products") or []:
                    if product.get("id"):
                        batch.append((product["id"], product, category_id))
                    if len(batch) >= SQLITE_BATCH_SIZE:
                        imported += catalog.bulk_import_success("lcgt", batch)
                        batch.clear()
        if batch:
            imported += catalog.bulk_import_success("lcgt", batch)
        log(f"SQLite 增量索引初始化: lcgt {imported} 条")
    run_id = catalog.start_run("lcgt")
    _active_catalog = catalog
    _active_run_id = run_id

    final_status = "failed"
    result = existing_data.copy()
    try:
        cat_map = get_categories()
        if not cat_map:
            log("未获取到类别数据")
            return

        for cat_id, cat_data in cat_map.items():
            cat_name = cat_data["name"]
            log(f"\n{'='*50}")
            log(f"类别: {cat_name}")
            log(f"{'='*50}")

            if cat_name not in result:
                result[cat_name] = {}

            for sub_id, sub_data in cat_data["subcategories"].items():
                sub_name = sub_data["name"]
                # Every run rescans lists; only successful detail requests are skipped.
                products = []
                complete = False
                try:
                    products = crawl_subcategory(cat_id, cat_name, sub_id, sub_data, products)
                    complete = not control.finishing()
                except control.FinishRequested:
                    log("结束请求：保存当前品名的部分数据")
                finally:
                    saved = result[cat_name].get(sub_name, {}).get("products", [])
                    merged = {str(p["id"]): p for p in saved if p.get("id")}
                    for product in products:
                        if product.get("id"):
                            key = str(product["id"])
                            previous = merged.get(key, {})
                            merged[key] = {
                                **previous,
                                **{k: v for k, v in product.items() if v or k not in previous},
                            }
                    products = list(merged.values())
                    result[cat_name][sub_name] = {
                        "category_id": cat_id,
                        "subcategory_id": sub_id,
                        "spec_key": sub_data["spec_key"],
                        "complete": complete,
                        "count": len(products),
                        "products": products,
                    }
                control.checkpoint()
        final_status = "success"
    except control.FinishRequested:
        final_status = "interrupted"
        raise
    finally:
        try:
            tmp_file = output_file + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, output_file)
            log("    已保存")
        finally:
            catalog.finish_run(run_id, final_status)
            stats = catalog.run_stats(run_id)
            log(
                "增量统计: "
                f"扫描={stats.get('scanned_count', 0)} "
                f"跳过={stats.get('skipped_count', 0)} "
                f"新增={stats.get('new_count', 0)} "
                f"失败={stats.get('failed_count', 0)}"
            )
            catalog.close()
            _active_catalog = None
            _active_run_id = None

    total = sum(sub["count"] for cats in result.values() for sub in cats.values())
    log(f"\n{'='*50}")
    log(f"爬取完成! 共 {total} 条产品")
    log(f"保存到: {output_file}")


if __name__ == "__main__":
    control.install()
    while True:
        try:
            main()
            log("爬取完成，退出")
            break
        except (control.FinishRequested, KeyboardInterrupt):
            log("用户中断")
            break
        except Exception as e:
            log(f"\n!!! 主程序异常: {e} !!!")
            log("30秒后自动重启...")
            time.sleep(30)
