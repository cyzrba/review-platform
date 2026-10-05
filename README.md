# 学生作业 / 实验报告自动评审平台

面向高校教师的批量批改工具：

> 导入班级名单 → 建评分细则 → 上传压缩包 → AI 评审出分与评语 → 按班级导出成绩

**当前状态**：整条链路（导入 / 上传 / 解包 / 匹配 / 入库 / 导出）已经跑通并验证过；
AI 评审是**可插拔的占位实现**（`mock` 评审器按文件内容稳定出分），
真实大模型只需实现一个方法，接入点见下方「接入真实 AI 评审」。

## 功能一览

| 页面 | 做什么 |
| --- | --- |
| 概览 | 课程 / 班级 / 学生 / 评分细则 / 评分结果的总量、评审进度、文件存储状态、**i学习 浏览器连接状态** |
| 班级与学生 | 三栏级联：**先建课程 → 选中课程 → 挂班级 / 导入名单 → 看学生** |
| 评分细则 | 一个项目 / 一次作业一份细则，可挂到多个课程；含类型、满分、任务说明、评分标准 |
| 评审与成绩 | 选课程 → 选类型 → 选细则 → 上传压缩包 → 自动匹配学生 → 一键 AI 评审 → 复核 → 导出 |
| 评审与成绩（i学习 作业） | 选课程 → 从 i学习 同步作业 → 按班级抓作业附件 → 自动抽出作答图片并匹配学生 |

关键设计：

- **课程是最外层单位**。课程下面挂班级（一个课程多个班级，一个班级也能同时挂多个课程），
  并关联自己的评分细则。导入名单必须先选课程，新建或复用的班级会自动挂到该课程下。
- **不分版本、不分维度**。一份评分细则对应一个项目 / 一次作业，AI 只输出**一个总分 + 一段评语**。
- **上传不用先选班级**。学生是按文件名里的学号 / 姓名自动匹配出来的，班级从学生反推，所以一个压缩包可以跨班级；
  需要时也可以用「限定匹配范围」把匹配锁在某个课程或某个班级内。
- **只有一张结果表**。不管上传的是压缩包还是单个文件，最终都只落成一条条评分结果，没有再单独记上传批次。
- **未匹配的文件不会静默丢失**。上传响应里会列出被跳过的文件和原因，页面上直接展示。
- **所有列表接口统一分页**：`?page=1&page_size=20`，统一返回
  `{"items": [...], "total": 120, "page": 1, "page_size": 20, "pages": 6}`（`page_size` 上限 200）。

## 技术栈

| 层 | 选型 |
| --- | --- |
| 后端 | FastAPI + SQLAlchemy 2.0 + SQLite |
| 文件存储 | 本地磁盘（`backend/data/objects/`），不需要 Docker / MinIO |
| 前端 | Vue 3 + Vite + Element Plus + vue-router |
| 导出 | openpyxl 生成 Excel（含班级统计页），另附 CSV |

## 目录结构

```
review-platform/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口、CORS、建表
│   │   ├── config.py            # 全部配置走环境变量 / .env
│   │   ├── database.py          # SQLite 连接、外键、WAL、旧库结构自检
│   │   ├── models.py            # 14 张表的 ORM 定义
│   │   ├── pagination.py        # 统一分页参数与响应结构
│   │   ├── schemas.py           # 请求 / 响应模型
│   │   ├── serializers.py       # ORM -> 响应模型
│   │   ├── storage.py           # 文件读写（LocalStorage，key 是相对路径）
│   │   ├── routers/             # courses / classes / rubrics / grading / exports / files / system
│   │   └── services/
│   │       ├── archive.py       # 嵌套压缩包展开 + 中文文件名修复 + 学生匹配
│   │       ├── importer.py      # CSV / XLSX 名单导入
│   │       ├── reviewer.py      # AI 评审器接口 + mock 实现 + prompt 组装
│   │       ├── grading.py       # 评审调度、并发、结果入库
│   │       ├── document.py      # 文档取文本（PDF / Word 留了 TODO）
│   │       ├── istudy.py        # 深职 i学习 抓取客户端（名单 + 作业 + 实验报告）
│   │       ├── homework_sync.py # 作业列表同步 + 导出附件 / 下载 / 解包 / 匹配
│   │       ├── lab_sync.py      # 实验报告同步 + 导出作答 / 解包抽图 / 入库
│   │       ├── answer_files.py  # 从学生提交里抽「作答图片」（PyMuPDF）
│   │       └── exporter.py      # Excel 导出
│   ├── .env.example             # 配置模板（复制成 .env 用）
│   ├── scripts/                 # smoke_test / e2e_http
│   └── tests/                   # 35 个 pytest 用例
├── frontend/                    # Vue3 前端（4 个页面）
├── start.bat / stop.bat         # Windows 一键启停（含「i学习浏览器」启动器）
└── Makefile                     # macOS / Linux 下的等价命令
```

