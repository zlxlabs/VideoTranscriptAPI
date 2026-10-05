# Download URL 和 Metadata Override 功能

## 功能概述

当使用本地已下载的视频文件时，可以通过 `download_url` 和 `metadata_override` 参数来保留原始平台的元数据信息，避免信息丢失。

## 使用场景

### 问题背景

某些平台（如 YouTube）的视频下载可能会因风控失败，此时用户可能会：
1. 在本地先下载视频文件
2. 通过本地 HTTP 服务器（如 `http://localhost:8080/video.mp4`）暴露文件
3. 将平台链接提交给转录 API，并提供本地下载地址

**但这样做会导致：**
- 平台信息丢失（被识别为 `generic` 平台）
- 缓存键不准确（无法与原视频关联）
- 视频标题、作者等元数据丢失

### 解决方案

通过 `download_url` 配合平台链接 `url`，可以保留原始元数据。

---

## API 参数说明

### 完整请求格式

```json
{
  "url": "https://www.youtube.com/watch?v=abc123",  // 必填，平台链接
  "use_speaker_recognition": true,
  "wechat_webhook": "...",

  // 新增可选参数
  "download_url": "http://localhost:8080/video.mp4",  // 可选，实际下载地址
  "metadata_override": {  // 可选，元数据覆盖
    "title": "视频标题",
    "description": "视频描述",
    "author": "作者名称"
  }
}
```

### 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | **平台链接**（用于 view_token 生成、缓存查询、元数据解析） |
| `download_url` | string | 否 | **实际下载地址**（可选，如果提供则优先使用） |
| `metadata_override` | object | 否 | 元数据覆盖对象 |
| `metadata_override.title` | string | 否 | 视频标题 |
| `metadata_override.description` | string | 否 | 视频描述 |
| `metadata_override.author` | string | 否 | 视频作者 |

---

## 工作原理

### 元数据合并逻辑

系统按以下优先级处理元数据：

```
1. 尝试从 url 解析元数据（platform, media_id, title, author, description）
2. 如果解析成功：
   - 使用解析的元数据作为基础
   - 用 metadata_override 中的字段进行补充/覆盖
3. 如果解析失败：
   - 使用 metadata_override 作为主要来源
4. 填充默认值（如果仍然缺失）：
   - title: 从 url 提取文件名，或 "Untitled"
   - description: ""（空字符串）
   - author: "Unknown"
   - platform: "generic"
   - media_id: url 的 MD5 哈希值（前16位）
```

### 下载逻辑

- **元数据解析**：使用 `url` 匹配对应的平台下载器（如 `YoutubeDownloader`），但**仅调用** `get_video_info()` 提取元数据
- **文件下载**：优先使用 `download_url`，否则使用 `url`
- **download_url 优先**：提供 `download_url` 时跳过平台字幕与 YouTube API Server 等平台下载方式

---

## 使用示例

### 场景 1：YouTube 视频（本地文件 + 自动解析）

**场景描述**：用户本地下载了 YouTube 视频，希望保留原始元数据

**请求**：
```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=abc123",
    "download_url": "http://localhost:8080/yt_video.mp4",
    "use_speaker_recognition": true
  }'
```

**处理流程**：
1. 使用 `YoutubeDownloader` 解析 `url`，获取：
   - `platform`: "youtube"
   - `media_id`: "abc123"
   - `title`: "视频标题"
   - `author`: "频道名称"
   - `description`: "视频描述"
2. 使用 `download_url` 下载文件
3. 缓存键：`youtube_abc123_true`

---

### 场景 2：YouTube 视频（手动补充元数据）

**场景描述**：自动解析的元数据不准确，用户手动提供更准确的信息

**请求**：
```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=abc123",
    "download_url": "http://localhost:8080/yt_video.mp4",
    "metadata_override": {
      "title": "更准确的中文标题",
      "description": "补充的详细描述"
    }
  }'
```

**处理流程**：
1. 解析 `url` 获取基础元数据
2. 用 `metadata_override` **补充/覆盖**：
   - `title` 使用自定义值
   - `description` 使用自定义值
   - `author` 保留 YouTube 解析的值
3. 缓存键：`youtube_abc123_false`

---

### 场景 3：纯本地文件（手动提供所有信息）

**场景描述**：本地文件无法关联到任何平台，手动提供元数据

**请求**：
```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://localhost:8080/unknown.mp4",
    "metadata_override": {
      "title": "手动输入的标题",
      "author": "手动输入的作者",
      "description": "手动输入的描述"
    }
  }'
```

**处理流程**：
1. 没有可解析的平台链接，跳过解析
2. 直接使用 `metadata_override`
3. 缓存键：`generic_{url_hash}_false`

---

### 场景 4：url 解析失败，降级到 metadata_override

**场景描述**：提供的 `url` 平台不支持，使用 `metadata_override` 兜底

**请求**：
```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://unsupported-platform.com/video/123",
    "download_url": "http://localhost:8080/video.mp4",
    "metadata_override": {
      "title": "兜底标题",
      "author": "兜底作者"
    }
  }'
```

