# FunASR WebSocket 客户端交互指南

## 概述

本文档详细介绍如何与 FunASR 转录服务器进行 WebSocket 通信，包括单文件上传、分片上传、实时转录状态监控等功能。服务器已根据最佳实践进行优化，支持大文件的稳定传输。

## 连接配置

### WebSocket 连接参数

```python
import asyncio
import websockets
import json
import base64
import hashlib
from pathlib import Path

# 推荐的连接配置（适配服务器端设置）
websocket = await websockets.connect(
    "ws://localhost:8767",
    ping_interval=60,       # 60秒发送一次心跳（与服务器一致）
    ping_timeout=120,       # 心跳响应超时120秒
    close_timeout=60,       # 关闭连接超时60秒
    max_size=10 * 1024 * 1024,  # 单消息最大10MB
    read_limit=2**20,       # 1MB读缓冲
    write_limit=2**20       # 1MB写缓冲
)
```

## 消息协议

### 基本消息格式

所有消息都使用 JSON 格式，包含 `type` 和 `data` 字段：

```json
{
  "type": "消息类型",
  "data": {
    "具体数据字段": "值"
  }
}
```

## 文件上传流程

### 1. 单文件上传（< 5MB）

适用于小文件的快速上传。

#### 步骤1：发送上传请求

```json
{
  "type": "upload_request",
  "data": {
    "file_name": "audio.mp3",
    "file_size": 1048576,
    "file_hash": "d41d8cd98f00b204e9800998ecf8427e",
    "output_format": "json",  // 可选: "json" 或 "srt"
    "force_refresh": false    // 可选: 是否强制刷新缓存
  }
}
```

#### 步骤2：接收上传就绪响应

```json
{
  "type": "upload_ready",
  "data": {
    "task_id": "uuid-task-id",
    "message": "准备接收文件数据"
  }
}
```

#### 步骤3：发送文件数据

```json
{
  "type": "upload_data",
  "data": {
    "task_id": "uuid-task-id",
    "file_data": "base64编码的文件数据"
  }
}
```

#### 步骤4：接收上传完成响应

```json
{
  "type": "upload_complete",
  "data": {
    "task_id": "uuid-task-id",
    "message": "文件上传成功，开始处理"
  }
}
```

### 2. 分片上传（≥ 5MB）

适用于大文件的稳定传输，自动分片处理。

#### 步骤1：发送分片上传请求

```json
{
  "type": "upload_request",
  "data": {
    "file_name": "large_audio.m4a",
    "file_size": 52428800,    // 50MB
    "file_hash": "文件MD5哈希",
    "chunk_size": 1048576,    // 1MB分片大小
    "total_chunks": 50,       // 总分片数
    "upload_mode": "chunked", // 标识分片上传模式
    "output_format": "json",
    "force_refresh": false
  }
}
```

#### 步骤2：接收分片上传就绪响应

```json
{
  "type": "upload_ready",
  "data": {
    "task_id": "uuid-task-id",
    "message": "准备接收分片数据",
    "chunk_size": 1048576,
    "total_chunks": 50
  }
}
```

#### 步骤3：循环发送分片数据

```json
{
  "type": "upload_chunk",
  "data": {
    "task_id": "uuid-task-id",
    "chunk_index": 0,         // 分片索引（从0开始）
    "chunk_size": 1048576,    // 当前分片实际大小
    "chunk_hash": "分片MD5哈希",
    "chunk_data": "base64编码的分片数据",
    "is_last": false          // 是否为最后一个分片
  }
}
```

#### 步骤4：接收分片确认响应

```json
{
  "type": "chunk_received",
  "data": {
    "task_id": "uuid-task-id",
    "chunk_index": 0,
    "progress": 2.0,          // 上传进度百分比
    "status": "received"      // 状态: "received" 或 "duplicate"
  }
}
```

#### 步骤5：接收最终上传完成响应

当所有分片上传完成后：

```json
{
  "type": "upload_complete",
  "data": {
    "task_id": "uuid-task-id",
    "message": "分片文件上传成功，开始处理"
  }
}
```

