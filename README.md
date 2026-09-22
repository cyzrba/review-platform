# 学生作业 / 实验报告自动评审平台

面向高校教师的批量批改工具：

> 导入班级名单 → 建评分细则 → 上传压缩包 → AI 评审出分与评语 → 按班级导出成绩

**当前状态**：整条链路（导入 / 上传 / 解包 / 匹配 / 入库 / 导出）已经跑通并验证过；
AI 评审是**可插拔的占位实现**（`mock` 评审器按文件内容稳定出分），
真实大模型只需实现一个方法，接入点见下方「接入真实 AI 评审」。

## 功能一览

| 页面 | 做什么 |
| --- | --- |
| 概览 | 课程 / 班级 / 学生 / 评分细则 / 评分结果的总量、评审进度、文件存储状态 |
| 班级与学生 | 三栏级联：**先建课程 → 选中课程 → 挂班级 / 导入名单 → 看学生** |
| 评分细则 | 一个项目 / 一次作业一份细则，可挂到多个课程；含类型、满分、任务说明、评分标准 |
| 评审与成绩 | 选课程 → 选类型 → 选细则 → 上传压缩包 → 自动匹配学生 → 一键 AI 评审 → 复核 → 导出 |

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
| 后端 | FastAPI + SQLAlchemy 2.0 + SQLite（WAL 模式） |
| 文件存储 | MinIO（S3 协议），另带本地磁盘兜底实现，没 MinIO 也能跑 |
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
│   │   ├── models.py            # 7 张表的 ORM 定义
│   │   ├── pagination.py        # 统一分页参数与响应结构
│   │   ├── schemas.py           # 请求 / 响应模型
│   │   ├── serializers.py       # ORM -> 响应模型
│   │   ├── storage.py           # MinioStorage / LocalStorage
│   │   ├── routers/             # courses / classes / rubrics / grading / exports / files / system
│   │   └── services/
│   │       ├── archive.py       # 嵌套压缩包展开 + 中文文件名修复 + 学生匹配
│   │       ├── importer.py      # CSV / XLSX 名单导入
│   │       ├── reviewer.py      # AI 评审器接口 + mock 实现 + prompt 组装
│   │       ├── grading.py       # 评审调度、并发、结果入库
│   │       ├── document.py      # 文档取文本（PDF / Word 留了 TODO）
│   │       └── exporter.py      # Excel 导出
│   ├── scripts/                 # smoke_test / e2e_http / clear_storage
│   └── tests/                   # 35 个 pytest 用例
├── frontend/                    # Vue3 前端（4 个页面）
└── docker-compose.yml           # 可选：本地起 MinIO
```

## 快速开始

### 1. 文件存储

本机已经有 MinIO 就直接用（默认连 `localhost:9000`）。没有的话：

```bash
make minio        # 等价于 docker compose up -d，控制台 http://localhost:9001
```

想完全跳过 MinIO，把 `backend/.env` 的 `STORAGE_BACKEND` 改成 `local`，文件就落本地磁盘。

### 2. 后端

```bash
cd backend
cp .env.example .env                 # 按需改 MinIO 地址 / 账号
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

打开 <http://localhost:5173>（端口被占用时 Vite 会自动顺延）。前端通过 Vite 代理访问 `/api`，浏览器不直连 MinIO。

### 4. 验收

```bash
make test      # 31 个后端用例
make smoke     # 进程内跑完整流程（导入→细则→压缩包→评审→导出）

cd backend && PYTHONPATH=. ./.venv/bin/python scripts/e2e_http.py http://127.0.0.1:8010
```

## 数据表设计

一共 **7 张表**。

| 表 | 说明 | 关键字段 |
| --- | --- | --- |
| `courses` | 课程（最外层组织单位） | `name`(唯一)、`code`(课程代码)、`term`(学期) |
| `course_classes` | 课程-班级关联，多对多 | `course_id`、`class_id`；唯一约束 `(course_id, class_id)` |
| `course_rubrics` | 课程-评分标准关联，多对多 | `course_id`、`rubric_id`；唯一约束 `(course_id, rubric_id)` |
| `classes` | 班级，由「院系 + 专业 + 班级名称」唯一确定 | `department`、`major`、`name`；唯一约束 `(department, major, name)` |
| `students` | 学生，归属唯一班级 | `class_id`、`student_no`(学号/工号)、`name`、`joined_at`(加入时间)、`enrollment_year`(入学年份)；唯一约束 `(class_id, student_no)` |
| `rubrics` | 评分细则 = 一个项目 / 一次作业，不分版本不分维度 | `name`、`kind`(homework/lab_report)、`description`、`criteria`、`extra_prompt`、`total_score` |
| `grading_results` | 评分结果：一个细则 × 一个学生 = 一条 | `rubric_id`、`student_id`、`class_id`、`source_filename`、`object_key`、`status`、`score`(总分)、`comment`(评语)、`model`、`graded_at`、`is_manual`；唯一约束 `(rubric_id, student_id)` |

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
| `GET` | `/api/rubrics/{id}/export.xlsx` | **一键导出成绩与评语**（可带 `?course_id=` / `?class_id=`） |
| `GET` | `/api/files/download?key=...` | 经服务端代理下载，前端不直连 MinIO |

所有返回列表的接口都支持分页：`?page=1&page_size=20`。

## 接入真实 AI 评审

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
- PDF / Word 正文抽取待接入（代码里已标 TODO）。
- 压缩包只支持 zip，7z / rar 需要额外装 `py7zr` / `rarfile`。
- 评审走 FastAPI 后台线程池，没有接 Celery 之类的任务队列；重启会丢掉进行中的评审任务
  （记录状态会停在「评审中」，再点一次一键评审即可）。
- 没有登录与权限体系。
