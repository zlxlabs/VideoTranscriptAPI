# 风控模块使用指南

## 概述

风控模块用于检测和处理企业微信通知中的敏感词，防止因发送敏感内容导致账号被封。

### 核心功能

1. **从云端URL加载敏感词库**：支持多个URL，自动合并去重
2. **智能敏感词检测**：不区分大小写，自动排除URL中的内容
3. **三种消敏策略**：
   - 总结文本：如有敏感词则整体替换为「内容风险，请通过url查看」
   - 标题/作者：移除敏感词后取前6字符
   - 普通文本：移除所有敏感词
4. **本地缓存**：下载失败时使用本地缓存
5. **只处理发送内容**：不修改数据源（缓存、数据库）

---

## 配置方法

### 1. 编辑配置文件

在 `config/config.json` 中添加风控配置：

```json
{
  "risk_control": {
    "enabled": true,  // 启用风控
    "sensitive_word_urls": [
      "https://raw.githubusercontent.com/konsheng/Sensitive-lexicon/refs/heads/main/Vocabulary/%E6%96%B0%E6%80%9D%E6%83%B3%E5%90%AF%E8%92%99.txt",
      "https://your-other-source.com/words.txt"  // 可以添加多个URL
    ],
    "cache_file": "./data/risk_control/sensitive_words.txt"
  }
}
```

### 2. 配置参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `enabled` | 是否启用风控模块 | `false` |
| `sensitive_word_urls` | 敏感词库URL列表 | `[]` |
| `cache_file` | 本地缓存文件路径 | `./data/risk_control/sensitive_words.txt` |

---

## 工作原理

### 1. 初始化流程

服务启动时：
1. 从配置的URL列表下载敏感词库
2. 合并所有词库，转小写并去重
3. 保存到本地缓存文件
4. 如果下载失败，使用本地缓存

### 2. 消敏处理流程

发送企业微信通知前：

```
原文本
  ↓
提取所有URL位置
  ↓
在非URL部分检测敏感词
  ↓
根据文本类型应用不同消敏策略
  ↓
发送处理后的文本
```

### 3. 三种消敏策略

#### 策略1：总结文本 (text_type="summary")

如果检测到敏感词，整个总结文本会被替换为：
```
内容风险，请通过url查看
```

**示例**：
- 原文：`"这是一段包含敏感词的总结文本，共3000字..."`
- 处理后：`"内容风险，请通过url查看"`

**适用场景**：LLM生成的总结文本

---

#### 策略2：标题/作者 (text_type="title" / "author")

移除所有敏感词后，取前6个字符。

**示例**：
- 原标题：`"关键时刻必有关键抉择——习近平经济思想..."`
- 检测到：`["习近平"]`
- 处理后：`"关键时刻必"`（移除敏感词后取前6字符）

**适用场景**：视频标题、作者名称

---

#### 策略3：普通文本 (text_type="general")

直接移除所有敏感词，保留其余内容。

**示例**：
- 原文：`"这是test文本，还有test"`
- 检测到：`["test"]`
- 处理后：`"这是文本，还有"`

**URL排除示例**：
- 原文：`"访问 https://test.com 查看test结果"`
- 处理后：`"访问 https://test.com 查看结果"`
  - URL中的"test"不被移除
  - 文本中的"test"被移除

**适用场景**：任务状态更新、错误信息等

---

## 应用范围

风控模块自动应用于所有企业微信通知：

### 1. 任务状态通知 (notify_task_status)
- 视频标题：使用 `text_type="title"`
- 作者名：使用 `text_type="author"`
- 状态信息：通过整体消息消敏

### 2. LLM处理结果 (send_long_text_wechat)
- 校对文本：使用 `text_type="general"`
- 总结文本：使用 `text_type="summary"`
- 标题：使用 `text_type="title"`

### 3. 查看链接通知 (send_view_link_wechat)
- 标题：使用 `text_type="title"`

---

## 日志说明

### 启动日志

```
INFO - 正在初始化风控模块...
INFO - Starting to load sensitive words from URLs...
INFO - Successfully downloaded 150 words from https://...
INFO - Successfully loaded 150 sensitive words from URLs
INFO - 风控模块初始化完成
```

### 检测日志

发现敏感词时会输出警告日志：

```
WARNING - Detected 2 sensitive words in summary text: ['敏感词1', '敏感词2']
WARNING - Title contains sensitive words: ['敏感词']
```

### 错误日志

```
ERROR - Failed to download from URL: connection timeout
WARNING - Failed to download from all URLs
INFO - Successfully loaded 100 sensitive words from cache
```

---

## 维护管理

### 1. 更新敏感词库