## 快速开始

### 1. 文件存储

不需要任何外部服务：抓下来的作业 / 实验报告直接落在 `backend/data/objects/`，
数据库是 `backend/data/review.db`。两个路径都跟着项目目录走，**换盘符、换电脑都不用改配置**；
想放到别处就在 `.env` 里加 `LOCAL_STORAGE_DIR` / `DATABASE_URL`（写法见 `.env.example`）。

整个 `backend/data/` 可以直接拷贝搬走 —— 数据库里存的是相对 key
（如 `istudy/lab-reports/25人工智能本1-单摆测量重力加速度实验/students/...`），不带盘符。

### 2. 后端

```bash
cd backend
cp .env.example .env                 # 按需填大模型 key；数据库 / 存储路径不填就用默认
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r pyproject.toml pytest httpx

PYTHONPATH=. ./.venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

接口文档：<http://127.0.0.1:8010/docs>

> 默认端口是 **8010**（这台机器上 8000 已被其它服务占用）。改端口要同步改 `frontend/vite.config.js` 里的代理目标。

### 3. 前端

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>（端口被占用时 Vite 会自动顺延）。前端通过 Vite 代理访问 `/api`。

### 4. 验收

```bash
make test      # 31 个后端用例
make smoke     # 进程内跑完整流程（导入→细则→压缩包→评审→导出）

cd backend && PYTHONPATH=. ./.venv/bin/python scripts/e2e_http.py http://127.0.0.1:8010
```

## 数据表设计

一共 **14 张表**：7 张平台自己的业务表，7 张用来承接从「深职 i学习」抓下来的作业与实验报告。

| 表 | 说明 | 关键字段 |
| --- | --- | --- |
| `courses` | 课程（最外层组织单位） | `name`(唯一)、`code`(课程代码)、`term`(学期) |
| `course_classes` | 课程-班级关联，多对多 | `course_id`、`class_id`；唯一约束 `(course_id, class_id)` |
| `course_rubrics` | 课程-评分标准关联，多对多 | `course_id`、`rubric_id`；唯一约束 `(course_id, rubric_id)` |
| `classes` | 班级，由「院系 + 专业 + 班级名称」唯一确定 | `department`、`major`、`name`；唯一约束 `(department, major, name)` |
| `students` | 学生，归属唯一班级 | `class_id`、`student_no`(学号/工号)、`name`、`joined_at`(加入时间)、`enrollment_year`(入学年份)；唯一约束 `(class_id, student_no)` |
| `rubrics` | 评分细则 = 一个项目 / 一次作业，不分版本不分维度 | `name`、`kind`(homework/lab_report)、`description`、`criteria`、`extra_prompt`、`total_score` |
| `grading_results` | 评分结果：一个细则 × 一个学生 = 一条 | `rubric_id`、`student_id`、`class_id`、`source_filename`、`object_key`、`status`、`score`(总分)、`comment`(评语)、`model`、`graded_at`、`is_manual`；唯一约束 `(rubric_id, student_id)` |
| `istudy_works` | i学习 的「一次作业 × 一个班级」。一次作业发给多个班就会拆成多个批次（`istudy_task_id`），每班有自己的 `istudy_work_id` | `course_id`、`class_id`、`rubric_id`、`istudy_cid`、`istudy_clazzid`、`istudy_task_id`、`istudy_library_id`、`istudy_work_id`、`name`、`status`、`start_at`、`end_at`、`submitted_count`、`unsubmitted_count`、`pending_count`；唯一约束 `(istudy_work_id)` |
| `istudy_submissions` | 某学生在某次作业下的提交（**没交也会有一行**） | `work_id`、`student_id`、`istudy_answer_id`、`state`(已交/已保存/未交)、`submitted_at`、`source_filename`、`object_key`、`file_state`、`image_count`、`extracted_text`；唯一约束 `(work_id, student_id)` |
| `istudy_submission_files` | 学生提交里拆出来的单个文件，主要就是**作答图片** | `submission_id`、`seq`(顺序)、`role`(answer/question)、`filename`、`object_key`、`content_type`、`size_bytes`、`checksum` |
| `istudy_exports` | i学习「导出作业附件」任务。导出是异步的（一个班约 3 分钟），要落库轮询 | `work_id`、`content`、`fmt`、`status`、`istudy_download_id`、`download_url`、`object_key`、`size_bytes`、`file_count`、`message` |
| `istudy_lab_reports` | i学习 的「一次实验报告 × 一个班级」。一个报告记录发给多个班就拆成多行 | `course_id`、`class_id`、`rubric_id`、`istudy_report_id`、`istudy_class_id`、`istudy_dept_id`、`name`、`report_type`(1 表单/2 附件/3 Word)、`sync_state`、`submitted_count`；唯一约束 `(istudy_report_id, istudy_class_id)` |
| `istudy_lab_submissions` | 某学生在某次实验报告下的提交（**没交也会有一行**） | `report_id`、`student_id`、`istudy_fill_id`、`state`、`submitted_at`、`source_filename`、`object_key`、`file_state`、`image_count`、`extracted_text`；唯一约束 `(report_id, student_id)` |
| `istudy_lab_files` | 实验报告 PDF 按页渲染出来的作答图片 | `submission_id`、`seq`、`role`、`filename`、`object_key`、`content_type`、`size_bytes`、`checksum` |

关系：

```
courses ─N:M─ classes        （course_classes）
courses ─N:M─ rubrics        （course_rubrics）
classes ─1:N─ students ─1:N─ grading_results ─N:1─ rubrics
                                    │
                                    └─ 冗余 class_id，便于直接按「项目 × 班级」出成绩
