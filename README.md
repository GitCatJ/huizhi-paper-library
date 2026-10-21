# 试卷本地题库

指定一个有公考试卷的本地目录，递归识别试卷、题目、材料、选项、来源答案和解析，写入独立 SQLite 数据库，再通过本地网页或命令行查询。

从「绘智公考」项目抽取解析核心，重新实现独立的导入、任务恢复和查询层。无需启动原项目、Docker、数据库服务或 AI 模型。

## 适用文件

面向当前下载器生成的 **HTML 格式 `.doc`**，以及同结构的 `.html` / `.htm`。典型目录：

```text
试卷目录/
  行测/
    2024_山东_省考_示例试卷_真题.doc
    2024_山东_省考_示例试卷_答案.doc
    2024_山东_省考_示例试卷_解析.doc
  申论/
    2024_山东_省考_示例申论_真题.doc
    2024_山东_省考_示例申论_解析.doc
```

支持递归子目录，也支持直接把文件放在指定目录中。按同目录、同名主体配对「真题 / 答案 / 解析」。没有配套答案或解析的真题仍可录入，缺失内容保留标记。普通 HTML 文件名会保留原名，年份、地区无法确定时不猜测。

**格式边界：**传统二进制 Word `.doc`、`.docx`、PDF、扫描图片的 OCR 尚未实现。扫描会报告不支持的文档；二进制内容会记录为失败，避免生成看似成功的假题目。仅扩展名符合要求不代表版式能被正确拆题，未拆分内容和材料关联歧义会保留待核对标记。

## 安装

需要 Python 3.11 或更新版本。Windows PowerShell：

```powershell
cd "D:\Users\Documents\CODE\试卷本地题库"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

也可运行 `scripts/Setup.ps1`。其他系统使用对应虚拟环境的 Python；下面以 Windows 为例。安装后的 `paper-library` 命令与 `python run.py` 等价。

## 先扫描、再试跑

扫描只读取文件目录，不创建数据库、不联网下载：

```powershell
.\.venv\Scripts\python.exe run.py scan --source "E:\Temp\试卷下载"
```

先导入少量试卷，检查实际拆题结果：

```powershell
.\.venv\Scripts\python.exe run.py import --source "E:\Temp\试卷下载" --limit 10
.\.venv\Scripts\python.exe run.py verify
.\.venv\Scripts\python.exe run.py serve
```

浏览器打开 [本地题库页面](http://127.0.0.1:8913)，可浏览试卷、逐题查看材料/选项/答案/解析，按年份、地区、科目和关键词筛选。页面只读，导入进程和查询服务可以同时运行。端口被占用时指定 `serve --port 8914`。

## 全量导入与后台运行

在本机终端手动运行，移除 `--limit` 即处理全部可识别试卷。已成功导入且文件内容和解析版本未变化的试卷自动跳过：

```powershell
.\scripts\Start-Import.ps1 -Source "E:\Temp\试卷下载"
```

脚本以隐藏窗口启动后台 Python 进程，打印 PID、输出日志和错误日志路径。终端关闭后导入继续；电脑睡眠/关机或进程终止后需恢复。也可在前台运行：

```powershell
.\.venv\Scripts\python.exe run.py import --source "E:\Temp\试卷下载"
```

进度由本地进程写入数据库和 JSONL 日志。控制台与「导入进度」页面显示阶段、已完成/总量、成功/跳过/失败数、开始时间、最近更新时间、预计剩余时间和日志位置；ETA 来自当前执行段实测速度，刚开始时为未知。可随时读取一次，无需 Codex 轮询：

```powershell
.\.venv\Scripts\python.exe run.py status
```

完成判断：

- `completed`：本次任务全部完成。
- `completed_with_errors`：存在失败试卷或扫描问题，查看状态输出、页面和日志。
- `interrupted`：捕获到中断，已提交的试卷保留。
- 强制结束进程可能留下 `running` 状态；确认原进程已经退出后，使用原任务 ID 续跑。

每卷独立提交，解析或提交失败不会破坏已入库内容。异常结束后使用 `status` 中的 `job_id`：

```powershell
.\.venv\Scripts\python.exe run.py import --resume import_替换为实际任务ID
# 或后台续跑
.\scripts\Start-Import.ps1 -Resume import_替换为实际任务ID
```

`--resume` 固定使用原任务清单，只处理未完成/失败项。目录中后来增加的试卷、后来补齐的配套文件，需重新执行带 `--source` 的导入，才能重新扫描；已有成功项仍会跳过。文件被删除时不会自动删库。源目录移动会视为新来源，避免丢失出处。

## 图片离线显示

导入保留图片原始顺序、位置、尺寸声明和 URL，**默认不下载远程图片**。未本地化图片在页面显示占位提示；浏览页面不会自动访问源站。

先试下载 100 张：

```powershell
.\.venv\Scripts\python.exe run.py images --limit 100 --workers 8
```

确认后，手动去掉 `--limit` 处理全部待下载图片。此步骤需要联网且可能耗时较长，请根据试跑速度估计时间和磁盘空间。成功图片先解码验证，再按 SHA-256 存储，相同图片仅存一份。失败信息、尝试次数和最新进度持久保存：

```powershell
.\.venv\Scripts\python.exe run.py images --workers 8
.\.venv\Scripts\python.exe run.py images --retry-failed --workers 4
```

`images --retry-failed` 处理待下载项和失败项；正常续跑不会重下成功项。TLS 证书验证保持开启，源站证书错误会如实记为失败，不以不安全下载冒充完成。本地相对图片路径仅允许读取试卷文件目录内的图片。图片任务也使用同一题库写入锁，避免与导入重叠运行。

## 命令行检索与导出

```powershell
.\.venv\Scripts\python.exe run.py search "基层治理" --year 2024 --category "行测"
.\.venv\Scripts\python.exe run.py export --paper paper_实际ID --output data\exports\paper.json
```

题目全文包含题干、选项、来源答案和解析。三个字符及以上的中文关键词优先使用 SQLite FTS5 trigram 索引；短词以及不支持 trigram 的 SQLite 使用字面子串匹配。结果每页 30 条，`--page 2` 翻页。材料单独存储，可在试卷和题目详情中查看；材料全文暂未纳入搜题索引。

## 数据位置与资源

默认数据库为**当前工作目录**的 `data/library.sqlite3`。各命令支持 `--db`；后台脚本默认固定使用项目内的题库，也支持 `-Database`：

```powershell
.\.venv\Scripts\python.exe run.py import --source "E:\Temp\试卷下载" --db "F:\题库\library.sqlite3"
.\.venv\Scripts\python.exe run.py serve --db "F:\题库\library.sqlite3"
```

```text
data/
  library.sqlite3        题库、全文索引、导入清单与进度
  sources/               按 SHA-256 保存的解析时原文快照
  images/                已验证的本地图片
  logs/                  每次任务的 JSONL、后台启动日志、图片进度
