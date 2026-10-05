# source_url 重构方案：改为 download_url

## 一、背景与问题

### 当前问题

**view_token 无法复用的场景**：

```python
# 场景1：用户提供本地直链 + source_url
POST /api/transcribe
{
  "url": "http://localhost:8080/video.mp4",          # 实际下载地址
  "source_url": "https://youtube.com/watch?v=abc123"  # 平台链接
}
# 生成: view_token_A（基于 localhost URL）
# 缓存: platform=youtube, media_id=abc123

# 场景2：用户直接提供平台链接
POST /api/transcribe
{
  "url": "https://youtube.com/watch?v=abc123"  # 平台链接
}
# 生成: view_token_B（基于 youtube URL）← 与场景1不同！
# 缓存命中: platform=youtube, media_id=abc123 ← 与场景1相同！
```

**问题根源**：
- view_token 基于 `url` 生成（实际下载地址）
- 缓存 key 基于 `platform + media_id`（从 source_url 解析）
- **两个维度不一致**，导致缓存能命中，但 view_token 不同

### 语义混乱

**当前参数语义**（不直观）：
- `url`: 实际下载地址
- `source_url`: 原始平台URL（用于元数据解析）

**问题**：
- 参数名 `source_url` 暗示是"原始URL"，但实际 `url` 才是用户直接使用的
- 下载逻辑使用 `url`，元数据解析优先使用 `source_url`，容易混淆

---

## 二、重构目标

### 新的参数语义

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | **平台链接**（用于 view_token 生成、缓存查询、元数据解析） |
| `download_url` | string | 否 | **实际下载地址**（可选，如果提供则优先使用下载） |

### 使用场景

**场景1：本地文件 + 平台链接**
```json
{
  "url": "https://youtube.com/watch?v=abc123",      // 平台链接
  "download_url": "http://localhost:8080/video.mp4"  // 实际下载地址
}
```
- view_token 基于 `youtube URL`
- 下载时使用 `localhost URL`
- 缓存 key: `platform=youtube, media_id=abc123`

**场景2：纯平台链接**（原有逻辑，零改动）
```json
{
  "url": "https://youtube.com/watch?v=abc123"
}
```
- view_token 基于 `youtube URL`
- 下载时使用 `youtube URL`
- 缓存 key: `platform=youtube, media_id=abc123`

**场景3：通用直链**（原有逻辑，零改动）
```json
{
  "url": "http://example.com/video.mp4"
}
```
- view_token 基于 `example.com URL`
- 下载时使用 `example.com URL`
- 缓存 key: `platform=generic, media_id=MD5(url)`

### 解决的问题

✅ **view_token 复用**：场景1和场景2的 `url` 都是 `youtube.com/watch?v=abc123`，生成相同的 view_token

✅ **语义清晰**：`url` 是主要参数（平台链接），`download_url` 是可选的下载优化

✅ **向后兼容**：旧客户端不传 `download_url`，行为完全不变

---

## 三、影响范围分析

### 涉及文件

| 文件 | 修改点数量 | 主要改动 |
|------|-----------|---------|
| `cache/cache_manager.py` | 31处 | 数据库结构、API方法、查询逻辑 |
| `api/services/transcription.py` | 39处 | 核心处理流程、URL解析、下载逻辑 |
| `api/routes/tasks.py` | 6处 | 请求模型、参数传递 |

### 核心逻辑变化

```python
# ===== 旧逻辑 =====
check_url = source_url if source_url else url  # URL解析（优先 source_url）
actual_download_url = url                       # 实际下载（总是用 url）

# ===== 新逻辑 =====
check_url = url                                 # URL解析（直接用 url）
actual_download_url = download_url or url       # 实际下载（优先 download_url）
```

**语义反转**：
- 旧逻辑：`url` 是主数据（下载），`source_url` 是辅助（元数据）
- 新逻辑：`url` 是主数据（平台链接），`download_url` 是辅助（下载优化）

---

## 四、详细修改清单

### 文件1：`cache_manager.py`（31处）

#### 4.1 数据库表结构（5处）

**位置1：主表定义（Line 92）**
```python
# 修改前
CREATE TABLE IF NOT EXISTS task_status (
    ...
    url TEXT NOT NULL,
    source_url TEXT,
    ...
)

# 修改后
CREATE TABLE IF NOT EXISTS task_status (
    ...
    url TEXT NOT NULL,
    download_url TEXT,
    ...
)
```

