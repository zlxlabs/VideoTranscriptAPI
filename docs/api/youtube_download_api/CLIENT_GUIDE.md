# YouTube Audio API 客户端集成最佳实践指南

## 1. 基础配置

### 1.1 环境变量

```bash
YOUTUBE_API_BASE_URL=http://your-server:8000
YOUTUBE_API_KEY=your-api-key
```

### 1.2 HTTP 客户端设置建议

- **超时设置**: 创建任务 10s，文件下载根据文件大小调整
- **重试策略**: 对 5xx 错误自动重试 3 次，指数退避
- **连接池**: 复用 HTTP 连接，避免频繁握手

---

## 2. Python 客户端示例

### 2.1 基础客户端类

```python
"""YouTube Audio API Client"""
import hashlib
import hmac
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import httpx


class TaskStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorCode(str, Enum):
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    VIDEO_PRIVATE = "VIDEO_PRIVATE"
    VIDEO_REGION_BLOCKED = "VIDEO_REGION_BLOCKED"
    VIDEO_AGE_RESTRICTED = "VIDEO_AGE_RESTRICTED"
    VIDEO_LIVE_STREAM = "VIDEO_LIVE_STREAM"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    POT_TOKEN_FAILED = "POT_TOKEN_FAILED"


# 不可重试的错误码
NON_RETRYABLE_ERRORS = {
    ErrorCode.VIDEO_UNAVAILABLE,
    ErrorCode.VIDEO_PRIVATE,
    ErrorCode.VIDEO_REGION_BLOCKED,
    ErrorCode.VIDEO_AGE_RESTRICTED,
    ErrorCode.VIDEO_LIVE_STREAM,
}


@dataclass
class FileInfo:
    url: str
    size: Optional[int] = None
    format: Optional[str] = None
    language: Optional[str] = None


@dataclass
class TaskResult:
    task_id: Optional[str]
    status: TaskStatus
    video_id: str
    cache_hit: bool = False
    audio: Optional[FileInfo] = None
    transcript: Optional[FileInfo] = None
    error_code: Optional[ErrorCode] = None
    error_message: Optional[str] = None


class YouTubeAudioClient:
    """YouTube Audio API 客户端"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )

    def create_task(
        self,
        video_url: str,
        include_audio: bool = True,
        include_transcript: bool = True,
        callback_url: Optional[str] = None,
        callback_secret: Optional[str] = None,
    ) -> TaskResult:
        """
        创建下载任务

        Args:
            video_url: YouTube 视频 URL
            include_audio: 是否下载音频
            include_transcript: 是否获取字幕
            callback_url: Webhook 回调 URL
            callback_secret: HMAC 签名密钥 (8-256字符)
        """
        payload = {
            "video_url": video_url,
            "include_audio": include_audio,
            "include_transcript": include_transcript,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        if callback_secret:
            payload["callback_secret"] = callback_secret

        resp = self.client.post("/api/v1/tasks", json=payload)
        resp.raise_for_status()
        return self._parse_response(resp.json())

    def get_task(self, task_id: str) -> TaskResult:
        """查询任务状态"""
        resp = self.client.get(f"/api/v1/tasks/{task_id}")
        resp.raise_for_status()
        return self._parse_response(resp.json())

    def cancel_task(self, task_id: str) -> bool:
        """取消任务（仅 pending 状态可取消）"""
        resp = self.client.delete(f"/api/v1/tasks/{task_id}")
        return resp.status_code == 200

    def download_file(self, file_url: str) -> bytes:
        """下载文件内容（不需要 API Key）"""
        # 文件下载是公开接口
        with httpx.Client(timeout=300) as client:
            resp = client.get(f"{self.base_url}{file_url}")
            resp.raise_for_status()
            return resp.content

    def _parse_response(self, data: dict) -> TaskResult:
        """解析响应数据"""
        audio = None
        transcript = None

        if files := data.get("files"):
            if audio_data := files.get("audio"):
                audio = FileInfo(
                    url=audio_data["url"],
                    size=audio_data.get("size"),
                    format=audio_data.get("format"),
                )
            if transcript_data := files.get("transcript"):
                transcript = FileInfo(
                    url=transcript_data["url"],
                    size=transcript_data.get("size"),
                    format=transcript_data.get("format"),
                    language=transcript_data.get("language"),
                )

        error_code = None
        error_message = None
        if error := data.get("error"):
            error_code = ErrorCode(error["code"])
            error_message = error.get("message")

        return TaskResult(
            task_id=data.get("task_id"),
            status=TaskStatus(data["status"]),
            video_id=data["video_id"],
            cache_hit=data.get("cache_hit", False),
            audio=audio,
            transcript=transcript,
            error_code=error_code,
            error_message=error_message,
        )

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
```