```

原试卷只读。单卷解析、逐卷事务，不把整库题目载入内存；实际内存取决于最大单套试卷。磁盘至少应容纳数据库、全文索引和一份原文归档，图片空间另计，宜预留数 GB 余量。全量耗时由文件大小、磁盘和图片源站速度决定，以本机小样本及进度 ETA 为准；尚未对 4,709 套执行独立项目全量基准。

导入用文件内容哈希判断变更。更新试卷时保存旧解析版本和原文快照，保留来源；跨卷重复题不自动合并。数据库有独立应用标识，拒绝连接其他项目已有数据库。备份应在停止导入和查询服务后复制整个 `data` 目录，不能只复制正在写入的 `.sqlite3` 主文件。当前归档和图片记录包含绝对路径，备份恢复到原位置可直接使用；移动数据目录后的路径迁移尚未实现。

**本地解析、检索和图片处理均不调用 Codex CLI、OpenAI API 或第三方模型，不按试卷数量消耗模型 Token。**

## 开发与验证

```powershell
python -m unittest discover -s tests -v
python run.py verify
```

测试均使用临时目录中的合成试卷，不要求访问原项目或真实题库。CI 配置覆盖 Windows/Linux、Python 3.11/3.13。测试范围包括图文选项、材料关联、中文搜索、增量导入、修改后更新、事务回滚、中断恢复、原文快照、图片重试和只读页面。

2026-10-21 本机验证：17 项自动化回归全部通过；从五个科目各选最早/最新两套，共 10 套真实试卷，录入 450 道题、42 份材料，独立样本库一致性检查通过。样本包含 436 个待本地化图片地址，未发起远程图片下载。样本中的 6 道来源占位题和 2 道缺少来源答案的题目保留质量标记；不据此宣称全库识别率。

仓库只保存源代码、文档和合成测试逻辑。运行数据库、真实试卷、原文归档、图片、日志和虚拟环境均被 `.gitignore` 排除。代码来源及抽取边界见 [架构说明](docs/ARCHITECTURE.md)。