```

展开说就是：**课程下挂班级、也关联评分细则；班级下有学生；学生提交后产生评分结果；
每条结果既属于一个学生，也属于一份评分细则（项目）。**

i学习 作业那 4 张表是另一条支线：

```
courses ─1:N─ istudy_works ─N:1─ rubrics     （一次 i学习 作业 = 一份细则）
                    │
                    ├─N:1─ classes           （这次作业发给了哪个班）
                    ├─1:N─ istudy_submissions ─N:1─ students
                    │           └─1:N─ istudy_submission_files   （抽出来的作答图片）
                    └─1:N─ istudy_exports    （导出任务）
```

实验报告是同一条思路的复制（i学习 那边是另一套页面，所以单独建表）：

```
courses ─1:N─ istudy_lab_reports ─N:1─ rubrics   （一次实验报告 = 一份细则）
                    │
                    ├─N:1─ classes               （这次报告发给了哪个班）
                    └─1:N─ istudy_lab_submissions ─N:1─ students
                                └─1:N─ istudy_lab_files   （PDF 按页渲染出来的作答图）
```

几个刻意的取舍：

- **班级是全局实体，课程关联只是"挂靠"**。`classes` 仍然按「院系 + 专业 + 班级名称」唯一，
  同一个班级挂到第二个课程时复用同一条记录、只加一条关联。所以删除课程只删关联，不会动班级、学生和细则。
- **按课程看成绩不需要在结果表里存 `course_id`**。查询时用 `course_id` 过滤「该课程下的班级」即可
  （`/api/rubrics/{id}/results?course_id=`、导出接口同理）。这样同一份细则被两个课程复用时不会有归属歧义。
- **`grading_results` 上 `(rubric_id, student_id)` 唯一**：同一个学生重新上传是覆盖（旧文件留在对象存储里备查，不删），
  评审状态和分数会被重置。这样「谁交了谁没交」天然不重复。
- **`class_id` 冗余存在结果表里**：虽然能从学生反推，但成绩单是按「项目 × 班级」出的，
  直接存一份可以让按班级筛选、按班级统计和导出都是一次索引扫描。
- **没有上传批次表 / 未匹配文件表**：上传只是产生评分结果的动作，被跳过或匹配不上的文件在上传响应里返回，
  页面上实时展示（见「压缩包与匹配规则」）。要留痕的话在响应里也能拿到完整清单。
- **没有做用户 / 权限表**：定位是单机教师工具，没做登录。要多人的话加 `users` 表并在 `courses` 上挂 `owner_id` 即可。

### 对象存储目录约定

```
rubrics/{rubric_id}/results/{uuid}/{提交文件名}
```

i学习 抓下来的东西按「班级-任务名」分文件夹，直接翻磁盘也看得懂：

```
istudy/works/{班级}-{作业名}/            作业
    exports/                             整包 zip，抓完可以删
    students/{学号}/{原文件}              学生交的原始文件
    students/{学号}/answer/{序号}-{文件名} 抽出来的作答图片（喂给 AI 的就是这些）