## 转录状态监控

### 任务进度通知

```json
{
  "type": "task_progress",
  "data": {
    "task_id": "uuid-task-id",
    "progress": 45.0,         // 转录进度百分比
    "status": "processing",   // 状态: processing, completed, failed
    "message": "正在处理音频...",
    "timestamp": "2025-08-12T15:30:00.000Z"
  }
}
```

### 排队状态通知

```json
{
  "type": "task_queued",
  "data": {
    "task_id": "uuid-task-id",
    "queue_position": 3,      // 排队位置
    "estimated_wait_minutes": 5, // 预计等待时间（分钟）
    "message": "任务排队中，位置: 3"
  }
}
```

### 任务完成通知

```json
{
  "type": "task_complete",
  "data": {
    "task_id": "uuid-task-id",
    "result": {
      // 转录结果数据
      "text": "转录文本内容",
      "segments": [...],      // 详细分段信息
      "speakers": [...],      // 说话人信息
      "duration": 120.5,      // 音频时长（秒）
      "format": "json"
    },
    "timestamp": "2025-08-12T15:35:00.000Z"
  }
}
```

## 错误处理

### 错误响应格式

```json
{
  "type": "error",
  "data": {
    "error": "错误类型",
    "message": "详细错误信息"
  }
}
```

### 常见错误类型

| 错误类型 | 说明 | 处理建议 |
|---------|------|----------|
| `file_too_large` | 文件超过最大限制 | 检查文件大小限制 |
| `invalid_file_type` | 不支持的文件格式 | 使用支持的音频格式 |
| `hash_mismatch` | 文件哈希不匹配 | 重新计算并发送文件 |
| `chunk_hash_mismatch` | 分片哈希不匹配 | 重新发送该分片 |
| `session_not_found` | 上传会话不存在 | 重新开始上传流程 |
| `task_not_found` | 任务不存在 | 检查任务ID是否正确 |

## 完整客户端示例

### Python 客户端示例