**方法1：修改URL源**
- 敏感词库维护在云端
- 重启服务自动更新

**方法2：手动编辑缓存文件**
- 编辑 `./data/risk_control/sensitive_words.txt`
- 每行一个敏感词
- 重启服务生效

### 2. 查看缓存文件

```bash
cat ./data/risk_control/sensitive_words.txt
```

文件格式：
```
敏感词1
敏感词2
test
...
```

### 3. 清空缓存

```bash
rm ./data/risk_control/sensitive_words.txt
```

重启服务会重新下载。

---

## 测试验证

### 运行测试脚本

```bash
python tests/features/test_risk_control.py
```

测试内容：
1. 普通文本敏感词移除
2. 总结文本风控提示替换
3. 标题/作者截断处理
4. URL排除功能
5. 不区分大小写匹配
6. 中英文混合处理
7. 长文本处理

### 手动测试

1. 启用风控：`config.json` 中设置 `"enabled": true`
2. 添加测试敏感词到URL或本地缓存
3. 提交包含敏感词的转录任务
4. 检查企业微信通知中的文本是否被正确处理

---

## 性能说明

### 影响

- **词库大小**：≤ 1000词，检测速度 < 1ms
- **文本长度**：每10000字符约增加 2-3ms
- **内存占用**：词库约占用 < 1MB 内存

### 优化建议

- 词库保持在1000词以内
- 如需扩展到10000+词，可升级为AC自动机算法

---

## 常见问题

### Q: 风控会修改数据库中的内容吗？

**A**: 不会。风控只在发送企业微信通知前处理文本，不修改缓存、数据库中的原始内容。

### Q: 如果敏感词库下载失败怎么办？

**A**: 系统会自动使用本地缓存文件。首次启动建议确保网络通畅。

### Q: 可以临时禁用风控吗？

**A**: 可以。在 `config.json` 中设置 `"enabled": false`，重启服务即可。

### Q: 为什么URL中的敏感词没有被移除？

**A**: 这是设计行为。URL处理会导致链接失效，因此所有 `http://` 和 `https://` 链接中的内容都会被排除。

### Q: 总结文本为什么全部替换为风控提示？

**A**: 为了最大限度保护账号安全。如果总结包含敏感词，用户可以通过URL查看完整内容（页面不受风控限制）。

### Q: 标题为什么只保留前6字符？

**A**: 这是一个平衡策略：既能让用户识别内容，又最大限度降低风险。如果标题很短（≤6字符），则显示全部。

### Q: 风控失败会影响正常发送吗？

**A**: 不会。如果风控处理失败（异常），系统会继续使用原文本发送，并记录错误日志。

---

## 最佳实践

1. **定期更新词库**：使用云端URL，保持敏感词库最新
2. **监控日志**：关注 `WARNING` 级别日志，了解敏感词检测情况
3. **适度控制词库大小**：过大词库会影响性能，建议 < 1000词
4. **测试后上线**：新增敏感词后先测试，确认处理效果
5. **备份缓存文件**：定期备份 `sensitive_words.txt`
6. **合理设置URL**：确保查看链接可访问，以便用户查看被风控的完整内容

---

## 技术架构

```
src/video_transcript_api/utils/risk_control/
├── __init__.py                    # 模块入口，提供统一接口
├── sensitive_words_manager.py     # 敏感词库管理器
└── text_sanitizer.py              # 文本消敏处理器
```

### 核心接口

```python
from src.video_transcript_api.utils.risk_control import (
    init_risk_control,    # 初始化风控模块
    is_enabled,           # 检查是否启用
    sanitize_text         # 消敏处理
)

# 初始化
init_risk_control(config)

# 检查状态
if is_enabled():
    # 消敏处理
    result = sanitize_text("包含敏感词的文本", text_type="general")
    print(result["sanitized_text"])

    # 总结文本
    result = sanitize_text("总结内容", text_type="summary")

    # 标题处理
    result = sanitize_text("标题文本", text_type="title")
```

---

## 更新日志

### v2.0.0 (2025-10-17)

- ✅ **重大变更**：完全重写消敏策略
- ✅ 新增总结文本风控提示替换
- ✅ 新增标题/作者截断处理
- ✅ 移除随机字符插入逻辑
- ✅ 简化配置（移除 `safe_char_pool`）
- ✅ 更新所有测试用例

### v1.0.0 (2025-10-17)

- ✅ 实现敏感词检测和消敏功能
- ✅ 支持云端词库自动更新
- ✅ URL排除功能
- ✅ 不区分大小写匹配
- ✅ 集成到企业微信通知系统
- ✅ 完整的测试覆盖