**位置2：迁移逻辑（Line 145-152）**
```python
# 修改前
# 迁移3: 添加 source_url 字段
if 'source_url' not in columns:
    logger.info("添加 source_url 字段到 task_status 表...")
    cursor.execute("ALTER TABLE task_status ADD COLUMN source_url TEXT")
    logger.info("source_url 字段添加成功")

# 修改后
# 迁移3: 添加 download_url 字段（替换 source_url）
if 'download_url' not in columns:
    logger.info("添加 download_url 字段到 task_status 表...")
    cursor.execute("ALTER TABLE task_status ADD COLUMN download_url TEXT")
    logger.info("download_url 字段添加成功")
```

**位置3：表重建（Line 180）**
```python
# 修改前
CREATE TABLE task_status (
    ...
    source_url TEXT,
    ...
)

# 修改后
CREATE TABLE task_status (
    ...
    download_url TEXT,
    ...
)
```

**位置4：数据恢复注释（Line 207-212）**
```python
# 修改前
# 需要在 url 后插入 source_url(None)，最后添加 llm_config(None)
new_row_data = row_data[:3] + [None] + row_data[3:] + [None]

# 修改后
# 需要在 url 后插入 download_url(None)，最后添加 llm_config(None)
new_row_data = row_data[:3] + [None] + row_data[3:] + [None]
```

**位置5：INSERT 语句（Line 219）**
```python
# 修改前
INSERT INTO task_status
(task_id, view_token, url, source_url, platform, media_id, ...)
VALUES (?, ?, ?, ?, ?, ?, ...)

# 修改后
INSERT INTO task_status
(task_id, view_token, url, download_url, platform, media_id, ...)
VALUES (?, ?, ?, ?, ?, ?, ...)
```

#### 4.2 create_task 方法（4处）

**位置6：函数签名（Line 675）**
```python
# 修改前
def create_task(self, url: str, use_speaker_recognition: bool = False, source_url: str = None) -> Dict[str, str]:

# 修改后
def create_task(self, url: str, use_speaker_recognition: bool = False, download_url: str = None) -> Dict[str, str]:
```

**位置7：Docstring（Line 680-682）**
```python
# 修改前
Args:
    url: 视频URL（实际下载地址）
    use_speaker_recognition: 是否使用说话人识别
    source_url: 原始平台URL（用于显示，可选）

# 修改后
Args:
    url: 视频URL（平台链接，用于 view_token 和缓存）
    use_speaker_recognition: 是否使用说话人识别
    download_url: 实际下载地址（可选，如果提供则优先使用）
```

**位置8：参数规范化（Line 690-691）**
```python
# 修改前
if source_url is not None and not source_url.strip():
    source_url = None

# 修改后
if download_url is not None and not download_url.strip():
    download_url = None
```

**位置9：INSERT 语句（Line 706-708）**
```python
# 修改前
cursor.execute('''
    INSERT INTO task_status
    (task_id, view_token, url, source_url, use_speaker_recognition, status)
    VALUES (?, ?, ?, ?, ?, 'queued')
''', (task_id, view_token, url, source_url, use_speaker_recognition))

# 修改后
cursor.execute('''
    INSERT INTO task_status
    (task_id, view_token, url, download_url, use_speaker_recognition, status)
    VALUES (?, ?, ?, ?, ?, 'queued')
''', (task_id, view_token, url, download_url, use_speaker_recognition))
```

**位置10：日志输出（Line 710）**
```python
# 修改前
logger.info(f"任务创建成功: {task_id}, view_token: {view_token}, source_url: {source_url}")

# 修改后
logger.info(f"任务创建成功: {task_id}, view_token: {view_token}, download_url: {download_url}")
```

#### 4.3 update_task_status 方法（4处）

**位置11：函数签名（Line 721）**
```python
# 修改前
def update_task_status(self, task_id: str, status: str, platform: str = None,
                      media_id: str = None, title: str = None, author: str = None,
                      cache_id: int = None, source_url: str = None):

# 修改后
def update_task_status(self, task_id: str, status: str, platform: str = None,
                      media_id: str = None, title: str = None, author: str = None,
                      cache_id: int = None, download_url: str = None):
```

