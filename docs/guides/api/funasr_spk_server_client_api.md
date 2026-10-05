
## 客户端用法

### 测试客户端

基础测试：
```bash
python tests/server/test_server_transcription.py
```

并发测试：
```bash
python tests/server/test_concurrent_transcription.py [客户端数量]
```

### WebSocket API

#### 1. 连接到服务器
```javascript
const ws = new WebSocket('ws://localhost:8767');
```

#### 2. 认证（如果启用）
```json
{
  "type": "auth",
  "data": {
    "token": "your-jwt-token"
  }
}
```

#### 3. 上传文件请求
```json
{
  "type": "upload_request",
  "data": {
    "file_name": "test.mp3",
    "file_size": 1024000,
    "file_hash": "md5-hash",
    "force_refresh": false,
    "output_format": "json"  // 或 "srt"
  }
}
```

#### 4. 上传文件数据
```json
{
  "type": "upload_data",
  "data": {
    "task_id": "task-id",
    "file_data": "base64-encoded-data"
  }
}
```

#### 5. 接收转录结果
服务器会发送以下类型的消息：
- `task_progress`: 转录进度更新
- `task_complete`: 转录完成
- `error`: 错误信息

## 输出格式对比

### JSON 格式（推荐用于数据处理）

**特点**：
- 自动合并相同说话人的连续句子
- 提供完整的元数据和统计信息
- 便于程序处理和分析

**适用场景**：
- 会议纪要整理
- 对话分析
- 数据挖掘

```json
{
  "task_id": "uuid",
  "file_name": "meeting.mp3",
  "file_hash": "md5-hash",
  "duration": 120.5,
  "segments": [
    {
      "start_time": 0.88,
      "end_time": 5.195,
      "text": "欢迎大家来体验达摩院推出的语音识别模型。",
      "speaker": "Speaker1"
    }
  ],
  "speakers": ["Speaker1"],
  "processing_time": 1.03
}
```

### SRT 格式（推荐用于字幕制作）

**特点**：
- 保持FunASR原始的句子分割
- 不合并说话人内容，保持原始粒度
- 标准SRT字幕格式，兼容性好

**适用场景**：
- 视频字幕制作
- 直播转录
- 播客字幕

```srt
1
00:00:00,880 --> 00:00:05,195
Speaker1:欢迎大家来体验达摩院推出的语音识别模型。
```

### 请求格式选择

在上传请求中指定 `output_format` 参数：

```json
// JSON格式（默认）
{
  "type": "upload_request",
  "data": {
    "file_name": "test.mp3",
    "output_format": "json"
  }
}

// SRT格式
{
  "type": "upload_request", 
  "data": {
    "file_name": "test.mp3",
    "output_format": "srt"
  }
}
```

### 响应格式

#### JSON格式响应
```json
{
  "type": "task_complete",
  "data": {
    "task_id": "uuid",
    "result": {
      "task_id": "uuid",
      "file_name": "test.mp3",
      "segments": [...],
      "speakers": [...],
      // ... 完整的转录结果
    }
  }
}
```

#### SRT格式响应
```json
{
  "type": "task_complete", 
  "data": {
    "task_id": "uuid",
    "result": {
      "format": "srt",
      "content": "1\n00:00:00,880 --> 00:00:05,195\nSpeaker1:欢迎大家...\n\n",
      "file_name": "test.mp3",
      "file_hash": "md5-hash"
    }
  }
}
```