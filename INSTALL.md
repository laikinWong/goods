# 商品数据采集：安装与运行

本项目提供中建三局严选、乐从钢铁、八达通三个平台的商品采集脚本，以及用于启动任务、暂停、继续、结束汇总和查看日志的网页控制台。

## 1. 环境要求

- Python 3：当前项目已在 Python 3.9 环境运行验证。
- 操作系统：Windows、macOS 或 Linux。已实现 Windows 专用进程控制；当前开发环境是 macOS，尚未进行 Windows 真机验收。
- 浏览器：用于访问本地控制台。
- 网络：执行真实采集时需要能够访问三个平台。

前端使用原生 HTML、CSS 和 JavaScript，无需安装 Node.js、npm、前端构建工具或数据库。

## 2. Python 依赖

第三方依赖记录在 `requirements.txt` 中：

| 依赖 | 用途 |
| --- | --- |
| `requests` | 乐从钢铁、八达通的 HTTP 请求 |
| `beautifulsoup4` | 乐从钢铁 HTML 页面解析 |
| `ijson>=3.2` | 流式迁移大型历史商品 JSON 文件 |
| `openpyxl>=3.1` | 按云商品模板生成分表数据 |
| `psutil>=5.9` | 仅 Windows：暂停、恢复和识别任务进程，安装依赖时自动按系统选择 |

爬虫结束时通过保存逻辑退出。Windows 网页控制台额外使用 psutil 管理进程，无需安装 Web 框架。

当前依赖清单未固定版本；不同时间安装可能获得不同版本。

## 3. 首次安装

### Windows（PowerShell 或 CMD）

安装 Python 后打开终端，进入项目目录（路径按实际位置修改）：

```powershell
cd C:\projects\goods-cli
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe dashboard.py
```

如果没有 `py` 命令，先确认 `python --version` 能显示 Python 3，再将第一条 Python 命令替换为 `python -m venv .venv`。后续直接使用虚拟环境内的解释器，无需激活，也不需要修改 PowerShell 执行策略。

浏览器打开 <http://127.0.0.1:8765>。之后每次进入项目目录运行 `.venv\Scripts\python.exe dashboard.py` 即可。

Windows 请从新版网页控制台启动正式采集任务，以便暂停、继续和结束汇总。旧版或独立命令行启动的 Windows 任务缺少结束请求通道，页面会拒绝“结束并汇总”，避免强制终止导致数据丢失；试运行任务很快自行完成。