### 2.2 异步客户端版本

```python
"""异步客户端版本"""
import asyncio
import httpx


class AsyncYouTubeAudioClient:
    """异步版本客户端"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )

    async def create_task(self, video_url: str, **kwargs) -> TaskResult:
        payload = {"video_url": video_url, **kwargs}
        resp = await self.client.post("/api/v1/tasks", json=payload)
        resp.raise_for_status()
        return self._parse_response(resp.json())

    async def get_task(self, task_id: str) -> TaskResult:
        resp = await self.client.get(f"/api/v1/tasks/{task_id}")
        resp.raise_for_status()
        return self._parse_response(resp.json())

    async def wait_for_completion(
        self,
        task_id: str,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> TaskResult:
        """
        等待任务完成

        Args:
            task_id: 任务 ID
            poll_interval: 轮询间隔（秒）
            timeout: 最大等待时间（秒）
        """
        start_time = asyncio.get_event_loop().time()

        while True:
            result = await self.get_task(task_id)

            if result.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                return result

            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")

            await asyncio.sleep(poll_interval)

    async def close(self):
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
```

---

## 3. 核心使用模式

### 3.1 模式一：轮询等待（简单场景）

```python
import time

def download_audio_sync(video_url: str) -> bytes:
    """同步下载音频"""
    with YouTubeAudioClient(BASE_URL, API_KEY) as client:
        # 1. 创建任务
        result = client.create_task(video_url, include_transcript=False)

        # 2. 缓存命中，直接返回
        if result.cache_hit:
            print("缓存命中，直接获取文件")
            return client.download_file(result.audio.url)

        # 3. 轮询等待完成
        task_id = result.task_id
        while result.status in (TaskStatus.PENDING, TaskStatus.DOWNLOADING):
            time.sleep(5)  # 建议轮询间隔 5-10 秒
            result = client.get_task(task_id)
            print(f"任务状态: {result.status}")

        # 4. 处理结果
        if result.status == TaskStatus.COMPLETED:
            return client.download_file(result.audio.url)
        else:
            raise Exception(f"下载失败: {result.error_message}")
```

### 3.2 模式二：Webhook 回调（推荐生产环境）

```python
from flask import Flask, request
import hmac
import hashlib

app = Flask(__name__)
CALLBACK_SECRET = "your-hmac-secret-key"


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """验证 Webhook 签名"""
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@app.post("/webhook/youtube")
def handle_callback():
    # 1. 验证签名
    signature = request.headers.get("X-Signature", "")
    if not verify_signature(request.data, signature, CALLBACK_SECRET):
        return {"error": "Invalid signature"}, 401

    # 2. 解析回调数据
    data = request.json
    task_id = data["task_id"]
    status = data["status"]

    # 3. 处理完成/失败
    if status == "completed":
        audio_url = data["files"]["audio"]["url"]
        # 异步下载文件或记录 URL 供后续使用
        process_completed_task(task_id, audio_url)
    elif status == "failed":
        error_code = data["error"]["code"]
        error_msg = data["error"]["message"]
        handle_failed_task(task_id, error_code, error_msg)

    return {"status": "ok"}


# 创建任务时指定回调
def create_task_with_callback(video_url: str):
    with YouTubeAudioClient(BASE_URL, API_KEY) as client:
        result = client.create_task(
            video_url=video_url,
            callback_url="https://your-server.com/webhook/youtube",
            callback_secret=CALLBACK_SECRET,
        )

        if result.cache_hit:
            # 缓存命中不会触发回调，直接处理
            process_completed_task(None, result.audio.url)
        else:
            # 任务已入队，等待回调
            print(f"任务已创建: {result.task_id}")
```