istudy/lab-reports/{班级}-{实验名}/       实验报告，结构同上
```

## 压缩包与匹配规则

### 两种任务的文件结构

**实验报告（lab_report）**：上传的压缩包里是「每个学生一个压缩包」，
名为 `学生名字_学号.zip`，里面是 `学生名字_学号.pdf`。

```
实验一批次.zip
├── 张三_2023010101.zip
│   └── 张三_2023010101.pdf
└── 李四_2023010102.zip
    └── 李四_2023010102.pdf
```

**作业（homework）**：压缩包里既有压缩包也有 Word 文件，**只认 Word**（`.doc` / `.docx`），
文件名为 `院系-专业-班级名称-学号-学生名称`。Word 藏在内层压缩包里也能翻出来。

```
第3次作业.zip
├── 计算机学院-软件工程-软工2301-2023010101-张三.docx     ← 用这个
├── 素材.zip
│   └── 计算机学院-计算机科学与技术-计科2302-2023010103-王五.docx  ← 也会被翻出来
└── 说明.txt                                              ← 跳过
```

### 中文文件名

Windows 打的 zip 常把中文名按 GBK 存，而 zip 规范只标了「不是 UTF-8」，
所以 `services/archive.py` 会按 `cp437 → gbk` 还原，并过滤 `__MACOSX`、`.DS_Store`、`._*`、`~$*` 等噪音文件，
同时限制解压后总体积防止 zip bomb。

### 匹配打分

按文件名（以及外层压缩包名）给每个学生打分，取最高分：

| 分数 | 条件 |
| --- | --- |
| 100 | 文件名里**同时**有学号和姓名（标准命名就是这种） |
| 94 | 文件名里有学号（独立片段） |
| 88 | 文件名里有姓名（独立片段） |
| 80 | 学号出现在文件名中间（前后不能是数字） |
| 72 | 姓名出现在文件名中间（前后不能是汉字） |

判定规则：

- ≥ 88 直接采用；
- 分数并列（同学号跨班级，或同名同姓）时，**用文件名里出现的班级名做二次区分**；
- 还是分不出来就判为歧义，列进上传响应的「未采用文件」里交给老师处理；
- 姓名字符被别的汉字包住（如 `王五的实验报告.docx`）**不直接采用**——
  因为同样的规则会把「李四」错配给「李四光」，只会给一条提示建议改名成 `姓名_学号.pdf`。
- 学号前后带数字检查，所以 `2021001` 不会误吃 `20210012` 的文件（有单测覆盖）。

## i学习 作业抓取

### 先确认能连上浏览器

抓取全部靠本机那台「i学习浏览器」（带调试端口的 Edge），所以在**概览页**放了一张
**i学习 浏览器**状态卡，就两个标签：已连接 / 未连接、已登录 / 未登录，
旁边有「重新检测」。

命令行想确认的话：

```bash
curl http://127.0.0.1:8010/api/istudy/status
# {"available":true,"logged_in":true,"browser":"Edg/154.…","message":"浏览器就绪，已登录 i学习"}
```

「评审与成绩」页选好课程后，会多出一张 **i学习 作业** 卡片：

```
从 i学习 同步作业 → 列表按作业名显示（第1周作业 / 第2周作业 …）
   → 点「抓取附件」：选择范围（班级 + 导出内容）→ 抓取附件
     （后台导出 → 下载 → 解包 → 抽出作答图片 → 匹配学生）
   → 变成一条条待评审记录，再去「评分结果」卡片点「一键 AI 评审」