**位置12：Docstring（Line 733）**
```python
# 修改前
Args:
    ...
    source_url: 原始平台URL

# 修改后
Args:
    ...
    download_url: 实际下载地址
```

**位置13：参数规范化（Line 737-738）**
```python
# 修改前
if source_url is not None and isinstance(source_url, str) and not source_url.strip():
    source_url = None

# 修改后
if download_url is not None and isinstance(download_url, str) and not download_url.strip():
    download_url = None
```

**位置14：UPDATE 语句（Line 760-762）**
```python
# 修改前
if source_url is not None:
    update_fields.append("source_url = ?")
    params.append(source_url)

# 修改后
if download_url is not None:
    update_fields.append("download_url = ?")
    params.append(download_url)
```

#### 4.4 get_view_data_by_token 方法（2处）

**位置15：display_url 逻辑（Line 857-858）**
```python
# 修改前
# 优先使用 source_url（如果存在且非空），否则回退到 url
source_url_value = task_info.get('source_url')
display_url = source_url_value if (source_url_value and source_url_value.strip()) else task_info['url']

# 修改后
# 注意：语义反转！url 现在是平台链接，应该优先显示
# 如果没有 url，才回退到 download_url（但这种情况不应该发生，因为 url 是必填）
display_url = task_info['url']
```

#### 4.5 get_existing_task_by_url 方法（2处）

**位置16：SELECT 字段（Line 951）**
```python
# 修改前
SELECT task_id, view_token, url, source_url, use_speaker_recognition, status, ...

# 修改后
SELECT task_id, view_token, url, download_url, use_speaker_recognition, status, ...
```

**位置17：字典键（Line 972）**
```python
# 修改前
'source_url': row[3],

# 修改后
'download_url': row[3],
```

---

### 文件2：`transcription.py`（39处）

#### 4.6 TranscribeRequest 模型（2处）

**位置18：字段定义（Line 65-67）**
```python
# 修改前
source_url: Optional[str] = Field(
    None, description="原始视频URL（用于解析平台和元数据）"
)

# 修改后
download_url: Optional[str] = Field(
    None, description="实际下载地址（可选，如果提供则优先使用）"
)
```

#### 4.7 merge_metadata 函数（1处）

**位置19：Docstring（Line 123）**
```python
# 修改前
parsed_metadata: 从source_url解析的元数据（可能为None）

# 修改后
parsed_metadata: 从url解析的元数据（可能为None）
```

#### 4.8 process_transcription 函数签名（2处）

**位置20：函数签名（Line 266）**
```python
# 修改前
def process_transcription(
    task_id, url, use_speaker_recognition=False, wechat_webhook=None,
    source_url=None, metadata_override=None
):

# 修改后
def process_transcription(
    task_id, url, use_speaker_recognition=False, wechat_webhook=None,
    download_url=None, metadata_override=None
):
```

**位置21：Docstring（Line 273-277）**
```python
# 修改前
参数:
    task_id: 任务ID
    url: 实际下载地址
    use_speaker_recognition: 是否使用说话人识别
    wechat_webhook: 企业微信webhook
    source_url: 原始视频URL（用于解析平台和元数据）
    metadata_override: 元数据覆盖（dict）

# 修改后
参数:
    task_id: 任务ID
    url: 平台链接（用于元数据解析、view_token 生成、缓存查询）
    use_speaker_recognition: 是否使用说话人识别
    wechat_webhook: 企业微信webhook
    download_url: 实际下载地址（可选，如果提供则优先使用）
    metadata_override: 元数据覆盖（dict）
```

#### 4.9 参数规范化（Line 280-283）

**位置22：参数规范化**
```python
# 修改前
# 规范化 source_url：将空字符串转换为 None
if source_url is not None and isinstance(source_url, str) and not source_url.strip():
    source_url = None

# 修改后
# 规范化 download_url：将空字符串转换为 None
if download_url is not None and isinstance(download_url, str) and not download_url.strip():
    download_url = None
```

**位置23：日志输出（Line 284）**
```python
# 修改前
logger.info(f"开始处理转录任务: {task_id}, URL: {url}, source_url: {source_url}")

# 修改后
logger.info(f"开始处理转录任务: {task_id}, URL: {url}, download_url: {download_url}")
```

#### 4.10 display_url 逻辑（Line 286-288）

