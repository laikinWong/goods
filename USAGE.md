# 爬虫项目操作文档

## 网页控制台

在项目根目录运行 `python3 dashboard.py`，打开 <http://127.0.0.1:8765>。
可用 `python3 dashboard.py --port 8766` 更换端口。沿用现有 Python 依赖，无需 Node.js。

- 选择一个或多个平台并发启动，或点击单个平台的「启动」。
- 每个平台可分别填写「请求间隔（毫秒）」和「并发数量」。请求间隔范围为 `0-600000` 毫秒，并发范围为 `1-32`；启动后参数随任务固化，运行期间不能修改。
- 请求间隔表示同一平台相邻 HTTP 请求开始时间的最小间隔。即使并发大于 1，也不会在同一瞬间集中发起请求；并发用于覆盖网络等待时间。
- 「试运行」只验证命令与输出位置，不请求目标网站。
- 页面每秒刷新状态和日志，显示最近 64 KB，可下载当前显示的日志片段。
- 「暂停」挂起进程（Windows/macOS/Linux），保留内存；「继续」恢复原进程。暂停中的任务也会阻止同一平台重复启动。
- 「结束并汇总」会请求爬虫停止后续请求，等待当前请求和正在执行的详情任务收尾、保存结果，然后生成 `data/<平台>-<时间戳>-partial.json`。页面显示汇总路径和条数，0 条时也生成有效的空 JSON。结束后不能继续原进程，需要重新启动。
- 结束暂停中的任务会自动唤醒进程以完成保存。结束过程可能等待当前网络请求超时，请等待「已结束并汇总」，不要强制杀进程。
- 中建三局严选导出已写入 JSONL 的商品；八达通保存内存中的详情；乐从钢铁保存当前品名的部分数据并标记未完成，下次启动会重新采集该品名。
- 旧版命令行任务支持暂停、继续和结束，但结束只能汇总已落盘的数据，未保存的内存数据无法恢复。页面会提示此限制。
- 完整日志保存在 `logs/run-*/`，正常完成的结果也会复制到 `data/`。汇总文件保留各平台原有 JSON 结构，包含断点续跑之前已保存的记录。汇总失败会显示错误并保留源文件。
- 控制台仅监听本机。关闭浏览器不会停止任务；建议爬取结束后再退出 Python 服务，以确保结果汇总完成。
- 「云商品数据对接」支持单选、多选或全选平台，可执行全量处理或与增量爬取批次关联的增量处理。每个 Excel 最多 1000 条商品，结果保存在 `data/cloud-products/<处理任务>/`，页面显示进度和最终目录。
- 服务重启后会恢复任务记录和暂停状态；仍存活的任务可继续控制。没有退出记录的旧任务会标记「状态未知」，不会推断为成功。

控制台测试：`python3 -m unittest discover -s tests`。

## 1. 项目结构

```text
goods/
  run_crawlers.py          # 统一启动入口
  sjyx/                    # 3jyx 爬虫
  lcgt/                    # 乐从钢铁爬虫
  bdt/                     # 八达通爬虫
  data/                    # 统一汇总输出目录，运行时自动创建
  logs/                    # 运行日志目录，运行时自动创建
```

推荐优先使用根目录的 `run_crawlers.py` 启动爬虫。它可以单独启动、批量启动、并发启动，并把各爬虫主输出统一复制到 `data/` 目录。

## 2. 环境准备

建议使用 Python 3。

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

说明：

- `sjyx` 爬虫主要使用 Python 标准库。
- `lcgt` 需要 `requests` 和 `beautifulsoup4`。
- `bdt` 需要 `requests`。
- Windows 控制台额外需要 `psutil`，依赖清单会自动安装。完整 Windows 安装命令见 [INSTALL.md](INSTALL.md)。
- Windows 正式采集请从新版控制台启动，独立 CLI 任务没有保存后结束的请求通道。

## 3. 启动前检查

在项目根目录执行：

```bash
python3 run_crawlers.py --help
```

如果能看到参数说明，说明统一入口可以正常加载。

建议先 dry-run，确认将要启动哪些爬虫、日志和数据文件会写到哪里：

```bash
python3 run_crawlers.py --target all --parallel --dry-run --quiet
```

dry-run 不会真实请求网站。

## 4. 常用运行命令

### 4.1 启动全部爬虫，并发运行

```bash
python3 run_crawlers.py --target all --parallel
```

### 4.2 只启动一个爬虫

```bash
python3 run_crawlers.py --target sjyx
python3 run_crawlers.py --target lcgt
python3 run_crawlers.py --target bdt
```

### 4.3 启动多个指定爬虫

```bash
python3 run_crawlers.py --target sjyx lcgt --parallel
```

### 4.4 后台运行

macOS / Linux 可以使用：

```bash
nohup python3 run_crawlers.py --target all --parallel --quiet > crawler.out 2>&1 &
```