```

弹窗里**已经抓过的班级默认不勾选**，会跳过不重复抓（每次导出都要等 i学习 打包几分钟，
按班级状态跳过能省不少时间）；想重新抓就手动勾上，或者点「全选（含重抓）」。

### 按人抓取（只补新交的）

每个班右边有「详情」：点开会**拿 i学习 的已交名单和本地已抓的对比**，列出
「还没抓 / 已抓过 / 未交 / 名单里没有」，默认只勾「还没抓」的那些。勾完确定，
这次就只导出这些人，不用整个班重抓。

- 走的是 i学习 自己的按人导出（`packWork` 的 `personIds`，值是批阅页上的 `createid`）。
- 实测一个班整抓要 3 分钟左右，按人抓 12 个人十几秒就回来了。
- **按人导出会自动改用「仅提交附件」**：实测 `personIds` 配 PDF 会一直卡在「导出中」
  不产出文件，Word 和「仅提交附件」都正常（见下面「两种导出格式」）。
- 没交作业的人勾不了——i学习 那边没有他们的提交，导不出来。

### 两种导出格式，我们都会解析

i学习 的「答题记录」导出成 `.doc` 时其实是 **Word 2003 XML**，学生的作答照片是 base64
塞在 `<w:binData>` 里的；导成 PDF 则是图片 + 文字的混排。`services/answer_files.py`
两种都支持：

- PDF → PyMuPDF 按页抠图 + 抽文字
- Word 2003 XML（伪 .doc） → 解 base64、按文件头认出图片类型、抽 `<w:t>` 文字
- 学生直接交的 jpg / png → 本身就是作答图片

### 重新抓取会覆盖什么

- **数据库不会翻倍**：提交记录按 `(work_id, student_id)`、评分结果按 `(rubric_id, student_id)`
  唯一，重抓是原地更新；图片也是一批一批替换。
- **磁盘上同名的文件会被覆盖**；而且写之前会先清掉这个学生上一版的整个目录，
  所以换了文件名、图片张数变少都不会留下孤儿文件。
- **学生姓名不会被重抓改掉**。姓名来自名单（`students` 表），只有重新导入名单才会变。
- **按人抓取不会动别人**。「谁没交」只有整班导出才判断得了——包里本来就只有勾选的那几个人。
  早期版本拿按人导出的结果去标记全班，结果把之前已经抓过（甚至已经评过）的学生覆盖成
  「未交」，出现「有图片却显示未交」的矛盾状态。现在按人抓取只更新包里出现的学生。
- **评审结果按「内容指纹」判断**：内容没变就保留原来的分数和评语；真的换了才退回
  「待评审」并清空分数，免得出现「分是照着旧文件给的」。

  这里有个坑：**i学习 每次导出的文件字节都不一样**（实测同一份作业两次导出，
  外层 `.doc` 差 8 个字节，但里面的图片和文字一模一样）。所以不能拿文件 sha256
  判断「学生有没有重新交」，否则每次重抓都会把所有已评的分误清掉。
  现在用的是 `content_checksum`：把抽出来的图片（按顺序）和文字拼起来算指纹，
  图片没变就是同一个内容。
- **整包 zip 不会删**：每次导出都会在 `exports/` 下新增一个包作为留档，磁盘会慢慢涨
  （见下面「清理」一节的建议）。

页面上几块各管各的，互不影响（顶部是汇总，下面是明细）：

- **统计卡片**（评分结果 / 已评审 / 待评审 / 涉及班级 / 平均分）跟**顶部的作业**走
- **i学习 作业**：有自己的班级 / 状态筛选，看「有哪些作业、抓了多少」
- **评分结果**：有自己的**作业**和**班级**筛选，看某次作业的明细。跟顶部完全独立，
  只在进页面 / 换课程时给一个默认值，之后改哪边都不影响另一边

**评分细则不放在页面上**，而是放在「一键 AI 评审」弹窗里选：
细则只是一份丢给 AI 看的标准，不跟某次作业绑定，所以放页面上跟着成绩卡片一起变会很奇怪。
弹窗里可以换细则，换了以后这次作业已有的评分结果会跟着改挂到新细则下；
一份细则不允许同时挂在两次作业下（要复用就去「评分细则」页复制一份）。

对应的抓取链路（`services/istudy.py` + `services/homework_sync.py`）：

```
进课       /courselist/opencoursenewfy                 拿 enc / openc / t 等隐藏字段
班级列表   /mooc2-ans/tcm/clazz-manage
作业列表   /mooc2-ans/work/list?selectClassid=&status=  按班级 + 状态筛选
批阅页     /mooc2-ans/work/mark?id=<workId>             把 workId 换成 taskId（导出要用）
提交导出   /mooc2-ans/work/packWork                     只是入队，异步打包
下载中心   /mooc2-ans/tcm/downloadcenter                轮询打包进度，完成后给直链
下载       https://d.istudy.szpu.edu.cn/workzip/...     换域名，不带 Cookie
```

几个踩过的坑，改代码时注意：

- **列表页的 id 有两种含义**：不筛班级时 `<li id="workN">` 的 N 是 `taskId`，
  筛班级时才是 `workId`。导出接口要的是 `taskId`，所以每个 workId 都得去批阅页换一次。
- **别按 taskId 建评分细则**。一次作业发给 6 个班就会拆成多个批次（taskId），
  但它们 `viewWork(...)` 的第一个参数（作业题库 id）是一样的 —— 这才是「同一次作业」，
  所以细则按 `istudy_library_id` 认（`ensure_rubric` 会自动把重复的并成一份）。
  第一版按 taskId 建，结果「第2周作业」被拆成了 4 份细则，页面上看着像重复项。
- **批阅页的 `id` 参数必须传 workId**，传列表页的 taskId 会返回一个读不到任何参数的壳页面。
- **导出是异步的**，实测一个班 33 人要 3 分钟左右，所以 `istudy_exports` 要落库、前端轮询。
- **同一个作业重新导出会复用下载中心里的老记录**（把它重新置成「导出中」），
  所以不能只认「新出现的 id」，得按「作业名 + 班级名 + 格式后缀」挑。
- **下载直链在 `d.istudy.szpu.edu.cn`**，跟 `mooc.istudy.szpu.edu.cn` 不是一个域名，别把 Cookie 带过去。
- **导出的 `.doc` 不是真 Word**，是 Word 2003 XML，正文是 base64 内嵌图片。
  所以默认导 **PDF**（`fmt=1`）——PDF 里既有题面图也有学生手写作答图，加上嵌入字体承载的文字，
  PyMuPDF 能一次性把图片和文字都取出来。

抓下来的东西放这里：

```
istudy/works/{班级}-{作业名}/exports/{export_id}-{作业名}.zip  整包留档，重跑不用再等 3 分钟
istudy/works/{班级}-{作业名}/students/{学号}/{原文件名}         学生提交的原始文件
istudy/works/{班级}-{作业名}/students/{学号}/answer/{seq}-{名字}  抽出来的作答图片
```

目录名用「班级-作业名」而不是数字 id，例如 `25人工智能本1-第1周作业`，直接翻磁盘也看得懂。
早期版本用的是 `istudy/works/{work_id}`，跑一次
`PYTHONPATH=. ./.venv/Scripts/python.exe scripts/rename_istudy_folders.py --apply`
就能改名并同步更新数据库里的 `object_key`（默认是预览，加 `--apply` 才真改）。

图片的 `role` 会自动标：同一个作业里多个人都有的那张图基本是老师发的题面（`question`），
其余算学生作答（`answer`）。

### 只读，不会把成绩写回 i学习

整个 `services/istudy.py` 里对 i学习 的请求**全是 GET**，没有任何 POST / PUT / 上传成绩的调用：

```
portal / coursegroupdata / opencoursenewfy / clazz-manage / clazz-student /
personexcel / work/list / work/mark / work/packWork / tcm/downloadcenter / workzip
```

唯一算「产生副作用」的是 `work/packWork` —— 它只是在老师自己的下载中心里生成一个打包任务，
不涉及学生成绩。AI 评审（含 mock）的结果只写本地 SQLite 的 `grading_results`，
接真实大模型时也是发给模型服务商，跟 i学习 没有任何关系。

## i学习 实验报告抓取

实验报告在 i学习 上是**另一套页面**（`mooc.istudy.szpu.edu.cn/lab/#/redirect?key=labTemplate/report`），
接口都在 `/lab/api/labTemplate` 下，跟作业完全不是一回事：