**位置24：display_url（关键改动）**
```python
# 修改前
# 优先使用 source_url 用于通知显示（保持原始平台链接的可读性）
display_url = source_url or url
logger.info(f"企业微信通知将使用URL: {display_url}")

# 修改后
# url 现在就是平台链接，直接使用
display_url = url
logger.info(f"企业微信通知将使用URL: {display_url}")
```

#### 4.11 URL 解析逻辑（Line 301-303）

**位置25：URL 解析（最关键改动）**
```python
# 修改前
# 优先从 source_url 解析，否则从 url 解析
check_url = source_url if source_url else url
logger.info(f"[URL解析] 开始解析 URL: {check_url[:100]}")

# 修改后
# url 本身就是平台链接，直接解析
check_url = url
logger.info(f"[URL解析] 开始解析 URL: {check_url[:100]}")
```

#### 4.12 元数据获取逻辑（Line 553）

**位置26：元数据解析 URL**
```python
# 修改前
parse_url = source_url if source_url else url

# 修改后
parse_url = url
```

#### 4.13 下载地址判断（Line 606-612）

**位置27：has_separate_download_url（关键改动）**
```python
# 修改前
# 判断是否同时提供了 url 和 source_url
# 如果同时提供且不同，说明 url 是直接下载地址，source_url 仅用于元数据解析
has_separate_download_url = (
    source_url is not None and
    source_url.strip() != "" and
    source_url != url
)

# 修改后
# 判断是否提供了 download_url
# 如果提供，说明需要从 download_url 下载，而 url 仅用于元数据解析
has_separate_download_url = (
    download_url is not None and
    download_url.strip() != ""
)
```

#### 4.14 下载器选择逻辑（Line 886-890）

**位置28：下载器判断**
```python
# 修改前
original_downloader = None
if not source_url:
    original_downloader = metadata_downloader or create_downloader(url)
else:
    logger.info("已提供 source_url，使用解析的元数据，跳过传统下载器的 get_video_info")
    is_from_generic = (platform == 'generic')

# 修改后
original_downloader = None
if not download_url:
    original_downloader = metadata_downloader or create_downloader(url)
else:
    logger.info("已提供 download_url，使用解析的元数据，跳过传统下载器的 get_video_info")
    is_from_generic = (platform == 'generic')
```

#### 4.15 字幕获取逻辑（Line 896-901, 909-912）

**位置29：字幕获取日志**
```python
# 修改前（Line 896-901）
if has_separate_download_url:
    logger.info(
        f"检测到提供了独立的下载地址，跳过字幕获取，直接使用 url 进行转录: "
        f"url={url}, source_url={source_url}"
    )
    subtitle = None

# 修改后
if has_separate_download_url:
    logger.info(
        f"检测到提供了独立的下载地址，跳过字幕获取，直接使用 download_url 进行转录: "
        f"url={url}, download_url={download_url}"
    )
    subtitle = None
```

**位置30：YouTube 字幕获取（Line 909-912）**
```python
# 修改前
if metadata_downloader and metadata_downloader.__class__.__name__ == "YoutubeDownloader" and source_url:
    logger.info(f"不需要说话人识别，尝试从 source_url 获取YouTube平台字幕: {source_url}")
    subtitle = metadata_downloader.get_subtitle(source_url)
elif not source_url and original_downloader:

# 修改后
if metadata_downloader and metadata_downloader.__class__.__name__ == "YoutubeDownloader":
    logger.info(f"不需要说话人识别，尝试获取YouTube平台字幕: {url}")
    subtitle = metadata_downloader.get_subtitle(url)
elif not download_url and original_downloader:
```

#### 4.16 实际下载逻辑（Line 1013）

**位置31：实际下载（最关键改动）**
```python
# 修改前
local_file = download_downloader.download_file(url, filename)

# 修改后
# 优先使用 download_url，如果没有则使用 url
actual_download_url = download_url or url
logger.info(f"[下载] 使用下载地址: {actual_download_url}")
local_file = download_downloader.download_file(actual_download_url, filename)
```

#### 4.17 update_task_status 调用（多处）

**位置32-40：所有 update_task_status 调用**
```python
# 修改前（示例：Line 216）
cache_manager.update_task_status(task_id, "processing", source_url=source_url)

# 修改后
cache_manager.update_task_status(task_id, "processing", download_url=download_url)
```

**需要修改的行号**：
- Line 216
- Line 484
- Line 732
- Line 856
- Line 985
- Line 1202
- Line 1214