Windows 的暂停和继续由 psutil 挂起与恢复进程线程；结束操作通过任务独立的请求文件通知爬虫，等当前请求完成后保存退出。参考：[psutil 文档](https://psutil.readthedocs.io/stable/)、[Python 子进程文档](https://docs.python.org/3/library/subprocess.html)。

### macOS / Linux


打开终端，进入项目根目录。以下路径是当前电脑上的项目位置，其他电脑请替换为实际路径：

```bash
cd /Users/chaoyue/Desktop/project/goods-cli
```

检查 Python：

```bash
python3 --version
```

创建并激活虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
```

安装依赖并检查是否能导入：

```bash
python -m pip install -r requirements.txt
python -c "import requests, bs4, ijson, openpyxl; print('依赖安装成功')"
```

虚拟环境只需创建一次。之后重新打开终端时，进入项目目录并执行 `source .venv/bin/activate` 即可。

## 4. 启动网页控制台

在项目根目录、虚拟环境已激活的终端中执行：

```bash
python dashboard.py
```

浏览器打开：<http://127.0.0.1:8765>。

启动网页服务本身不会自动开始采集，需要在页面点击启动按钮。服务仅监听本机地址。

如果默认端口被占用，可以更换端口：

```bash
python dashboard.py --port 8766
```

然后打开 <http://127.0.0.1:8766>。

## 5. 页面操作

| 操作 | 说明 |
| --- | --- |
| 试运行 | 检查执行命令与输出位置，不请求目标平台 |
| 启动 | 启动单个平台的采集任务 |
| 启动所选平台 | 并发启动勾选的平台，同一平台不能重复启动 |
| 暂停 | 挂起进程，保留内存和当前进度 |
| 继续 | 恢复暂停中的原进程 |
| 结束并汇总 | 请求停止后续采集，保存当前结果，并汇总到 `data/` |
| 查看日志 | 切换到对应任务的日志 |
| 下载当前日志 | 下载当前显示的最近一段日志，最多约 64 KB |
| 云商品数据对接 | 选择平台与全量/增量模式，将商品按模板处理到 `data/cloud-products/` |

云商品数据处理每个 Excel 最多写入 1000 条商品。增量处理只读取增量爬取实际新增成功且尚未处理的商品；处理结果直接保存在页面显示的本地目录，不提供浏览器下载。

任务状态导航默认显示“全部”，可切换“进行中”“已暂停”“已结束”“异常”。筛选时的 `0 / 6` 表示匹配当前条件的任务为 0 条，总任务记录为 6 条，不是采集进度。

日志每秒刷新一次。结束任务时可能需要等待当前网络请求或详情任务收尾；请等待“已结束并汇总”和输出文件路径出现。

“结束”后不能继续原进程，但重新启动可利用已有断点数据。暂停中的任务可以直接执行“结束并汇总”，程序会唤醒进程完成保存。

## 6. 数据和日志位置

| 路径 | 内容 |
| --- | --- |
| `data/` | 汇总后的 JSON 文件 |
| `logs/run-*/` | 每次任务的日志与任务状态记录 |
| `sjyx/outputs/full/` | 中建三局严选商品 JSONL、JSON 和断点记录 |
| `lcgt/lcgt_products.json` | 乐从钢铁商品数据和分类完成标记 |
| `bdt/data/` | 八达通分类、列表、详情与断点数据 |

手动结束生成的汇总文件格式：

```text
data/sjyx-<时间戳>-partial.json
data/lcgt-<时间戳>-partial.json
data/bdt-<时间戳>-partial.json
```

`partial` 表示任务提前结束时导出的阶段性结果。各平台保留自己的 JSON 结构，汇总内容也会包含断点续跑之前已保存的商品。

完整日志保存在磁盘上，页面只显示最近约 64 KB。新任务的暂停与继续不会清除已有数据。

## 7. 命令行运行

如果只需要执行采集、不使用网页，可在项目根目录运行：

```bash
# 并发启动三个平台
python run_crawlers.py --target all --parallel

# 单独启动一个平台
python run_crawlers.py --target sjyx
python run_crawlers.py --target lcgt
python run_crawlers.py --target bdt

# 检查命令，不执行真实采集
python run_crawlers.py --target all --parallel --dry-run
```

平台标识对应关系：`sjyx` 为中建三局严选，`lcgt` 为乐从钢铁，`bdt` 为八达通。

## 8. 关闭与重新启动

关闭浏览器不会停止采集。需要结束采集时，先在页面点击“结束并汇总”，确认保存完成，再在网页服务所在终端按 `Ctrl+C` 关闭服务。

macOS / Linux 下次启动（Windows 使用第 3 节的命令）：

```bash
cd /Users/chaoyue/Desktop/project/goods-cli
source .venv/bin/activate
python dashboard.py
```

不要直接强制终止爬虫来代替“结束并汇总”，否则尚未保存的内存数据可能丢失。旧版进程无法使用新增的保存逻辑，页面会提示其结束操作只能汇总已落盘数据。

## 9. 常见问题

### 提示缺少 requests 或 bs4

确认已激活虚拟环境，并使用同一个 Python 安装和运行：

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python dashboard.py
```

### 出现 NotOpenSSLWarning

当前电脑上的系统 Python 使用 LibreSSL 2.8.3，安装的 urllib3 v2 会输出兼容性警告。此前运行中该警告没有阻止采集，但不能据此保证所有 HTTPS 请求正常。

可换用链接 OpenSSL 的 Python 安装，重新创建虚拟环境并安装依赖。查看当前 Python 的 SSL 实现：

```bash
python -c "import ssl; print(ssl.OPENSSL_VERSION)"
```

### 页面打不开

检查 `python dashboard.py` 是否仍在运行，以及终端是否报错。浏览器端口要与启动参数一致，默认是 `8765`。

### 显示“已结束 · 状态未知”

通常表示旧进程已退出，但没有留下可确认的最终退出记录，例如被直接终止。它不代表进程仍在运行。

### 安装后如何验证

在项目根目录执行：

```bash
python -X utf8 -B -m unittest discover -s tests
```

中建三局严选的独立测试需在其目录执行：

```bash
cd sjyx
python -X utf8 -B -m unittest discover -s tests
```

Windows 测试使用 `.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests`。测试包含真实子进程保存退出与 Windows 进程接口模拟；Unix 专属信号测试在 Windows 跳过。

更多操作说明见 [USAGE.md](USAGE.md)。