```
/report/index                          报告列表，一次给全（每个报告自带 stuList）
/report/correction/index               某次报告的提交名单
/report/correction/exportStuFillPdf    导出学生作答，同步返回 zip
```

几个和作业不一样的地方：

- **列表一次就能拿全**：`/report/index` 里每个报告自带 `stuList` 和 `stuStatusList`，
  人数、谁交了直接就有，不用每个报告再问一次。但 `search[0..2]`（moocCourseId / reportType / status）
  三个条件一个都不能少，少了服务端直接 500。
- **导出是同步的**，不用去下载中心轮询，实测一个班几十人十几秒到一分钟。
  包里是「每个学生一个压缩包（`姓名_学号.zip`），里面是 `姓名_学号.pdf`」。
- **只传要抓的 fillId**，包里就只有这些人 —— 所以「只补新交的那几个」是天然支持的。
- **一个报告记录可能同时发给好几个班**，本地按班级拆成多行，磁盘上也是一个班一个文件夹。
- `type` 是作答形式：`1` 表单型 / `2` 附件型 / `3` Word 模板。只有附件型会传 PDF，
  表单型的报告在界面上会标成「表单型（没有附件）」，抓不到东西是正常的。
- **PDF 是按页渲染成图片的**，不是抠内嵌图。学生是在电脑上排版实验报告，正文是矢量文字、
  只有图表是内嵌图片；只抠内嵌图会丢掉绝大部分内容。整页渲染（长边约 1700px）后模型才能看到完整报告。

前端「评审与成绩」页把「类型」切到**实验报告**，就是这整套流程：
选班级 / 状态 → 抓取附件（默认只抓没抓过的）→ 一键 AI 评审 → 复核（能直接看渲染出来的报告页）。
和作业一样，**只读不写回**：导出接口是 GET，不会把分数或评语传回 i学习。