### 3.3 模式三：批量下载

```python
import asyncio
from typing import List, Dict

async def batch_download(video_urls: List[str]) -> Dict[str, TaskResult]:
    """
    批量下载多个视频

    注意：服务端默认单并发，批量任务会排队处理
    """
    async with AsyncYouTubeAudioClient(BASE_URL, API_KEY) as client:
        # 1. 批量创建任务
        tasks = {}
        for url in video_urls:
            result = await client.create_task(url)
            if result.cache_hit:
                tasks[url] = result  # 缓存命中，直接完成
            else:
                tasks[url] = result.task_id

        # 2. 等待所有任务完成
        results = {}
        for url, task_id_or_result in tasks.items():
            if isinstance(task_id_or_result, TaskResult):
                results[url] = task_id_or_result
            else:
                results[url] = await client.wait_for_completion(
                    task_id_or_result,
                    timeout=1200  # 批量任务需要更长超时
                )

        return results


# 使用示例
async def main():
    urls = [
        "https://www.youtube.com/watch?v=video1",
        "https://www.youtube.com/watch?v=video2",
        "https://www.youtube.com/watch?v=video3",
    ]
    results = await batch_download(urls)

    for url, result in results.items():
        if result.status == TaskStatus.COMPLETED:
            print(f"OK {url} -> {result.audio.url}")
        else:
            print(f"FAIL {url} -> {result.error_message}")
```

---

## 4. 错误处理最佳实践

### 4.1 错误分类处理

```python
def handle_task_error(result: TaskResult) -> None:
    """根据错误类型采取不同处理策略"""

    if result.error_code in NON_RETRYABLE_ERRORS:
        # 不可恢复错误，记录并通知用户
        log_permanent_failure(result)
        notify_user_video_unavailable(result.video_id, result.error_code)
        return

    # 可重试错误
    if result.error_code == ErrorCode.RATE_LIMITED:
        # 限流：等待更长时间后重试
        schedule_retry(result.video_id, delay_minutes=30)

    elif result.error_code == ErrorCode.NETWORK_ERROR:
        # 网络错误：短暂等待后重试
        schedule_retry(result.video_id, delay_minutes=5)

    elif result.error_code == ErrorCode.POT_TOKEN_FAILED:
        # PO Token 失败：可能是服务问题，检查服务健康
        check_service_health()
        schedule_retry(result.video_id, delay_minutes=10)

    elif result.error_code == ErrorCode.DOWNLOAD_FAILED:
        # 通用下载失败：重试
        schedule_retry(result.video_id, delay_minutes=5)
```

### 4.2 HTTP 错误处理

```python
import httpx

def safe_api_call(func):
    """API 调用装饰器，统一处理 HTTP 错误"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise AuthenticationError("API Key 无效")
            elif e.response.status_code == 400:
                detail = e.response.json().get("detail", "请求参数错误")
                raise ValidationError(detail)
            elif e.response.status_code == 404:
                raise NotFoundError("任务或资源不存在")
            elif e.response.status_code >= 500:
                raise ServiceError("服务暂时不可用，请稍后重试")
            raise
        except httpx.TimeoutException:
            raise ServiceError("请求超时，请稍后重试")
        except httpx.RequestError as e:
            raise NetworkError(f"网络错误: {e}")
    return wrapper
```

