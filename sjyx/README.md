# 3jyx 商品爬虫

接口型爬虫，用于抓取 `https://www.3jyx.cn/` 公开商品信息。输出为 JSON/JSONL，落盘前会递归移除价格相关字段。

## 运行

小样本验证：

```bash
python3 sjyx_crawler.py --output-dir outputs/smoke --max-categories 1 --max-pages-per-category 1 --page-size 2
```

正式抓取：

```bash
python3 sjyx_crawler.py --output-dir outputs/full --page-size 200 --min-delay 1.5 --max-delay 4.0 --export-json
```

更保守的反爬参数：

```bash
python3 sjyx_crawler.py --output-dir outputs/full --page-size 100 --min-delay 3 --max-delay 8 --max-retries 3 --export-json
```

如果只是想先确认列表覆盖情况，不抓详情：

```bash
python3 sjyx_crawler.py --output-dir outputs/list-only --page-size 200 --min-delay 1.5 --max-delay 4.0 --no-detail
```

详情抓取默认每 20 条打印一次进度，可调整：

```bash
python3 sjyx_crawler.py --output-dir outputs/full --detail-log-every 10
```

## 输出

- `products.jsonl`: 主输出，每行一个商品对象，适合断点续跑和大文件写入。
- `products.json`: 使用 `--export-json` 时生成的 JSON 数组。
- `catalog.sqlite3`: SQLite 增量索引；通过统一入口运行时使用根目录 `data/catalog.sqlite3`。
- `state.json`: 旧版断点文件，仅为兼容历史数据保留；新版判重以 SQLite 为准。
- `errors.jsonl`: 详情失败、分类上限等错误记录；没有错误时不会创建。

商品对象结构：

```json
{
  "category": {
    "ids": [],
    "names": [],
    "cascade_id": "",
    "leaf_id": ""
  },
  "product": {
    "spu_sys_no": "",
    "sku_sys_no": "",
    "name": "",
    "brand": "",
    "shop_name": "",
    "shop_sys_no": "",
    "unit": "",
    "images": [],
    "attributes": [],
    "spec_model": "",
    "listing": {},
    "detail": {}
  }
}
```

## 反爬处理

- 默认低并发：脚本是单线程请求。
- 默认随机延迟：每次请求间隔 `1.5-4.0` 秒。
- 失败指数退避：网络错误或接口异常最多重试 3 次。
- 增量抓取：每轮重新扫描分类列表以发现新品，只跳过 SQLite 中详情状态为 `success` 的 SKU。
- 失败重试：详情失败记录为 `failed`，下一轮仍会再次尝试。
- 不绕过验证码、不抓未授权登录数据。

如果 `errors.jsonl` 中出现 `category_limit`，表示该分类可能命中接口 `5000` 条上限，需要进一步按品牌、属性或价格以外的筛选条件拆分。

## 运行时间预估

带详情抓取时，每个商品至少会多一次详情接口请求。比如某个分类一页有 200 条，且请求延迟为 `1.5-4.0` 秒，这一页详情大约需要 5-13 分钟。脚本不是卡住，而是在逐条请求详情；终端会输出类似：

```text
detail page=1 item=20/200 sku=...
```

## 测试

```bash
python3 -m unittest tests/test_sjyx_crawler.py
```
