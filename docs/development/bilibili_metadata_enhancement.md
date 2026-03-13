# Bilibili 元数据增强实现报告

## 概述

本次更新为 Bilibili 下载器添加了从官方 API 获取完整元数据的功能，解决了原有实现中只能获取 `title` 而缺少 `description` 和 `author` 的问题。

## 实现时间

2026-01-28

## 问题背景

在原有实现中，Bilibili 视频的元数据获取存在以下局限：

- **TikHub API 方式**：虽然能获取 `author`，但获取的信息可能不完整
- **BBDown 方式**：只能从文件名中提取 `title`，无法获取 `description` 和 `author`
- **缺失字段**：`description`（视频简介）字段始终为空

## 解决方案

### 核心思路

使用 **Bilibili 官方 API**（`https://api.bilibili.com/x/web-interface/view`）获取完整的视频元数据，然后与现有下载器数据合并。

### 技术实现

#### 1. 新增方法：`_fetch_bilibili_official_metadata()`

```python
def _fetch_bilibili_official_metadata(self, bvid: str) -> dict:
    """
    调用Bilibili官方API获取视频元数据

    返回字段：
    - title: 视频标题
    - description: 视频简介 ✨（新增）
    - author: 作者昵称
    - author_id: 作者mid
    - duration: 视频时长（秒）
    - pubdate: 发布时间戳
    """
```

**关键特性**：

- ✅ 无需登录即可访问公开视频
- ✅ 实例级缓存（`self._cached_metadata`）
- ✅ 完整的错误处理和降级机制
- ✅ 请求头伪装，避免反爬虫拦截
- ✅ 5 秒超时控制

#### 2. 更新方法：`_fetch_metadata()`

```python
def _fetch_metadata(self, url: str, video_id: str) -> VideoMetadata:
    """
    优先使用B站官方API获取完整元数据，
    然后用下载器API/BBDown的结果补充或覆盖某些字段
    """
```

**数据合并策略**：

1. 首先调用官方 API 获取完整元数据
2. 然后调用现有的下载器方法（TikHub API 或 BBDown）
3. 按优先级合并数据：
   - `title`: 官方 API > 下载器
   - `author`: 官方 API > 下载器
   - `description`: **仅从官方 API 获取**
   - `duration`: 官方 API 优先

## 实现细节

### 代码变更

**文件**：`src/video_transcript_api/downloaders/bilibili.py`

**变更内容**：

1. 导入 `requests` 库
2. 添加 `self._cached_metadata` 缓存字典
3. 新增 `_fetch_bilibili_official_metadata()` 方法（约 80 行）
4. 重构 `_fetch_metadata()` 方法（约 30 行）

**总代码量**：约 110 行

### 请求头配置

```python
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...",
    "Referer": f"https://www.bilibili.com/video/{bvid}",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
```

## 测试验证

### 测试用例

**测试文件**：`tests/platforms/test_bilibili_metadata.py`

**测试项目**：

1. ✅ 直接 API 元数据获取
2. ✅ 缓存命中验证
3. ✅ 集成元数据获取（通过 `_fetch_metadata`）
4. ✅ 错误处理（无效 BV ID）

**测试结果**：全部通过 ✅

### 测试视频

- **URL**: `https://www.bilibili.com/video/BV1zW2vB2Ey2`
- **标题**: "为什么老司机看一眼就明白的事，你的车却要开到跟前才急刹？【差评君】"
- **作者**: "差评君"
- **简介**: "从VA到VLA，为什么加入了语言（L）部分，智驾的体验就能大幅度提升？\n今天我们想跟大家聊聊的是：语言模型是如何改变智驾的？"
- **时长**: 704 秒（11 分 44 秒）

### 演示脚本

**文件**：`tests/platforms/demo_bilibili_metadata.py`

运行演示可查看完整的元数据获取效果。

## 性能影响

### API 请求性能

- **平均响应时间**：< 1 秒
- **超时设置**：5 秒
- **缓存策略**：实例级缓存（任务内复用）

### 对现有流程的影响

- **额外时间开销**：< 1 秒（首次请求）
- **缓存命中后**：0 秒（实例级缓存）
- **下载流程**：完全不受影响

## 兼容性保证

### 向后兼容

- ✅ 不影响现有代码逻辑
- ✅ API 失败时优雅降级
- ✅ `description` 字段为可选（失败时为空字符串）

### 错误处理

| 错误类型 | 处理方式 |
|---------|---------|
| 网络超时 | 返回空字典，使用下载器数据 |
| API 返回错误 | 记录警告日志，返回空字典 |
| JSON 解析失败 | 记录警告日志，返回空字典 |
| 未知异常 | 记录错误日志，返回空字典 |

## 额外收益

除了 `description` 和 `author`，此次实现还带来了以下额外数据：

| 字段 | 说明 | 用途 |
|-----|------|------|
| `author_id` | 作者 mid | 可用于作者去重、统计 |
| `pubdate` | 发布时间戳 | 可用于时间排序、筛选 |
| `duration` | 视频时长（秒）| 更准确的时长信息 |

这些字段存储在 `VideoMetadata.extra` 字典中。

## 使用示例

### 基本用法

```python
from src.video_transcript_api.downloaders.bilibili import BilibiliDownloader

downloader = BilibiliDownloader()
url = "https://www.bilibili.com/video/BV1zW2vB2Ey2"
bvid = "BV1zW2vB2Ey2"

# 获取元数据
metadata = downloader._fetch_metadata(url, bvid)

print(f"标题: {metadata.title}")
print(f"作者: {metadata.author}")
print(f"简介: {metadata.description}")  # ✨ 新增字段
print(f"时长: {metadata.duration} 秒")
print(f"作者ID: {metadata.extra['author_id']}")
```

### 与现有流程集成

此功能已自动集成到 `BilibiliDownloader` 的标准流程中，无需额外调用。

## 总结

### 实现亮点

1. ✅ **功能完整**：成功获取 `description`、`author` 等完整元数据
2. ✅ **性能优秀**：实例级缓存，< 1 秒响应时间
3. ✅ **健壮可靠**：完善的错误处理和降级机制
4. ✅ **无缝集成**：不影响现有代码，向后兼容
5. ✅ **额外收益**：获取 `author_id`、`pubdate`、`duration` 等额外字段

### 测试覆盖

- ✅ 单元测试：4 个测试用例全部通过
- ✅ 集成测试：与 BBDown 和 TikHub API 无缝集成
- ✅ 错误测试：各种异常情况正常处理

### 代码质量

- ✅ 遵循项目编码规范
- ✅ 完整的中文注释和 docstring
- ✅ 合理的日志记录
- ✅ 类型提示完整

## 后续优化建议

### 可选优化

1. **全局缓存**：将元数据缓存到 SQLite（跨任务复用）
2. **批量获取**：支持一次 API 请求获取多个视频元数据（如果有需要）
3. **配置开关**：允许用户禁用官方 API（通过配置文件）

### 监控建议

- 监控官方 API 的响应时间和成功率
- 统计缓存命中率

## 参考资料

- [Bilibili API 文档](https://github.com/SocialSisterYi/bilibili-API-collect)
- [项目 README](../../README.md)
- [BBDown 使用指南](../guides/api/bbdown_guide.md)