---

## 5. 性能优化建议

### 5.1 利用缓存机制

```python
def smart_download(video_url: str) -> TaskResult:
    """
    智能下载：充分利用缓存

    服务端会自动缓存已下载的资源，60天内有效
    """
    with YouTubeAudioClient(BASE_URL, API_KEY) as client:
        result = client.create_task(video_url)

        if result.cache_hit:
            # 缓存命中的特征：
            # - task_id 为 None
            # - status 为 completed
            # - cache_hit 为 True
            print(f"缓存命中! 直接返回文件")
            return result

        # 新任务，需要等待
        return client.wait_for_completion(result.task_id)
```

### 5.2 轮询策略优化

```python
def adaptive_polling(client: YouTubeAudioClient, task_id: str) -> TaskResult:
    """
    自适应轮询策略

    - pending 状态：较长间隔（队列等待）
    - downloading 状态：较短间隔（实时进度）
    """
    result = client.get_task(task_id)

    while result.status in (TaskStatus.PENDING, TaskStatus.DOWNLOADING):
        if result.status == TaskStatus.PENDING:
            # 等待队列，间隔长一些
            interval = 10
            if result.position:
                # 根据队列位置估算等待时间
                estimated_wait = result.estimated_wait or (result.position * 60)
                print(f"队列位置: {result.position}, 预计等待: {estimated_wait}s")
        else:
            # 下载中，间隔短一些
            interval = 3
            if result.progress:
                print(f"下载进度: {result.progress}%")

        time.sleep(interval)
        result = client.get_task(task_id)

    return result
```

---

## 6. Webhook 回调详解

### 6.1 回调请求格式

```http
POST {callback_url}
Content-Type: application/json
X-Signature: sha256=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
X-Task-Id: 550e8400-e29b-41d4-a716-446655440000
X-Timestamp: 1702357425
```

### 6.2 回调 Payload 结构

**成功回调：**

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "video_id": "dQw4w9WgXcQ",
  "video_info": {
    "title": "Video Title",
    "author": "Channel Name",
    "channel_id": "UCxxxxxxxxxxxxxx",
    "duration": 213,
    "description": "Video description...",
    "upload_date": "20251201",
    "view_count": 1000000,
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"
  },
  "files": {
    "audio": {
      "url": "/api/v1/files/abc123.m4a",
      "size": 3456789,
      "format": "m4a",
      "bitrate": 128,
      "language": null
    },
    "transcript": {
      "url": "/api/v1/files/def456.srt",
      "size": 12345,
      "format": "srt",
      "bitrate": null,
      "language": "en"
    }
  },
  "error": null,
  "expires_at": "2025-02-10T10:00:00Z"
}
```

**失败回调：**

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "failed",
  "video_id": "dQw4w9WgXcQ",
  "error": {
    "code": "VIDEO_PRIVATE",
    "message": "This video is private",
    "retry_count": 0
  }
}
```

### 6.3 签名验证实现

```python
import hmac
import hashlib

def verify_webhook_signature(
    body: bytes,
    signature: str,
    secret: str
) -> bool:
    """
    验证 Webhook 签名

    Args:
        body: 原始请求体 (bytes)
        signature: X-Signature 头部值
        secret: 创建任务时提供的 callback_secret
    """
    expected = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


# FastAPI 示例
from fastapi import FastAPI, Request, HTTPException

@app.post("/webhook")
async def webhook_handler(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Signature", "")

    if not verify_webhook_signature(body, signature, CALLBACK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid signature")

    data = await request.json()
    # 处理回调...
```

---

## 7. 完整使用示例

