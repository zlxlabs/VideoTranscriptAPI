# LLM 模块迁移指南

> **当前状态**: 新旧架构并存，完全向后兼容
> **目标**: 逐步迁移到新架构，最终移除旧代码

---

## 一、何时可以移除旧架构？

### ✅ 移除条件（所有条件都满足时）

1. **生产代码已完全迁移**
   - ✅ API 路由层已切换到新架构
   - ✅ 后台任务处理已切换到新架构
   - ✅ 所有功能测试通过

2. **新架构稳定运行**
   - ✅ 在生产环境运行至少 **2 周**
   - ✅ 没有出现重大 bug 或性能问题
   - ✅ 日志显示新架构正常工作

3. **测试覆盖完整**
   - ✅ 所有旧架构的测试用例已迁移到新架构
   - ✅ 新架构测试通过率 >= 95%
   - ✅ 集成测试全部通过

4. **团队准备就绪**
   - ✅ 团队成员熟悉新架构
   - ✅ 文档已更新完毕
   - ✅ 回滚方案已准备

---

## 二、迁移路线图

### 阶段 1: 生产代码迁移（当前阶段）

#### 步骤 1.1: 创建新的 context 函数

**文件**: `src/video_transcript_api/api/context.py`

```python
from functools import lru_cache
from ..utils.llm import LLMCoordinator

@lru_cache
def get_llm_coordinator():
    """获取 LLM 协调器（新架构）"""
    config = get_config()
    cache_dir = config.get("cache", {}).get("cache_dir", "./data/cache")
    return LLMCoordinator(config_dict=config, cache_dir=cache_dir)

# 保留旧函数以便回滚
@lru_cache
def get_enhanced_llm_processor():
    """获取增强 LLM 处理器（旧架构，已废弃）"""
    return EnhancedLLMProcessor(get_config())
```

#### 步骤 1.2: 更新 API 路由

**文件**: `src/video_transcript_api/api/routes/views.py`

**当前代码**:
```python
from ...utils.llm import EnhancedLLMProcessor
from ..context import get_enhanced_llm_processor

llm_processor = get_enhanced_llm_processor()
result = llm_processor.process_llm_task(llm_task)
```

**新代码**:
```python
from ...utils.llm import LLMCoordinator
from ..context import get_llm_coordinator

coordinator = get_llm_coordinator()

# 准备数据
if use_speaker_recognition:
    content = transcription_data["dialogs"]  # List[Dict]
else:
    content = transcript  # str

# 调用新架构
result = coordinator.process(
    content=content,
    title=video_title,
    author=author,
    description=description,
    platform=platform,
    media_id=media_id,
    has_risk=has_risk,
)
```

#### 步骤 1.3: 适配返回格式

新架构的返回格式与旧架构略有不同，需要适配：

```python
# 新架构返回格式
{
    "calibrated_text": str,           # 校对后文本
    "key_info": dict,                 # 关键信息
    "stats": dict,                    # 统计信息
    "structured_data": dict,          # 仅对话场景有，包含 dialogs 和 speaker_mapping
}

# 如果需要兼容旧格式，添加适配层
if "structured_data" in result:
    # 对话场景
    calibrated_text = result["calibrated_text"]
    dialogs = result["structured_data"]["dialogs"]
    speaker_mapping = result["structured_data"]["speaker_mapping"]
else:
    # 纯文本场景
    calibrated_text = result["calibrated_text"]
```

### 阶段 2: 灰度发布（预计 1-2 周）

1. **功能开关控制**
   ```python
   # 在配置中添加开关
   config["llm"]["use_new_architecture"] = True

   # 在代码中使用开关
   if config["llm"].get("use_new_architecture", False):
       coordinator = get_llm_coordinator()
       result = coordinator.process(...)
   else:
       processor = get_enhanced_llm_processor()
       result = processor.process_llm_task(...)
   ```

2. **监控指标**
   - 请求成功率
   - 平均响应时间
   - 错误率
   - 降级次数

3. **逐步放量**
   - 第 1-3 天: 10% 流量
   - 第 4-7 天: 30% 流量
   - 第 8-10 天: 50% 流量
   - 第 11-14 天: 100% 流量

### 阶段 3: 测试代码迁移（预计 3-5 天）

#### 需要迁移的测试文件

**核心测试**（优先迁移）:
- `tests/llm/test_structured_calibration.py`
- `tests/llm/test_segmentation.py`
- `tests/llm/test_segmentation_simple.py`

**功能测试**:
- `tests/features/test_risk_model_selection.py`
- `tests/features/test_risk_validator_model.py`
- `tests/features/test_risk_model_selection_shared.py`
- `tests/features/test_wechat_notification.py`

**总结测试**:
- `tests/llm/test_summary_prompt_improvement.py`
- `tests/llm/test_summary_conditional_sections.py`

**手动测试** (可保留或删除):
- `tests/manual/test_llm_failure_with_formatting.py`
- `tests/manual/test_transcript_formatting.py`

#### 测试迁移示例

**旧代码**:
```python
from video_transcript_api.utils.llm import EnhancedLLMProcessor

processor = EnhancedLLMProcessor(config)
result = processor.process_llm_task(llm_task)
```

**新代码**:
```python
from video_transcript_api.utils.llm import LLMCoordinator

coordinator = LLMCoordinator(config_dict=config, cache_dir="./test_cache")
result = coordinator.process(
    content=llm_task["transcription_data"],
    title=llm_task["video_title"],
    author=llm_task.get("author", ""),
    description=llm_task.get("description", ""),
    platform=llm_task.get("platform", ""),
    media_id=llm_task["media_id"],
)
```