## 名单导入

支持 `.csv` / `.xlsx`，表头需包含「学号/工号」和「姓名」，可选「院系」「专业」「班级」「加入时间」「入学年份」。
**导入前必须先选课程**（`course_id` 必填）：班级按「院系 + 专业 + 班级名称」自动创建或复用，
并自动挂到该课程下；学号相同的记录做更新。列名支持常见别名
（如 工号、编号、学院、系、年级、入班时间…），日期支持 Excel 日期单元格和 `2023-09-01` / `2023/09/01` / `2023年9月1日` 等写法。

模板见 `GET /api/classes/import/template`，页面上也有「下载模板」按钮。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET/POST/PATCH/DELETE` | `/api/courses` | 课程 CRUD |
| `GET/POST/DELETE` | `/api/courses/{id}/classes` | 课程下的班级：列表 / 挂载 / 摘除 |
| `GET/POST/DELETE` | `/api/courses/{id}/rubrics` | 课程下的评分细则：列表 / 挂载 / 摘除 |
| `POST` | `/api/classes/import` | 一键导入班级与学生（必须带 `course_id`） |
| `GET` | `/api/classes/import/template` | 下载名单模板 CSV |
| `GET/POST/PATCH/DELETE` | `/api/classes`、`/api/students` | 班级与学生 CRUD（`?course_id=` 按课程过滤） |
| `GET/POST/PATCH/DELETE` | `/api/rubrics` | 评分细则 CRUD |
| `GET` | `/api/rubrics/{id}/upload-hint` | 该细则要求的文件命名格式 |
| `POST` | `/api/rubrics/{id}/submissions` | **上传压缩包 / 单文件**，自动展开并匹配学生 |
| `POST` | `/api/rubrics/{id}/grade` | **触发 AI 评审**（`?sync=true` 同步执行，便于调试） |
| `GET` | `/api/rubrics/{id}/results` | 评分结果（可按课程、班级、状态、关键字筛选） |
| `GET` | `/api/rubrics/{id}/class-summary` | 按班级汇总（可按课程过滤） |
| `PATCH` | `/api/grading-results/{id}` | 教师人工修正分数或评语（打上 `is_manual`） |
| `GET` | `/api/grading-results/{id}/images` | 这条结果对应的作业图片（题面 + 该学生作答），复核时对照用 |
| `GET` | `/api/rubrics/{id}/export.xlsx` | **一键导出成绩与评语**（可带 `?course_id=` / `?class_id=`） |
| `GET` | `/api/files/download?key=...` | 经服务端代理下载（key 是相对路径） |
| `GET` | `/api/istudy/status` | i学习 连接与登录状态 |
| `GET` | `/api/istudy/courses` | i学习 上「我教的课」 |
| `POST` | `/api/istudy/roster/import` | 从 i学习 一键导入学生名单 |
| `POST` | `/api/istudy/works/sync` | **同步作业列表**（我教的课 → 课程 → 作业） |
| `GET` | `/api/istudy/works` | 作业列表，按作业名归并（可按 `class_id` / `status` 筛选） |
| `POST` | `/api/istudy/works/{id}/export` | **抓这次作业的附件**（异步任务，返回任务 id） |
| `GET` | `/api/istudy/exports/{id}` | 导出任务进度 |
| `GET` | `/api/istudy/works/{id}/submissions` | 这次作业下每个学生的提交与作答图片 |
| `POST` | `/api/istudy/lab-reports/sync` | **同步实验报告列表**（我教的课 → 课程 → 实验报告） |
| `GET` | `/api/istudy/lab-reports` | 实验报告列表，按实验名归并（可按 `class_id` / `status` 筛选） |
| `POST` | `/api/istudy/lab-reports/{id}/export` | **抓这次实验报告的作答**（`student_ids` 指定人，`force` 重抓） |
| `GET` | `/api/istudy/lab-reports/{id}/students` | 对比已交名单和本地已抓，找出还没抓的人 |
| `GET` | `/api/istudy/lab-reports/{id}/submissions` | 这次实验报告下每个学生的提交与作答图片 |
| `POST` | `/api/istudy/lab-reports/{id}/grade` | 对这次实验报告触发 AI 评审（可换评分细则） |

所有返回列表的接口都支持分页：`?page=1&page_size=20`。

## 接入真实 AI 评审

### 大模型（多模态）

抓下来的作业是学生拍照的手写解答，所以走**多模态**：把「题面图 + 这位学生的作答图 +
评分细则正文」一起发给模型，让它返回 JSON。

在 `backend/.env` 里填三样东西就能用：

```ini
AI_REVIEWER=llm
LLM_API_KEY=你的key
LLM_BASE_URL=https://api.deepseek.com/v1   # 或自建网关地址
LLM_MODEL=deepseek-flash
```

其余可调项：`LLM_TEMPERATURE`（默认 0，同一份作答每次给一样的分）、`LLM_RETRIES`（默认 2）、
`LLM_IMAGE_MAX_EDGE` / `LLM_IMAGE_QUALITY`（送模型前压图，长边默认 1600）。

#### 踩过的坑（换模型时照着查）

- **模型名要以 `/models` 接口返回的为准。** 这个网关实际只认 `deepseek-flash` 和
  `deepseek-v4-pro`；传 `deepseek-v4.1-flash` 会直接 400。先
  `GET {LLM_BASE_URL}/models` 看一眼最省事。
- **带「思考」的模型会烧 token 还不出结果。** `deepseek-flash` 默认会先推理，实测一次
  批改能思考 5 万字、把 8000/16000 token 全吃光，返回 `finish_reason=length` 且
  `content` 是空的。解决办法是 `LLM_DISABLE_THINKING=true`（代码里发
  `thinking: {"type": "disabled"}`）——实测思考 token 从几千变 0，
  单条从 87 秒降到 2 秒，输出 token 从 8000+ 降到 278。
  另外 `response_format: {"type": "json_object"}` 也是必须的。
- 代码里对这两点都有兜底：`content` 为空会明确报「token 都用在思考上了」；
  网关如果不认 `thinking` 字段，会自动摘掉重试一次。

几个实现上的取舍：

- **题面按「作业」取一份，不依赖某个学生有没有交题面**。实测有的学生只交了一张写满演算的
  整页照片，记录里没有题面图；而题面图在全班是同一份（去重后第1/2周各 1 张、第3周 2 张）。
- **送模型前先压图**。手机拍的原图一张 1.7MB，压到长边 1600 后不到 400KB，字迹依然清楚，
  费用差好几倍。同时按 EXIF 把方向摆正，否则竖拍的照片模型会看到一张躺着的图。
- **要求返回 breakdown**，分项得分会被拼进评语前面（`【分项】作答完整性：35；…`），
  老师复核时能看出分是怎么来的。
- **token 用量落库**（`grading_results.prompt_tokens` / `completion_tokens` / `images_sent`），
  复核抽屉里能看到「3 张图 · 输入 4321 / 输出 210 token」。

### 自定义评审器（纯文本）

契约在 `backend/app/services/reviewer.py`：

```python
class BaseReviewer(ABC):
    def review(self, context: ReviewContext) -> ReviewOutcome: ...