#### 4.18 display_url 异常处理（Line 242-244, 1209）

**位置41-42：异常处理**
```python
# 修改前（Line 242-244）
# 优先使用 source_url 用于通知显示
display_url = source_url or url
WechatNotifier().notify_task_status(display_url, "转录失败", str(exc))

# 修改后
# url 就是平台链接，直接使用
display_url = url
WechatNotifier().notify_task_status(display_url, "转录失败", str(exc))
```

#### 4.19 process_queue 函数（Line 208, 224）

**位置43-44：队列处理**
```python
# 修改前（Line 208）
source_url = task.get("source_url")

# 修改后
download_url = task.get("download_url")

# 修改前（Line 224）
source_url,

# 修改后
download_url,
```

---

### 文件3：`tasks.py`（6处）

#### 4.20 API 请求处理

**位置45：参数规范化（Line 51）**
```python
# 修改前
normalized_source_url = _normalize_empty_string(request_body.source_url)

# 修改后
normalized_download_url = _normalize_empty_string(request_body.download_url)
```

**位置46：日志输出（Line 68）**
```python
# 修改前
logger.info(
    f"收到转录API请求 - URL: {url}, 说话人识别: {request_body.use_speaker_recognition}, "
    f"自定义企微webhook: {request_body.wechat_webhook is not None}, "
    f"source_url: {normalized_source_url}, metadata_override: {normalized_metadata_override}"
)

# 修改后
logger.info(
    f"收到转录API请求 - URL: {url}, 说话人识别: {request_body.use_speaker_recognition}, "
    f"自定义企微webhook: {request_body.wechat_webhook is not None}, "
    f"download_url: {normalized_download_url}, metadata_override: {normalized_metadata_override}"
)
```

**位置47：create_task 调用（Line 88）**
```python
# 修改前
task_info = cache_manager.create_task(
    url=url,
    use_speaker_recognition=request_body.use_speaker_recognition,
    source_url=normalized_source_url
)

# 修改后
task_info = cache_manager.create_task(
    url=url,
    use_speaker_recognition=request_body.use_speaker_recognition,
    download_url=normalized_download_url
)
```

**位置48：队列任务数据（Line 113）**
```python
# 修改前
"source_url": normalized_source_url,

# 修改后
"download_url": normalized_download_url,
```

**位置49：display_url 逻辑（Line 125-126）**
```python
# 修改前
# 优先使用 source_url 用于平台识别和通知显示
display_url = normalized_source_url or url

# 修改后
# url 本身就是平台链接，直接使用
display_url = url
```

**位置50：通知消息（后续引用 display_url 的地方无需改动）**

---

## 五、数据库迁移策略

### 迁移步骤

```sql
-- 步骤1：检查是否存在 download_url 列
PRAGMA table_info(task_status);

-- 步骤2：如果不存在，添加列
ALTER TABLE task_status ADD COLUMN download_url TEXT;

-- 步骤3：（可选）删除旧的 source_url 列数据
-- SQLite 不支持直接删除列，但可以忽略该列
-- 新代码不再读取 source_url，因此旧数据不影响
```

### 历史数据处理

**现状**：
- 只有 1 条历史任务使用了 `source_url`
- 不需要保持兼容性

**处理方式**：
- 忽略历史的 `source_url` 数据
- 新增 `download_url` 列（默认为 NULL）
- 新代码只读写 `download_url`

---

## 六、测试验证点

### 测试场景

| 场景 | 请求参数 | 预期行为 |
|------|---------|---------|
| **场景1** | `url=youtube, download_url=None` | view_token 基于 youtube，下载从 youtube |
| **场景2** | `url=youtube, download_url=localhost` | view_token 基于 youtube，下载从 localhost |
| **场景3** | 两次请求（场景1和场景2） | 应复用相同的 view_token ✅ |
| **场景4** | `url=example.com, download_url=None` | 降级到 GenericDownloader，正常下载 ✅ |

### 验证点

1. ✅ **view_token 生成**：基于 `url` 字段
2. ✅ **缓存查询**：基于 `platform + media_id`（从 `url` 解析）
3. ✅ **下载地址**：优先 `download_url`，否则 `url`
4. ✅ **元数据解析**：从 `url` 解析
5. ✅ **display_url**：直接使用 `url`
6. ✅ **通用直链兜底**：GenericDownloader 正常工作

