# URL去重与View Token复用功能说明

## 问题描述

在之前的实现中，当多次请求相同的视频URL时，系统会为每次请求生成不同的`task_id`和`view_token`，导致：

1. **用户体验差**：同一个视频有多个不同的查看链接
2. **分享链接混乱**：用户无法使用一致的分享链接

## 解决方案

### 核心思路

- **每次请求都正常创建新的`task_id`**：保证完整的企微通知流程和任务追踪
- **相同URL复用`view_token`**：确保同一视频的查看链接保持一致
- **利用缓存避免重复处理**：已转录的内容直接使用缓存结果

### 实现细节

#### 1. View Token复用逻辑

在创建新任务时，检查是否已有相同URL和相同参数的成功任务：

```python
def create_task(self, url: str, use_speaker_recognition: bool = False):
    # 检查是否已有相同URL的成功任务，如果有则复用其view_token
    existing_task = self.get_existing_task_by_url(url, use_speaker_recognition)
    if existing_task and existing_task['status'] == 'success':
        view_token = existing_task['view_token']  # 复用现有view_token
    else:
        view_token = self.generate_view_token()   # 生成新view_token
```

#### 2. 处理策略

| 场景 | Task ID | View Token | 处理逻辑 |
|------|---------|------------|----------|
| 首次请求 | 新生成 | 新生成 | 正常下载转录处理 |
| 重复请求（已有缓存） | 新生成 | **复用已有** | 直接使用缓存+企微通知 |
| 不同参数 | 新生成 | 新生成 | 正常处理（不同配置视为不同任务） |

#### 3. 参数匹配规则

去重检查会考虑以下参数：
- `url`：视频URL（完全匹配）
- `use_speaker_recognition`：是否启用说话人识别

**重要**：`use_speaker_recognition`不同的请求会被视为不同的任务，因为它们会产生不同的转录结果。

### 代码实现

#### CacheManager新增方法

```python
def get_existing_task_by_url(self, url: str, use_speaker_recognition: bool = False) -> Optional[Dict[str, Any]]:
    """
    根据URL和说话人识别参数查找现有任务
    
    Args:
        url: 视频URL
        use_speaker_recognition: 是否使用说话人识别
        
    Returns:
        Optional[Dict]: 现有任务信息（包含task_id和view_token），如果没有找到则返回None
    """
```

#### API接口修改

在`/api/transcribe`接口中添加去重检查：

```python
# 首先检查是否已有相同的任务
existing_task = cache_manager.get_existing_task_by_url(url, request.use_speaker_recognition)

if existing_task:
    # 根据现有任务状态决定处理策略
    # ...
else:
    # 创建新任务
    # ...
```

## 使用示例

### 场景1：重复请求已完成的任务

```bash
# 第一次请求
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer token" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/video.mp4"}'

# 响应：202，创建新任务 task_abc123，生成 view_xyz789
# 立即发送企微通知：🔗 【查看链接】🎬 转录任务已创建
#                  点击查看转录进度和结果：http://localhost:8000/view/view_xyz789
# 用户立即可以点击查看processing状态

# 任务完成后，第二次请求相同URL
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer token" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/video.mp4"}'

# 响应：202，创建新任务 task_def456，但复用 view_xyz789
# 立即发送企微通知：🔗 【查看链接】🎬 转录任务已创建
#                  点击查看转录进度和结果：http://localhost:8000/view/view_xyz789 (相同链接！)
# 由于有缓存，很快发送完成通知：✅ 【任务完成】视频标题
```

### 场景2：不同参数创建新任务

```bash
# 请求不启用说话人识别
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer token" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/video.mp4","use_speaker_recognition":false}'

# 响应：返回 task_abc123

# 请求启用说话人识别（不同参数）
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer token" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/video.mp4","use_speaker_recognition":true}'

# 响应：创建新任务 task_def456
```

## 优势

1. **一致的查看链接**：同一视频（相同参数）始终使用相同的`view_token`
2. **即时通知体验**：任务创建后立即发送包含查看链接的企微通知
3. **完整状态跟踪**：用户可以立即访问查看页面，观察从processing到完成的全过程
4. **避免重复处理**：有缓存时直接使用，避免重复下载转录
5. **灵活的任务管理**：每次请求都有独立的`task_id`用于追踪
6. **更好的用户体验**：用户可以重复使用相同的分享链接

## 测试

项目包含以下测试用例：

### 单元测试
- `tests/unit/test_view_token_reuse.py`：测试View Token复用逻辑

运行测试：
```bash
# 运行View Token复用测试
python tests/unit/test_view_token_reuse.py
```

## 注意事项

1. **参数敏感性**：`use_speaker_recognition`参数不同会创建不同任务
2. **URL完全匹配**：URL必须完全相同（包括查询参数）
3. **失败任务重试**：失败的任务不会阻止创建新任务
4. **数据库索引**：建议在`(url, use_speaker_recognition)`上创建索引以提高查询性能

## 未来改进

1. **URL标准化**：处理同一视频的不同URL格式（如短链接vs长链接）
2. **过期策略**：设置任务过期时间，过期后允许重新处理
3. **批量去重**：支持批量请求的去重处理
4. **智能匹配**：基于视频ID而非URL的去重逻辑