`--quiet` 只关闭子爬虫实时输出，最终汇总仍会打印到终端或 `crawler.out`。

## 5. 输出数据

统一输出目录默认为：

```text
data/
```

每个爬虫成功结束后，会生成一个带时间戳的 JSON 文件：

```text
data/sjyx-YYYYMMDDHHMMSS.json
data/lcgt-YYYYMMDDHHMMSS.json
data/bdt-YYYYMMDDHHMMSS.json
```

示例：

```text
data/sjyx-20260702090721.json
data/lcgt-20260702090721.json
data/bdt-20260702090721.json
```

指定其他数据输出目录：

```bash
python3 run_crawlers.py --target all --parallel --data-dir /path/to/data
```

注意：统一输出是各爬虫的主结果文件副本。各爬虫自己的断点文件、缓存文件或中间文件仍保留在各自目录下。

### 5.1 SQLite 增量索引

通过统一入口启动时，三个爬虫共享：

```text
data/catalog.sqlite3
```

每轮仍会扫描商品列表以发现新增商品，但只有数据库中不存在或状态为 `failed`、`pending`、`partial` 的商品才会请求详情。状态为 `success` 的商品会直接跳过详情请求。

SQLite 是增量判重和失败重试的可信状态；原有 JSON 文件继续生成，供控制台、人工查看和下游兼容使用。

已有数据第一次运行时会自动为各爬虫建立 SQLite 索引。正式迁移大量历史文件时，推荐先显式执行迁移命令：

```bash
python3 migrate_catalog.py \
  --database data/catalog.sqlite3 \
  --sjyx /path/to/sjyx-products.jsonl \
  --bdt /path/to/goods_details_all.json \
  --lcgt /path/to/lcgt_products.json
```

参数可以只指定其中一个来源。迁移使用 `(source, product_id)` 唯一键，可重复执行，不会产生重复商品。大型 `.json` 文件通过 `ijson` 流式读取；迁移前请先安装 `requirements.txt` 中的依赖。

迁移完成后会输出每个来源的原始记录数、有效记录数、无 ID 记录数、本次新增唯一商品数和数据库总数。原始 JSON 文件不会被修改或删除。

## 6. 日志

统一日志目录默认为：

```text
logs/
```

每次运行会创建一个独立日志目录：

```text
logs/run-YYYYMMDD-HHMMSS/
  orchestrator.log
  sjyx.log
  lcgt.log
  bdt.log
```

文件说明：

- `orchestrator.log`：统一调度器日志，记录启动目标、进程 PID、退出码、耗时、统一输出路径。
- `sjyx.log`：`sjyx` 爬虫原始输出。
- `lcgt.log`：`lcgt` 爬虫原始输出。
- `bdt.log`：八达通爬虫原始输出。

指定其他日志目录：

```bash
python3 run_crawlers.py --target all --parallel --log-dir /path/to/logs
```

## 7. 参数说明

```text
--target
```

必填。指定要启动的爬虫。

可选值：

```text
sjyx
lcgt
bdt
all
```

```text
--parallel
```

并发启动所选爬虫。不加该参数时按顺序运行。

```text
--dry-run
```

只打印将要执行的命令和输出位置，不真实启动爬虫。

```text
--quiet
```

不在终端实时打印子爬虫输出。完整输出仍会写入日志文件。

```text
--continue-on-error
```

仅对顺序运行有效。某个爬虫失败后继续执行下一个。

```text
--log-dir
```

指定日志目录，默认 `logs/`。

```text
--data-dir
```

指定统一数据输出目录，默认 `data/`。

## 8. 验证命令

运行统一入口测试：

```bash
python3 -m unittest tests/test_run_crawlers.py
```

运行 `sjyx` 现有测试：

```bash
cd sjyx
python3 -m unittest tests/test_sjyx_crawler.py
```

语法检查：

```bash
python3 -m py_compile run_crawlers.py lcgt/lcgt_crawler.py bdt/scraper.py
```

## 9. 常见问题

### 9.1 dry-run 有输出文件路径，但 data 目录里没有文件

这是正常的。`--dry-run` 不会真实启动爬虫，也不会复制主输出文件。

### 9.2 某个爬虫失败后没有生成 data 文件

统一输出只在对应爬虫退出码为 `0` 且主输出文件存在时生成。如果爬虫失败，先查看对应日志：

```text
logs/run-YYYYMMDD-HHMMSS/<target>.log
```

### 9.3 并发运行时其中一个失败，其他会停止吗

不会。并发模式下，一个爬虫失败不会停止已经启动的其他爬虫。调度器会等待全部进程结束，并在最后汇总成功或失败状态。

### 9.4 顺序运行时失败后会继续吗

默认不会。需要继续执行下一个爬虫时，加：

```bash
python3 run_crawlers.py --target all --continue-on-error
```