---

## 七、API 文档更新

### API 参数说明（需更新 README.md）

**`POST /api/transcribe`**：

```json
{
  "url": "视频URL（必填，平台链接）",
  "use_speaker_recognition": "是否使用说话人识别（默认 false）",
  "wechat_webhook": "企业微信 webhook 地址（可选）",
  "download_url": "实际下载地址（可选，如果提供则优先使用）",
  "metadata_override": {
    "title": "视频标题（可选）",
    "description": "视频描述（可选）",
    "author": "视频作者（可选）"
  }
}
```

**参数说明**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | **平台链接**（用于 view_token 生成、缓存查询、元数据解析） |
| `use_speaker_recognition` | boolean | 否 | 是否使用说话人识别功能（默认 false） |
| `wechat_webhook` | string | 否 | 企业微信 webhook 地址，用于发送通知 |
| `download_url` | string | 否 | **实际下载地址**（可选，如果提供则优先使用，用于本地文件场景） |
| `metadata_override` | object | 否 | 元数据覆盖对象，用于补充或覆盖解析的元数据 |

### 使用场景示例（需更新 README.md）

**场景 1：本地文件 + 平台链接**

```json
{
  "url": "https://www.youtube.com/watch?v=abc123",
  "download_url": "http://localhost:8080/video.mp4",
  "use_speaker_recognition": true
}
```

**场景 2：纯平台链接**

```json
{
  "url": "https://www.youtube.com/watch?v=abc123",
  "use_speaker_recognition": true
}
```

**场景 3：通用直链**

```json
{
  "url": "http://example.com/video.mp4"
}
```

---

## 八、风险评估

### 技术风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **语义反转可能导致理解混乱** | 中 | 详细的代码注释 + 文档说明 |
| **遗漏修改点** | 高 | 全局搜索 `source_url`，确保全部替换 |
| **数据库迁移失败** | 低 | 迁移逻辑简单（ADD COLUMN），失败概率低 |
| **向后兼容性** | 低 | 旧客户端不传新参数，行为不变 |

### 业务风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **历史数据丢失** | 低 | 只有 1 条历史 `source_url` 数据，可忽略 |
| **客户端需要更新文档** | 低 | API 向后兼容，旧客户端无需改动 |

---

## 九、实施计划

### 实施步骤

1. ✅ **代码审查**：Review 所有修改点（50处）
2. 🔧 **数据库迁移**：添加 `download_url` 列
3. 🔧 **代码修改**：按照清单逐一修改
4. ✅ **本地测试**：验证 4 个测试场景
5. 📝 **文档更新**：更新 README.md 和 API 文档
6. 🚀 **部署上线**：灰度发布

### 回滚方案

如果出现问题，回滚步骤：

1. 恢复代码到旧版本（`git revert`）
2. 数据库的 `download_url` 列可以保留（不影响旧代码）
3. 清理新提交的任务（如果有）

---

## 十、附录

### 完整的文件对比

**修改前后对比统计**：

| 文件 | 修改行数 | 新增行数 | 删除行数 |
|------|---------|---------|---------|
| `cache_manager.py` | 31 | 10 | 10 |
| `transcription.py` | 39 | 15 | 15 |
| `tasks.py` | 6 | 3 | 3 |
| **总计** | **76** | **28** | **28** |

### 全局搜索验证

```bash
# 搜索所有 source_url 引用
grep -r "source_url" src/video_transcript_api --include="*.py"

# 应该返回 0 个结果（修改完成后）
```

---

## 十一、总结

### 改动摘要

1. **参数语义反转**：`url` 从"下载地址"变为"平台链接"
2. **新增可选参数**：`download_url` 用于指定实际下载地址
3. **解决核心问题**：view_token 现在基于稳定的平台链接，可正确复用
4. **向后兼容**：旧客户端不传新参数，行为完全不变
5. **代码清晰**：语义更加直观，易于理解和维护

### 关键点

- ✅ 修改点总数：**76 处**（3 个文件）
- ✅ 数据库迁移：简单（ADD COLUMN）
- ✅ 向后兼容：完全兼容
- ✅ 风险评估：低风险
- ✅ 测试覆盖：4 个场景

---

**文档版本**：v1.0
**创建时间**：2026-01-28
**作者**：Claude (Sonnet 4.5)