**处理流程**：
1. 尝试解析 `url` → 失败（平台不支持）
2. 使用 `metadata_override` **覆盖**
3. 缓存键：`generic_{url_hash}_false`

---

## 缓存行为

### 缓存键生成规则

缓存键由三部分组成：`{platform}_{media_id}_{use_speaker_recognition}`

**示例**：
- `youtube_abc123_true`（YouTube 视频，启用说话人识别）
- `bilibili_BV1xx_false`（Bilibili 视频，不启用说话人识别）
- `generic_a1b2c3d4e5f6g7h8_false`（通用文件，media_id 为 URL 哈希）

### 缓存命中逻辑

如果两个请求的 `platform`、`media_id` 和 `use_speaker_recognition` 相同，则会命中同一个缓存。

**示例**：
```json
// 请求 A
{
  "url": "https://www.youtube.com/watch?v=abc123",
  "download_url": "http://localhost:8080/video1.mp4",
  "use_speaker_recognition": true
}

// 请求 B（不同的本地文件，但 url 相同）
{
  "url": "https://www.youtube.com/watch?v=abc123",
  "download_url": "http://localhost:8080/video2.mp4",
  "use_speaker_recognition": true
}
```

**结果**：请求 B 会命中请求 A 的缓存（因为它们指向同一个 YouTube 视频）

---

## 注意事项

1. **download_url 和 metadata_override 都是可选的**
   - 如果都不提供，系统会尝试使用传统方式处理 `url`

2. **元数据覆盖的优先级**
   - 解析成功时：`metadata_override` 作为**补充**
   - 解析失败时：`metadata_override` 作为**覆盖**

3. **字幕获取**
   - 当提供了 `download_url` 时会跳过平台字幕获取
   - 如果 `use_speaker_recognition=true`，则跳过字幕获取，强制转录

4. **文件下载**
   - 实际下载优先使用 `download_url`，否则使用 `url`
   - 支持断点续传、大文件下载等特性

5. **缓存键冲突**
   - 如果多个本地文件使用相同的 `url`，它们会共享缓存
   - 这是期望行为：相同的源视频应该使用相同的转录结果

---

## 技术实现细节

### 核心函数

#### `merge_metadata(parsed_metadata, metadata_override, url)`

合并元数据的核心逻辑。

**参数**：
- `parsed_metadata`: 从 `url` 解析的元数据（可能为 None）
- `metadata_override`: 用户提供的元数据覆盖（可能为 None）
- `url`: 平台链接（用于生成默认值）

**返回**：
- `dict`: 合并后的完整元数据

#### `extract_filename_from_url(url)`

从 URL 中提取文件名（不含扩展名）。

#### `generate_media_id_from_url(url)`

生成 URL 的唯一标识（MD5 哈希的前 16 位）。

### 日志输出

系统会在关键步骤输出结构化日志：

```
[INFO] 使用 url 解析元数据: https://www.youtube.com/watch?v=abc123, 下载器类型: YoutubeDownloader
[INFO] 成功从 url 解析元数据: platform=youtube, media_id=abc123, title=视频标题
[INFO] 最终元数据: platform=youtube, media_id=abc123, title=更准确的中文标题, author=频道名称
[INFO] 使用下载地址: http://localhost:8080/video.mp4
```

---

## 常见问题

### Q1: 为什么需要 download_url？

**A**: 当使用本地文件时，系统需要稳定的平台链接作为 `url`，否则会导致：
- 无法生成准确的缓存键
- 无法获取原始元数据（标题、作者等）
- 无法利用平台特性（如 YouTube 字幕）

### Q2: metadata_override 什么时候生效？

**A**:
- **解析成功时**：作为补充（只覆盖指定的字段）
- **解析失败时**：作为主要来源（所有字段）

### Q3: 如果 url 和 download_url 指向的内容不匹配怎么办？

**A**: 系统会使用 `url` 的元数据，但转录的是 `download_url` 指向的文件。这可能导致元数据与实际内容不符，需要用户确保两者一致。

### Q4: 可以只提供 metadata_override 而不提供 download_url 吗？

**A**: 可以。此时系统会：
- 使用 `metadata_override` 作为主要元数据来源
- 平台识别为 `generic`
- media_id 为 URL 的哈希值

---

## 测试

### 单元测试

运行元数据合并逻辑的单元测试：

```bash
pytest tests/test_metadata_override.py -v
```

### 集成测试

提交实际转录请求进行端到端测试：

```bash
# 测试场景：本地文件 + download_url
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=test123",
    "download_url": "http://localhost:8080/test_video.mp4",
    "metadata_override": {
      "title": "测试标题"
    }
  }'
```

---

## 更新日志

- **2026-01-28**: 参数语义更新
  - 新增 `download_url` 参数（可选）
  - 新增 `metadata_override` 参数
  - 实现元数据合并逻辑
  - 分离元数据解析和文件下载