```python
"""完整使用示例"""
import asyncio
from youtube_audio_client import AsyncYouTubeAudioClient, TaskStatus

BASE_URL = "http://localhost:8000"
API_KEY = "your-api-key"


async def main():
    async with AsyncYouTubeAudioClient(BASE_URL, API_KEY) as client:
        # 示例1: 下载音频和字幕
        result = await client.create_task(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            include_audio=True,
            include_transcript=True,
        )

        if not result.cache_hit:
            result = await client.wait_for_completion(result.task_id)

        if result.status == TaskStatus.COMPLETED:
            print(f"音频: {result.audio.url}")
            if result.transcript:
                print(f"字幕: {result.transcript.url}")

        # 示例2: 仅获取字幕（无字幕时自动回退到音频）
        result = await client.create_task(
            "https://www.youtube.com/watch?v=another_video",
            include_audio=False,
            include_transcript=True,
        )

        if not result.cache_hit:
            result = await client.wait_for_completion(result.task_id)

        # 检查 result.audio_fallback 判断是否回退到了音频下载


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 8. 响应字段说明

### 8.1 TaskResponse 完整字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `task_id` | string/null | 任务 ID，缓存命中时为 null |
| `status` | string | 任务状态：pending/downloading/completed/failed/cancelled |
| `video_id` | string | YouTube 视频 ID |
| `video_url` | string | 原始视频 URL |
| `video_info` | object/null | 视频元信息（见下表） |
| `files` | object/null | 文件信息（见下表） |
| `error` | object/null | 错误信息，成功时为 null |
| `cache_hit` | boolean | 是否命中缓存 |
| `request` | object | 请求模式（include_audio, include_transcript） |
| `result` | object/null | 结果详情（见下表） |
| `position` | int/null | 队列位置（pending 状态） |
| `estimated_wait` | int/null | 预计等待时间（秒） |
| `progress` | int/null | 下载进度 0-100（downloading 状态） |
| `created_at` | datetime | 任务创建时间 |
| `started_at` | datetime/null | 开始下载时间 |
| `completed_at` | datetime/null | 完成时间 |
| `expires_at` | datetime/null | 文件过期时间 |
| `message` | string/null | 附加消息（如 "Resources retrieved from cache"） |

### 8.2 video_info 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `title` | string | 视频标题 |
| `author` | string | 频道名称 |
| `channel_id` | string | 频道 ID |
| `duration` | int | 视频时长（秒） |
| `description` | string | 视频描述 |
| `upload_date` | string | 上传日期（YYYYMMDD 格式） |
| `view_count` | int | 观看次数 |
| `thumbnail` | string | 缩略图 URL |

### 8.3 files 字段

```json
{
  "audio": {
    "url": "/api/v1/files/{file_id}.m4a",
    "size": 3456789,
    "format": "m4a",
    "bitrate": 128,
    "language": null
  },
  "transcript": {
    "url": "/api/v1/files/{file_id}.srt",
    "size": 12345,
    "format": "srt",
    "bitrate": null,
    "language": "en"
  }
}
```

**注意**：`audio.language` 和 `transcript.bitrate` 字段始终存在但值为 null。

### 8.4 result 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `has_transcript` | boolean | 视频是否有可用字幕 |
| `audio_fallback` | boolean | 是否因无字幕而回退到下载音频 |
| `reused_audio` | boolean | 音频是否来自缓存 |
| `reused_transcript` | boolean | 字幕是否来自缓存 |

---

## 9. 注意事项

1. **API Key 安全**: 不要将 API Key 硬编码或提交到代码仓库
2. **文件过期**: 下载的文件 60 天后会自动清理，请及时下载保存
3. **并发限制**: 服务端默认单并发，批量任务会排队处理
4. **缓存命中**: 缓存命中时 `task_id` 为 `null`，不会触发 Webhook
5. **错误重试**: 可重试错误（网络、限流等）服务端会自动重试 3 次
6. **文件下载**: `/api/v1/files/{file_id}.{ext}` 是公开接口，无需 API Key
7. **文件命名**: 下载的文件名格式为 `{video_id}_{视频标题}.{ext}`，方便识别内容