```python
import asyncio
import websockets
import json
import base64
import hashlib
import os
from pathlib import Path

class FunASRClient:
    def __init__(self, server_url="ws://localhost:8767"):
        self.server_url = server_url
        self.websocket = None
    
    async def connect(self):
        """建立连接"""
        self.websocket = await websockets.connect(
            self.server_url,
            ping_interval=60,
            ping_timeout=120,
            max_size=10 * 1024 * 1024,
            read_limit=2**20,
            write_limit=2**20
        )
        
        # 接收欢迎消息
        welcome = await self.receive_message()
        if welcome.get("type") == "connected":
            print(f"✓ 连接成功: {welcome['data']['message']}")
            return True
        return False
    
    async def disconnect(self):
        """断开连接"""
        if self.websocket:
            await self.websocket.close()
    
    async def send_message(self, message):
        """发送消息"""
        await self.websocket.send(json.dumps(message, ensure_ascii=False))
    
    async def receive_message(self, timeout=30):
        """接收消息"""
        message_json = await asyncio.wait_for(
            self.websocket.recv(), timeout=timeout
        )
        return json.loads(message_json)
    
    def calculate_file_hash(self, file_path):
        """计算文件MD5哈希"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    
    async def transcribe_file(self, file_path, output_format="json", force_refresh=False):
        """转录文件"""
        file_path = Path(file_path)
        file_size = file_path.stat().st_size
        file_hash = self.calculate_file_hash(file_path)
        
        print(f"文件: {file_path.name}")
        print(f"大小: {file_size/1024/1024:.2f}MB")
        print(f"哈希: {file_hash[:8]}...")
        
        # 判断使用单文件还是分片上传
        if file_size > 5 * 1024 * 1024:  # >5MB使用分片
            return await self._upload_chunked(file_path, file_size, file_hash, output_format, force_refresh)
        else:
            return await self._upload_single(file_path, file_size, file_hash, output_format, force_refresh)
    
    async def _upload_single(self, file_path, file_size, file_hash, output_format, force_refresh):
        """单文件上传"""
        print("使用单文件上传模式")
        
        # 发送上传请求
        request = {
            "type": "upload_request",
            "data": {
                "file_name": file_path.name,
                "file_size": file_size,
                "file_hash": file_hash,
                "output_format": output_format,
                "force_refresh": force_refresh
            }
        }
        
        await self.send_message(request)
        response = await self.receive_message()
        
        if response["type"] == "task_complete":
            # 直接返回缓存结果
            print("✓ 使用缓存结果")
            return response["data"]["result"]
        
        if response["type"] != "upload_ready":
            raise Exception(f"上传请求失败: {response}")
        
        task_id = response["data"]["task_id"]
        print(f"✓ 获得任务ID: {task_id}")
        
        # 读取文件并发送
        with open(file_path, 'rb') as f:
            file_data = f.read()
        
        upload_data = {
            "type": "upload_data",
            "data": {
                "task_id": task_id,
                "file_data": base64.b64encode(file_data).decode()
            }
        }
        
        await self.send_message(upload_data)
        response = await self.receive_message()
        
        if response["type"] != "upload_complete":
            raise Exception(f"文件上传失败: {response}")
        
        print("✓ 文件上传完成，等待转录结果...")
        
        # 等待转录完成
        return await self._wait_for_result()
    
    async def _upload_chunked(self, file_path, file_size, file_hash, output_format, force_refresh):
        """分片上传"""
        chunk_size = 1024 * 1024  # 1MB分片
        total_chunks = (file_size + chunk_size - 1) // chunk_size
        
        print(f"使用分片上传模式（{total_chunks}个分片）")
        
        # 发送分片上传请求
        request = {
            "type": "upload_request",
            "data": {
                "file_name": file_path.name,
                "file_size": file_size,
                "file_hash": file_hash,
                "chunk_size": chunk_size,
                "total_chunks": total_chunks,
                "upload_mode": "chunked",
                "output_format": output_format,
                "force_refresh": force_refresh
            }
        }
        
        await self.send_message(request)
        response = await self.receive_message()
        
        if response["type"] == "task_complete":
            print("✓ 使用缓存结果")
            return response["data"]["result"]
        
        if response["type"] != "upload_ready":
            raise Exception(f"分片上传请求失败: {response}")
        
        task_id = response["data"]["task_id"]
        print(f"✓ 获得任务ID: {task_id}")
        
        # 分片上传
        with open(file_path, 'rb') as f:
            for chunk_index in range(total_chunks):
                chunk_data = f.read(chunk_size)
                chunk_hash = hashlib.md5(chunk_data).hexdigest()
                
                chunk_message = {
                    "type": "upload_chunk",
                    "data": {
                        "task_id": task_id,
                        "chunk_index": chunk_index,
                        "chunk_size": len(chunk_data),
                        "chunk_hash": chunk_hash,
                        "chunk_data": base64.b64encode(chunk_data).decode(),
                        "is_last": chunk_index == total_chunks - 1
                    }
                }
                
                await self.send_message(chunk_message)
                
                # 等待分片确认
                chunk_response = await self.receive_message(timeout=60)
                if chunk_response["type"] != "chunk_received":
                    raise Exception(f"分片 {chunk_index} 上传失败")
                
                progress = chunk_response["data"]["progress"]
                print(f"上传进度: {progress:.1f}% ({chunk_index + 1}/{total_chunks})")
        
        print("✓ 所有分片上传完成，等待处理...")
        
        # 等待上传完成通知
        response = await self.receive_message()
        if response["type"] not in ["upload_complete", "task_queued"]:
            raise Exception(f"分片上传完成失败: {response}")
        
        if response["type"] == "task_queued":
            position = response["data"]["queue_position"]
            wait_time = response["data"]["estimated_wait_minutes"]
            print(f"⏳ 任务排队中，位置: {position}，预计等待: {wait_time}分钟")
        
        # 等待转录完成
        return await self._wait_for_result()
    
    async def _wait_for_result(self):
        """等待转录结果"""
        while True:
            response = await self.receive_message(timeout=300)  # 5分钟超时
            
            if response["type"] == "task_progress":
                progress = response["data"]["progress"]
                status = response["data"]["status"]
                message = response["data"].get("message", "")
                print(f"转录进度: {progress}% - {status} - {message}")
            
            elif response["type"] == "task_complete":
                result = response["data"]["result"]
                print("✓ 转录完成")
                return result
            
            elif response["type"] == "error":
                error_msg = response["data"]["message"]
                raise Exception(f"转录失败: {error_msg}")

# 使用示例
async def main():
    client = FunASRClient()
    
    try:
        # 连接服务器
        if not await client.connect():
            print("❌ 连接失败")
            return
        
        # 转录文件
        result = await client.transcribe_file(
            "path/to/your/audio.m4a",
            output_format="json",
            force_refresh=False
        )
        
        # 处理结果
        print("=" * 50)
        print("转录结果:")
        print(f"文本: {result.get('text', '')}")
        print(f"时长: {result.get('duration', 0)}秒")
        print(f"分段数: {len(result.get('segments', []))}")
        print(f"说话人数: {len(result.get('speakers', []))}")
        
    except Exception as e:
        print(f"❌ 错误: {e}")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
```