### 阶段 4: 清理旧代码（预计 1 天）

#### 可以移除的文件

**核心文件**（慎重！）:
- `src/video_transcript_api/utils/llm/llm_enhanced.py` (1900+ 行)
- `src/video_transcript_api/utils/llm/llm_segmented.py`
- `src/video_transcript_api/utils/llm/structured_calibrator.py`
- `src/video_transcript_api/utils/llm/text_segmentation.py`

**保留的文件**（新架构仍在使用）:
- `src/video_transcript_api/utils/llm/llm.py` (基础 API 调用)
- `src/video_transcript_api/utils/llm/prompts/` (Prompt 模板)
- `src/video_transcript_api/utils/llm/prompts/schemas/` (Schema 定义)

#### 清理 __init__.py 导出

**文件**: `src/video_transcript_api/utils/llm/__init__.py`

**移除导出**:
```python
# 移除这些导出
from .llm_enhanced import EnhancedLLMProcessor
from .llm_segmented import SegmentedLLMProcessor
from .structured_calibrator import StructuredCalibrator
from .text_segmentation import TextSegmentationProcessor
```

**保留导出**:
```python
# 保留这些
from .coordinator import LLMCoordinator
from .core import *
from .processors import *
from .segmenters import *
```

---

## 三、迁移检查清单

### 迁移前检查

- [ ] 新架构测试通过率 >= 95%
- [ ] 已阅读并理解新架构文档
- [ ] 已准备好回滚方案
- [ ] 已通知团队成员迁移计划

### 生产代码迁移检查

- [ ] 更新 `context.py` 添加 `get_llm_coordinator()`
- [ ] 更新 API 路由使用新协调器
- [ ] 添加功能开关支持灰度发布
- [ ] 本地测试全部通过
- [ ] 代码审查已完成

### 灰度发布检查

- [ ] 10% 流量测试 3 天，无问题
- [ ] 30% 流量测试 3 天，无问题
- [ ] 50% 流量测试 3 天，无问题
- [ ] 100% 流量测试 3 天，无问题
- [ ] 监控指标正常

### 测试代码迁移检查

- [ ] 核心测试已迁移
- [ ] 功能测试已迁移
- [ ] 所有测试通过
- [ ] 测试覆盖率未降低

### 清理代码检查

- [ ] 所有生产代码已停止使用旧架构
- [ ] 所有测试代码已停止使用旧架构
- [ ] 备份旧代码到 Git 分支（以防万一）
- [ ] 移除旧文件
- [ ] 更新 __init__.py 导出
- [ ] 最终测试全部通过

---

## 四、回滚方案

如果迁移过程中出现问题，可以快速回滚：

### 方案 1: 功能开关回滚

```python
# 在配置中设置
config["llm"]["use_new_architecture"] = False
```

### 方案 2: Git 回滚

```bash
# 查看提交历史
git log --oneline

# 回滚到迁移前的提交
git revert <commit-hash>
```

### 方案 3: 代码回滚

```python
# 在 context.py 中切换回旧函数
def get_active_llm_processor():
    """获取当前使用的 LLM 处理器"""
    # 临时切回旧架构
    return get_enhanced_llm_processor()
    # return get_llm_coordinator()
```

---

## 五、移除时间建议

基于以上分析，建议的移除时间线：

| 阶段 | 时间 | 说明 |
|------|------|------|
| **准备阶段** | 当前 | 完成新架构开发和测试 ✅ |
| **生产迁移** | 第 1 周 | 完成 API 层代码迁移 |
| **灰度发布** | 第 2-3 周 | 逐步放量，监控稳定性 |
| **测试迁移** | 第 4 周 | 迁移所有测试代码 |
| **稳定观察** | 第 5-6 周 | 100% 新架构运行，观察稳定性 |
| **移除旧代码** | 第 7 周 | 满足所有条件后，移除旧架构 |

**最早移除时间**: **6 周后**（约 1.5 个月）

**推荐移除时间**: **8-10 周后**（约 2-2.5 个月），更稳妥

---

## 六、风险提示

### ⚠️ 不要过早移除的原因

1. **功能差异风险**: 新架构可能存在未发现的边界情况
2. **性能差异风险**: 新架构的并发处理可能有不同的性能特征
3. **数据兼容性风险**: 缓存格式、返回格式可能有细微差异
4. **回归测试不足**: 某些边界场景可能未被测试覆盖

### ✅ 安全移除的标志

1. **零回滚**: 灰度发布期间没有出现需要回滚的情况
2. **性能稳定**: 响应时间、成功率等指标持续稳定
3. **团队信心**: 所有团队成员对新架构有信心
4. **文档完善**: 所有相关文档已更新完毕

---

## 七、总结

### 当前状态（2026-01-27）

- ✅ 新架构开发完成
- ✅ 测试通过率 95% (20/21)
- ✅ 文档已完善
- ⏳ 生产代码尚未迁移

### 下一步行动

1. **立即可做**: 在开发/测试环境使用新架构
2. **本周可做**: 准备生产代码迁移方案
3. **下周可做**: 开始生产代码迁移和灰度发布
4. **6-8 周后**: 考虑移除旧架构代码

### 最终建议

**不要急于移除旧代码**。保持新旧并存状态 1.5-2.5 个月，让新架构在真实生产环境中充分验证后，再考虑清理旧代码。这样可以：

1. 降低风险
2. 保留回滚路径
3. 给团队充足的适应时间
4. 发现并修复边界情况

**记住**: 代码可以重构，但稳定性和可靠性更重要！