```

`grading.py` 已经把上下文准备好了：评分细则名称 / 类型 / 任务说明 / 评分标准、学生信息、
文件名，以及**文档正文**（`document.extract_text` 抽好放在 `context.text`）。接真模型只需实现 `LLMReviewer.review()`：

1. 用 `build_prompt(context)` 或 `prompt_payload(context)` 组装 prompt；
2. 让模型返回 JSON `{"score": 88, "comment": "……"}`；
3. 把分数 clamp 到 `[0, total_score]`，组装成 `ReviewOutcome` 返回；
4. `.env` 里把 `AI_REVIEWER` 改成 `llm`。

`document.py` 目前实现了文本 / 代码 / Excel / CSV 的取文本，PDF 和 Word 的分支留了注释掉的实现，
装上 `pdfplumber` / `python-docx` 后放开即可。

## 已知限制

- AI 评审是 `mock` 占位实现，出分只是联调用的稳定伪随机值，评语开头带 `[占位评审]` 前缀。
- **SQLite 并发**：后台评审任务 + 接口查询 + 抓取入库同时写库时，SQLite 一次只允许一个写者。
  所以评审是「短事务」的：先标「评审中」提交，再去调模型（这几秒不占数据库），
  最后写结果提交。抓取入库也是每 10 个学生提交一次。即便如此，
  真要更高并发（比如多人同时用）建议把 `SQLITE_JOURNAL_MODE` 改成 `WAL`
  —— 代价是用外部工具打开 `.db` 时看不到最新写入（还在 `-wal` 里）。
- PDF / Word 正文抽取待接入（代码里已标 TODO）。
- 压缩包只支持 zip，7z / rar 需要额外装 `py7zr` / `rarfile`。
- 评审走 FastAPI 后台线程池，没有接 Celery 之类的任务队列；重启会丢掉进行中的评审任务
  （记录状态会停在「评审中」，再点一次一键评审即可）。
- 没有登录与权限体系。