## 支持的音频格式

- `.wav` - WAV音频文件
- `.mp3` - MP3音频文件
- `.mp4` - MP4视频文件
- `.m4a` - M4A音频文件
- `.flac` - FLAC无损音频
- `.aac` - AAC音频文件
- `.ogg` - OGG音频文件
- `.opus` - Opus音频文件

## 输出格式

### JSON格式输出

```json
{
  "text": "完整转录文本",
  "duration": 120.5,
  "segments": [
    {
      "text": "分段文本",
      "start": 0.0,
      "end": 5.2,
      "speaker": "speaker_0"
    }
  ],
  "speakers": [
    {
      "id": "speaker_0",
      "name": "说话人1",
      "segments": [0, 2, 4]
    }
  ],
  "format": "json"
}
```

### SRT格式输出

```
1
00:00:00,000 --> 00:00:05,200
<speaker_0>分段文本内容

2
00:00:05,200 --> 00:00:10,500
<speaker_1>另一个说话人的文本
```

## 性能优化建议

### 客户端优化

1. **文件预处理**
   - 压缩音频文件减少传输时间
   - 使用合适的采样率（16kHz推荐）

2. **网络优化**
   - 使用稳定的网络连接
   - 适当增加超时时间处理大文件

3. **并发控制**
   - 避免同时上传多个大文件
   - 合理设置重试间隔

### 错误恢复

1. **连接断开重试**
   ```python
   async def connect_with_retry(self, max_retry=3):
       for attempt in range(max_retry):
           try:
               return await self.connect()
           except Exception as e:
               if attempt < max_retry - 1:
                   await asyncio.sleep(2 ** attempt)
               else:
                   raise e
   ```

2. **分片重传**
   - 服务器会自动处理重复分片
   - 客户端可以安全地重传失败的分片

## 故障排除

### 常见问题

1. **连接超时**
   - 检查服务器是否正常运行
   - 确认网络连接稳定
   - 适当增加连接超时时间

2. **文件上传失败**
   - 检查文件格式是否支持
   - 验证文件大小是否超过限制
   - 确认文件哈希计算正确

3. **转录结果异常**
   - 检查音频质量
   - 确认音频语言设置
   - 验证文件编码格式

### 日志监控

启用客户端详细日志：

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## 总结

FunASR WebSocket 客户端支持灵活的文件上传和转录功能：

- **小文件（<5MB）**：使用单文件上传，快速高效
- **大文件（≥5MB）**：自动分片上传，稳定可靠
- **实时监控**：支持进度跟踪和状态通知
- **错误处理**：完善的错误恢复机制
- **格式支持**：多种音频格式和输出选项

通过遵循本指南，可以构建稳定、高效的音频转录客户端应用。