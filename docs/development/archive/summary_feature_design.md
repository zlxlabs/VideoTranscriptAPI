# LLM 总结功能设计文档

> **文档版本**: v1.0
> **设计时间**: 2026-01-27
> **设计原则**: 模块化 + 串行执行 + 质量优先
> **目标**: 在新架构中恢复完整的总结功能

---

## 一、背景和目标

### 1.1 当前状态

**已完成**：
- ✅ 校对功能已完全迁移到新架构
- ✅ Core 基础组件完整（LLMClient、KeyInfoExtractor、SpeakerInferencer 等）
- ✅ Processors 和 Segmenters 工作正常

**缺失**：
- ❌ **总结功能完全缺失**
- ❌ 返回结果中无 `summary_text` 字段
- ❌ 用户无法获取内容总结

### 1.2 设计目标

1. ✅ **功能完整性**：恢复旧架构的所有总结能力
2. ✅ **架构一致性**：符合新架构的模块化设计
3. ✅ **质量优先**：基于校对后的文本生成总结，确保质量
4. ✅ **易于维护**：独立的 SummaryProcessor，职责清晰
5. ✅ **向后兼容**：返回格式与旧架构保持兼容

### 1.3 核心决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| **模块设计** | 独立的 SummaryProcessor | 单一职责，易于测试和复用 |
| **执行顺序** | 串行（校对 → 总结） | 质量优先，总结基于校对文本 |
| **输入模式** | 纯文本 | 简化设计，减少复杂度 |
| **Prompt 策略** | 保留两套 Prompt | 已验证效果好，针对性强 |

---

## 二、整体架构设计

### 2.1 模块关系图

```
┌─────────────────────────────────────────────────────────┐
│                   LLMCoordinator                        │
│                                                         │
│  process(content, title, ...) → Dict                    │
│                                                         │
│  ┌────────────────────────────────────────────────┐    │
│  │  步骤 1: 路由到校对处理器                       │    │
│  │                                                 │    │
│  │  if isinstance(content, str):                   │    │
│  │      → PlainTextProcessor.process()             │    │
│  │  elif isinstance(content, list):                │    │
│  │      → SpeakerAwareProcessor.process()          │    │
│  │                                                 │    │
│  │  返回：                                          │    │
│  │  {                                              │    │
│  │      "calibrated_text": str,                    │    │
│  │      "key_info": dict,                          │    │
│  │      "stats": dict,                             │    │
│  │      "structured_data": dict (可选)             │    │
│  │  }                                              │    │
│  └────────────────────────────────────────────────┘    │
│                          ↓                              │
│  ┌────────────────────────────────────────────────┐    │
│  │  步骤 2: 生成总结（如果需要）                   │    │
│  │                                                 │    │
│  │  if len(calibrated_text) >= min_threshold:      │    │
│  │      → SummaryProcessor.process()               │    │
│  │          输入：calibrated_text                   │    │
│  │          返回：summary_text                      │    │
│  │  else:                                          │    │
│  │      summary_text = None                        │    │
│  └────────────────────────────────────────────────┘    │
│                          ↓                              │
│  ┌────────────────────────────────────────────────┐    │
│  │  步骤 3: 合并结果                               │    │
│  │                                                 │    │
│  │  return {                                       │    │
│  │      "calibrated_text": str,                    │    │
│  │      "summary_text": Optional[str],  ← 新增      │    │
│  │      "key_info": dict,                          │    │
│  │      "stats": dict,                             │    │
│  │      "structured_data": dict (可选)             │    │
│  │  }                                              │    │
│  └────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

### 2.2 执行流程对比

#### 旧架构（并行）：

```
开始
  ├─ 线程1: 校对（基于原始文本）─────┐
  └─ 线程2: 总结（基于原始文本）───┐ │
                                  ↓ ↓
                                合并结果
                                  ↓
                                返回
```

**问题**：
- ⚠️ 总结基于原始文本，可能包含错别字
- ⚠️ 总结和校对结果可能不一致

#### 新架构（串行）：

```
开始
  ↓
校对处理器（PlainTextProcessor / SpeakerAwareProcessor）
  ├─ 步骤1: 提取关键信息
  ├─ 步骤2: 分段
  ├─ 步骤3: 分段校对
  └─ 步骤4: 质量验证
  ↓
calibrated_text
  ↓
总结处理器（SummaryProcessor）
  ├─ 长度检查
  ├─ 选择 Prompt
  ├─ 调用 LLM
  └─ 验证结果
  ↓
summary_text
  ↓
合并返回
```

**优势**：
- ✅ 总结基于校对后的文本，质量更高
- ✅ 总结和校对内容一致
- ✅ 逻辑清晰，易于调试

**代价**：
- ⚠️ 时间增加约 15 秒（约 75%）

### 2.3 时间开销分析

| 场景 | 旧架构（并行） | 新架构（串行） | 差异 |
|------|--------------|--------------|------|
| 短文本（< 500字） | 8s | 8s | 0s（跳过总结） |
| 中等文本（500-2000字） | 15s | 20s | +5s（约 33%） |
| 长文本（2000-5000字） | 20s | 35s | +15s（约 75%） |
| 超长文本（> 5000字） | 35s | 55s | +20s（约 57%） |

**结论**：时间增加可接受，质量提升明显。

---

## 三、SummaryProcessor 详细设计

### 3.1 类定义

```python
# processors/summary_processor.py

from typing import Dict, Optional
from ..logging import setup_logger
from ..core.config import LLMConfig
from ..core.llm_client import LLMClient
from ..prompts import (
    SUMMARY_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER,
    build_summary_user_prompt,
)

logger = setup_logger(__name__)


class SummaryProcessor:
    """内容总结处理器

    职责：
    - 生成视频内容的文本总结
    - 根据说话人数量选择合适的 System Prompt
    - 处理长度检查和降级
    """

    def __init__(
        self,
        llm_client: LLMClient,
        config: LLMConfig,
    ):
        """初始化总结处理器

        Args:
            llm_client: LLM 客户端（含智能重试）
            config: LLM 配置对象
        """
        self.llm_client = llm_client
        self.config = config

        logger.info("SummaryProcessor initialized")

    def process(
        self,
        text: str,
        title: str,
        author: str = "",
        description: str = "",
        speaker_count: int = 0,
        transcription_data: Optional[Dict] = None,
        selected_models: Optional[Dict] = None,
    ) -> Optional[str]:
        """生成文本总结

        Args:
            text: 待总结的文本（通常是校对后的文本）
            title: 视频标题
            author: 作者/频道
            description: 视频描述
            speaker_count: 说话人数量（0 或 1 表示单说话人，>= 2 表示多说话人）
            transcription_data: 原始转录数据（可选，用于辅助分析）
            selected_models: 选定的模型配置（可选，来自风险检测）

        Returns:
            总结文本，如果文本过短则返回 None

        Raises:
            不抛出异常，出错时返回 None
        """
        # 步骤 1: 长度检查
        if len(text) < self.config.min_summary_threshold:
            logger.info(
                f"Text too short for summary: {len(text)} < {self.config.min_summary_threshold}"
            )
            return None

        logger.info(f"Generating summary for text (length: {len(text)}, speaker_count: {speaker_count})")

        try:
            # 步骤 2: 选择模型
            if selected_models:
                model = selected_models.get("summary_model", self.config.summary_model)
                reasoning_effort = selected_models.get(
                    "summary_reasoning_effort",
                    self.config.summary_reasoning_effort
                )
            else:
                model = self.config.summary_model
                reasoning_effort = self.config.summary_reasoning_effort

            # 步骤 3: 选择 System Prompt
            system_prompt = self._select_system_prompt(speaker_count)

            # 步骤 4: 构建 User Prompt
            user_prompt = build_summary_user_prompt(
                transcript=text,
                video_title=title,
                author=author,
                description=description,
                transcription_data=transcription_data,
            )

            # 步骤 5: 调用 LLM
            response = self.llm_client.call(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                reasoning_effort=reasoning_effort,
                task_type="summary",  # 标识为总结任务（用于日志追踪和监控）
            )

            summary_text = response.text

            # 步骤 6: 验证结果
            if not summary_text or len(summary_text) < 50:
                logger.warning(
                    f"Summary too short or empty: {len(summary_text) if summary_text else 0} chars"
                )
                return None

            logger.info(f"Summary generated successfully (length: {len(summary_text)})")
            return summary_text

        except Exception as e:
            logger.error(f"Summary generation failed: {e}", exc_info=True)
            return None

    def _select_system_prompt(self, speaker_count: int) -> str:
        """根据说话人数量选择 System Prompt

        Args:
            speaker_count: 说话人数量

        Returns:
            System Prompt 字符串
        """
        if speaker_count >= 2:
            # 多说话人：强调对话动态、观点碰撞
            logger.debug("Using multi-speaker summary prompt")
            return SUMMARY_SYSTEM_PROMPT
        else:
            # 单说话人：强调论点提取、逻辑结构
            logger.debug("Using single-speaker summary prompt")
            return SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER
```

### 3.2 Prompt 函数设计

```python
# prompts/prompts.py

def build_summary_user_prompt(
    transcript: str,
    video_title: str,
    author: str = "",
    description: str = "",
    transcription_data: Optional[Dict] = None,
) -> str:
    """构建总结的 User Prompt

    Args:
        transcript: 待总结的文本（校对后）
        video_title: 视频标题
        author: 作者/频道
        description: 视频描述
        transcription_data: 原始转录数据（可选）

    Returns:
        完整的 User Prompt
    """
    # 构建元数据部分
    metadata_parts = []

    if video_title:
        metadata_parts.append(f"**视频标题**: {video_title}")
    if author:
        metadata_parts.append(f"**作者/频道**: {author}")
    if description:
        metadata_parts.append(f"**视频描述**: {description}")

    metadata_text = "\n".join(metadata_parts) if metadata_parts else "无额外元数据"

    # 检测转录引擎（用于提示总结策略）
    engine_hint = ""
    if transcription_data:
        # 如果有 segments 字段，说明是 FunASR（有说话人）
        if "segments" in transcription_data:
            engine_hint = "\n**转录引擎**: FunASR（语音识别转录，包含说话人信息）"
        else:
            engine_hint = "\n**转录引擎**: CapsWriter（语音识别转录）"

    # 组装完整 Prompt
    prompt = f"""# 元数据

{metadata_text}{engine_hint}

# 待总结内容

{transcript}

---

请按照系统提示的要求，对上述内容进行详细总结。
"""

    return prompt
```

### 3.3 task_type 参数

为了日志追踪和性能监控，所有 LLM 调用都需要传递 `task_type` 参数。

**总结功能使用的 task_type**：
- `"summary"` - 基础总结（当前实现）
- `"segment_summary"` - 分段总结（未来扩展）
- `"final_summary"` - 最终总结（未来扩展）

**在代码中传递**：
```python
response = self.llm_client.call(
    model=model,
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    reasoning_effort=reasoning_effort,
    task_type="summary",  # ← 必须添加
)
```

**详细说明**：参见 `docs/development/llm/task_type_design.md`

---

### 3.4 Prompt 模板（复用旧架构）

#### 多说话人 Prompt（SUMMARY_SYSTEM_PROMPT）

```python
SUMMARY_SYSTEM_PROMPT = """你是一个专业的内容分析助手，擅长总结多人对话和访谈内容。

请按以下结构进行详细总结：

## 1. 概述（Overview）
- 简要介绍对话主题、背景和参与者
- 概括核心观点和讨论焦点
- 字数：100-150字

## 2. 关键观点（Key Points）
针对每个主要话题或讨论点：
- 📌 **话题标题**
  - 各方观点和立场
  - 关键论据和案例
  - 观点碰撞和共识
- 使用分层 bullet points 组织
- 每个话题 150-300 字

## 3. 深入分析（In-depth Analysis）
- 🔍 **论证逻辑**
  - 各方论证的逻辑链条
  - 支撑论据的强弱分析

- 💡 **洞察和启发**
  - 讨论中的关键洞察
  - 对听众的启发意义

## 4. 总结（Summary）
- 核心结论和要点回顾
- 讨论的意义和价值
- 字数：100-150字

**格式要求**：
- 使用 markdown 格式
- 适当使用 emoji 增加可读性
- 分层 bullet points 提高结构清晰度
- 专注于总结，不要在输出中体现格式要求本身
- 只返回总结内容，不要返回无关信息
"""
```

#### 单说话人 Prompt（SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER）

```python
SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER = """你是一个专业的内容分析助手，擅长总结演讲、讲座和单人解说内容。

请按以下结构进行详细总结：

## 1. 概述（Overview）
- 简要介绍主题、背景和核心观点
- 概括主要论点和讨论重点
- 字数：100-150字

## 2. 核心内容（Core Content）
针对每个主要论点或章节：
- 📌 **论点标题**
  - 论点的详细阐述
  - 支撑论据和案例
  - 逻辑推理过程
- 使用分层 bullet points 组织
- 每个论点 150-300 字

## 3. 深入分析（In-depth Analysis）
- 🔍 **逻辑结构**
  - 整体论证的逻辑框架
  - 论点之间的关联

- 💡 **关键洞察**
  - 内容中的重要观点
  - 对观众的启发意义

## 4. 总结（Summary）
- 核心观点和要点回顾
- 内容的价值和意义
- 字数：100-150字

**格式要求**：
- 使用 markdown 格式
- 适当使用 emoji 增加可读性
- 分层 bullet points 提高结构清晰度
- 专注于总结，不要在输出中体现格式要求本身
- 只返回总结内容，不要返回无关信息
"""
```

---

## 四、LLMCoordinator 改造设计

### 4.1 初始化改造

```python
# coordinator.py

class LLMCoordinator:
    """LLM 处理协调器

    职责：
    - 场景路由（纯文本 vs 有说话人）
    - 统一入口接口
    - 集成校对和总结流程
    """

    def __init__(self, config_dict: dict, cache_dir: str):
        """初始化协调器

        Args:
            config_dict: 完整的配置字典
            cache_dir: 缓存目录路径
        """
        # 创建配置对象
        self.config = LLMConfig.from_dict(config_dict)

        # 创建核心组件
        self.llm_client = LLMClient(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            max_retries=self.config.max_retries,
            retry_delay=self.config.retry_delay,
        )

        self.cache_manager = CacheManager(cache_dir=cache_dir)

        self.key_info_extractor = KeyInfoExtractor(
            llm_client=self.llm_client,
            cache_manager=self.cache_manager,
            model=self.config.key_info_model or self.config.calibrate_model,
            reasoning_effort=self.config.key_info_reasoning_effort,
        )

        self.speaker_inferencer = SpeakerInferencer(
            llm_client=self.llm_client,
            cache_manager=self.cache_manager,
            model=self.config.speaker_model or self.config.calibrate_model,
            reasoning_effort=self.config.speaker_reasoning_effort,
        )

        self.quality_validator = QualityValidator(
            llm_client=self.llm_client,
            model=self.config.validator_model or self.config.calibrate_model,
            reasoning_effort=self.config.validator_reasoning_effort,
            overall_score_threshold=self.config.overall_score_threshold,
            minimum_single_score=self.config.minimum_single_score,
        )

        # 创建校对处理器
        self.plain_text_processor = PlainTextProcessor(
            config=self.config,
            llm_client=self.llm_client,
            key_info_extractor=self.key_info_extractor,
            quality_validator=self.quality_validator,
        )

        self.speaker_aware_processor = SpeakerAwareProcessor(
            config=self.config,
            llm_client=self.llm_client,
            key_info_extractor=self.key_info_extractor,
            speaker_inferencer=self.speaker_inferencer,
            quality_validator=self.quality_validator,
        )

        # ========== 新增：创建总结处理器 ==========
        self.summary_processor = SummaryProcessor(
            llm_client=self.llm_client,
            config=self.config,
        )

        logger.info("LLM Coordinator initialized with summary support")
```

### 4.2 process() 方法改造

```python
def process(
    self,
    content: Union[str, List[Dict]],
    title: str,
    author: str = "",
    description: str = "",
    platform: str = "",
    media_id: str = "",
    has_risk: bool = False,
) -> Dict:
    """处理文本（统一入口）

    Args:
        content: 文本内容（纯文本或对话列表）
        title: 视频标题
        author: 作者
        description: 描述
        platform: 平台标识
        media_id: 媒体 ID
        has_risk: 是否有风险内容

    Returns:
        处理结果字典:
        {
            "calibrated_text": str,        # 校对后的文本
            "summary_text": Optional[str], # 总结文本（新增）
            "key_info": dict,              # 关键信息
            "stats": dict,                 # 统计信息
            "structured_data": dict,       # 结构化数据（仅有说话人）
        }
    """
    logger.info(f"Processing content for: {title}")

    # ========== 步骤 1: 选择模型 ==========
    selected_models = self.config.select_models_for_task(has_risk)

    # ========== 步骤 2: 校对处理（路由到对应处理器） ==========
    logger.info("Step 1/2: Calibration processing")

    calibration_result = self._route_to_calibration_processor(
        content=content,
        title=title,
        author=author,
        description=description,
        platform=platform,
        media_id=media_id,
        selected_models=selected_models,
    )

    # 提取校对文本和说话人信息
    calibrated_text = calibration_result.get("calibrated_text", "")
    speaker_count = self._extract_speaker_count(content, calibration_result)

    # ========== 步骤 3: 总结生成（基于校对文本） ==========
    logger.info("Step 2/2: Summary generation")

    summary_text = self._generate_summary_if_needed(
        text=calibrated_text,
        title=title,
        author=author,
        description=description,
        speaker_count=speaker_count,
        transcription_data=self._extract_transcription_data(content),
        selected_models=selected_models,
    )

    # ========== 步骤 4: 合并结果 ==========
    return {
        "calibrated_text": calibrated_text,
        "summary_text": summary_text,  # ← 新增字段
        "key_info": calibration_result.get("key_info"),
        "stats": {
            **calibration_result.get("stats", {}),
            "summary_length": len(summary_text) if summary_text else 0,  # ← 新增
        },
        "structured_data": calibration_result.get("structured_data"),
    }
```

### 4.3 辅助方法

```python
def _route_to_calibration_processor(
    self,
    content: Union[str, List[Dict]],
    title: str,
    author: str,
    description: str,
    platform: str,
    media_id: str,
    selected_models: Dict,
) -> Dict:
    """路由到对应的校对处理器

    Args:
        content: 文本内容（纯文本或对话列表）
        ... 其他参数

    Returns:
        校对结果字典
    """
    if isinstance(content, str):
        # 纯文本 - 使用 PlainTextProcessor
        logger.info("Routing to PlainTextProcessor")
        return self.plain_text_processor.process(
            text=content,
            title=title,
            author=author,
            description=description,
            platform=platform,
            media_id=media_id,
            selected_models=selected_models,
        )
    elif isinstance(content, list):
        # 对话列表 - 使用 SpeakerAwareProcessor
        logger.info("Routing to SpeakerAwareProcessor")
        return self.speaker_aware_processor.process(
            dialogs=content,
            title=title,
            author=author,
            description=description,
            platform=platform,
            media_id=media_id,
            selected_models=selected_models,
        )
    else:
        raise ValueError(f"Unsupported content type: {type(content)}")


def _extract_speaker_count(
    self,
    content: Union[str, List[Dict]],
    calibration_result: Dict,
) -> int:
    """提取说话人数量

    Args:
        content: 原始内容
        calibration_result: 校对结果

    Returns:
        说话人数量（0 表示单说话人，>= 2 表示多说话人）
    """
    # 纯文本 → 单说话人
    if isinstance(content, str):
        return 0

    # 有说话人 → 从结果中提取
    structured_data = calibration_result.get("structured_data", {})
    speaker_mapping = structured_data.get("speaker_mapping", {})
    speaker_count = len(speaker_mapping)

    logger.debug(f"Detected speaker count: {speaker_count}")
    return speaker_count


def _extract_transcription_data(
    self,
    content: Union[str, List[Dict]],
) -> Optional[Dict]:
    """提取原始转录数据（用于辅助总结）

    Args:
        content: 原始内容

    Returns:
        转录数据字典（如果是有说话人文本）
    """
    if isinstance(content, list):
        # 有说话人 → 构建 transcription_data
        return {"segments": content}
    else:
        return None


def _generate_summary_if_needed(
    self,
    text: str,
    title: str,
    author: str,
    description: str,
    speaker_count: int,
    transcription_data: Optional[Dict],
    selected_models: Dict,
) -> Optional[str]:
    """生成总结（如果需要）

    Args:
        text: 校对后的文本
        title: 视频标题
        author: 作者
        description: 描述
        speaker_count: 说话人数量
        transcription_data: 原始转录数据
        selected_models: 选定的模型

    Returns:
        总结文本，如果文本过短则返回 None
    """
    # 检查长度阈值
    if len(text) < self.config.min_summary_threshold:
        logger.info(
            f"Text too short for summary: {len(text)} < {self.config.min_summary_threshold}"
        )
        return None

    # 调用总结处理器
    logger.info(f"Generating summary (text length: {len(text)}, speaker_count: {speaker_count})")

    try:
        summary = self.summary_processor.process(
            text=text,
            title=title,
            author=author,
            description=description,
            speaker_count=speaker_count,
            transcription_data=transcription_data,
            selected_models=selected_models,
        )

        if summary:
            logger.info(f"Summary generated successfully (length: {len(summary)})")
        else:
            logger.warning("Summary generation returned None")

        return summary

    except Exception as e:
        logger.error(f"Summary generation failed: {e}", exc_info=True)
        return None
```

---

## 五、集成到 transcription.py

### 5.1 调用方式（无需修改）

```python
# transcription.py (第 1240-1280 行)

# 准备新架构的参数
content = (
    llm_task.get("transcription_data")
    if use_speaker_recognition and llm_task.get("transcription_data")
    else transcript
)

# 调用新架构（无需修改）
coordinator_result = llm_coordinator.process(
    content=content,
    title=video_title,
    author=llm_task.get("author", ""),
    description=llm_task.get("description", ""),
    platform=platform or "",
    media_id=media_id or "",
    has_risk=False,
)
```

### 5.2 结果适配（需修改）

```python
# 适配返回格式为旧架构格式（保持后续代码兼容）
calibrated_text = coordinator_result.get("calibrated_text", "")
summary_text = coordinator_result.get("summary_text")  # ← 从新架构获取

# 判断是否跳过总结
should_skip_summary = summary_text is None

result_dict = {
    "校对文本": calibrated_text,
    "内容总结": summary_text,  # ← 修改：使用新架构返回的总结
    "skip_summary": should_skip_summary,
    "stats": coordinator_result.get("stats", {}),
    "models_used": {},  # 暂未实现
    "calibrate_success": True,
    "summary_success": summary_text is not None,  # ← 修改：根据总结是否存在判断
}
```

### 5.3 总结保存逻辑（需修改）

```python
# 保存总结文本到缓存（仅在成功时保存）
if summary_success:
    if skip_summary:
        # 跳过总结时，只有校对成功才保存（使用校对文本作为总结）
        if calibrate_success:
            summary_content = calibrated_text
            logger.info(f"文本过短，保存校对文本作为总结: {task_id}")
            cache_manager.save_llm_result(
                platform=platform,
                media_id=media_id,
                use_speaker_recognition=use_speaker_recognition,
                llm_type="summary",
                content=summary_content,
            )
    else:
        # ========== 修改：使用新架构返回的总结 ==========
        if summary_text is not None:
            logger.info(f"保存LLM总结到缓存: {task_id}")
            cache_manager.save_llm_result(
                platform=platform,
                media_id=media_id,
                use_speaker_recognition=use_speaker_recognition,
                llm_type="summary",
                content=summary_text,  # ← 直接使用新架构返回的总结
            )
        else:
            logger.warning(f"总结生成失败，跳过保存: {task_id}")
else:
    logger.warning(f"总结失败，跳过保存总结文件: {task_id}")
```

---

## 六、返回格式完整说明

### 6.1 Coordinator 返回格式

```python
{
    # 校对文本（必有）
    "calibrated_text": str,

    # 总结文本（新增，可能为 None）
    "summary_text": Optional[str],

    # 关键信息（必有）
    "key_info": {
        "names": List[str],
        "places": List[str],
        "technical_terms": List[str],
        "brands": List[str],
        "abbreviations": List[str],
        "foreign_terms": List[str],
        "other_entities": List[str],
    },

    # 统计信息（必有）
    "stats": {
        "original_length": int,
        "calibrated_length": int,
        "segment_count": int,       # 纯文本特有
        "dialog_count": int,        # 有说话人特有
        "chunk_count": int,         # 有说话人特有
        "summary_length": int,      # ← 新增
    },

    # 结构化数据（仅有说话人文本有）
    "structured_data": Optional[{
        "dialogs": List[Dict],
        "speaker_mapping": Dict[str, str],
    }],
}
```

### 6.2 transcription.py 适配后格式

```python
{
    # 校对文本
    "校对文本": str,

    # 内容总结（修改：使用新架构返回值）
    "内容总结": Optional[str],

    # 是否跳过总结
    "skip_summary": bool,

    # 统计信息
    "stats": dict,

    # 模型使用情况（暂未实现）
    "models_used": dict,

    # 成功标记
    "calibrate_success": bool,
    "summary_success": bool,  # ← 修改：基于 summary_text 是否为 None 判断
}
```

### 6.3 示例

#### 示例 1：短文本（跳过总结）

```python
# Coordinator 返回
{
    "calibrated_text": "这是一段很短的文本。",
    "summary_text": None,  # ← 长度不足，跳过总结
    "key_info": {...},
    "stats": {
        "original_length": 15,
        "calibrated_length": 15,
        "segment_count": 1,
        "summary_length": 0,
    },
    "structured_data": None,
}

# transcription.py 适配后
{
    "校对文本": "这是一段很短的文本。",
    "内容总结": None,
    "skip_summary": True,  # ← summary_text 为 None
    "stats": {...},
    "models_used": {},
    "calibrate_success": True,
    "summary_success": True,  # ← 跳过总结视为成功
}
```

#### 示例 2：长文本（生成总结）

```python
# Coordinator 返回
{
    "calibrated_text": "特朗普最近表示...",
    "summary_text": "## 概述\n本视频讨论了...",  # ← 生成了总结
    "key_info": {...},
    "stats": {
        "original_length": 5000,
        "calibrated_length": 4800,
        "segment_count": 3,
        "summary_length": 800,
    },
    "structured_data": None,
}

# transcription.py 适配后
{
    "校对文本": "特朗普最近表示...",
    "内容总结": "## 概述\n本视频讨论了...",
    "skip_summary": False,  # ← summary_text 不为 None
    "stats": {...},
    "models_used": {},
    "calibrate_success": True,
    "summary_success": True,  # ← 总结生成成功
}
```

---

## 七、文件结构变化

### 7.1 新增文件

```
src/video_transcript_api/utils/llm/
└── processors/
    └── summary_processor.py  ← 新增：总结处理器
```

### 7.2 修改文件

```
src/video_transcript_api/utils/llm/
├── coordinator.py                     ← 修改：集成总结功能
├── prompts/
│   └── prompts.py                     ← 新增：build_summary_user_prompt() 函数
└── processors/
    └── __init__.py                     ← 修改：导出 SummaryProcessor

src/video_transcript_api/api/services/
└── transcription.py                   ← 修改：适配新架构返回格式
```

---

## 八、测试计划

### 8.1 单元测试

#### 测试文件：`tests/llm/test_summary_processor.py`

```python
"""测试 SummaryProcessor"""

import unittest
from unittest.mock import Mock, patch
from video_transcript_api.utils.llm.processors.summary_processor import SummaryProcessor


class TestSummaryProcessor(unittest.TestCase):
    """测试总结处理器"""

    def setUp(self):
        """设置测试配置"""
        self.config = Mock()
        self.config.summary_model = "test-model"
        self.config.summary_reasoning_effort = None
        self.config.min_summary_threshold = 500

        self.llm_client = Mock()

        self.processor = SummaryProcessor(
            llm_client=self.llm_client,
            config=self.config,
        )

    def test_short_text_returns_none(self):
        """测试短文本返回 None"""
        result = self.processor.process(
            text="这是一段很短的文本。",  # < 500 字
            title="测试",
        )
        self.assertIsNone(result)

    @patch('video_transcript_api.utils.llm.processors.summary_processor.LLMClient.call')
    def test_long_text_generates_summary(self, mock_call):
        """测试长文本生成总结"""
        # 模拟 LLM 响应
        mock_response = Mock()
        mock_response.text = "这是生成的总结。" * 50  # > 50 字
        mock_call.return_value = mock_response

        result = self.processor.process(
            text="这是一段很长的文本..." * 100,  # > 500 字
            title="测试",
        )

        self.assertIsNotNone(result)
        self.assertIn("总结", result)

    def test_single_speaker_prompt_selection(self):
        """测试单说话人 Prompt 选择"""
        system_prompt = self.processor._select_system_prompt(speaker_count=0)
        self.assertIn("单人解说", system_prompt)

    def test_multi_speaker_prompt_selection(self):
        """测试多说话人 Prompt 选择"""
        system_prompt = self.processor._select_system_prompt(speaker_count=2)
        self.assertIn("多人对话", system_prompt)

    @patch('video_transcript_api.utils.llm.processors.summary_processor.LLMClient.call')
    def test_task_type_parameter(self, mock_call):
        """测试 task_type 参数是否正确传递"""
        # 模拟 LLM 响应
        mock_response = Mock()
        mock_response.text = "这是生成的总结。" * 50
        mock_call.return_value = mock_response

        # 调用处理器
        self.processor.process(
            text="这是一段很长的文本..." * 100,
            title="测试",
        )

        # 验证 task_type 参数
        mock_call.assert_called_once()
        call_kwargs = mock_call.call_args[1]
        self.assertEqual(call_kwargs.get("task_type"), "summary")
```

### 8.2 集成测试

#### 测试文件：`tests/llm/test_coordinator_with_summary.py`

```python
"""测试 Coordinator 集成总结功能"""

import unittest
from unittest.mock import Mock, patch
from video_transcript_api.utils.llm import LLMCoordinator


class TestCoordinatorWithSummary(unittest.TestCase):
    """测试 Coordinator 总结集成"""

    def setUp(self):
        """设置测试配置"""
        self.config_dict = {
            "llm": {
                "api_key": "test_key",
                "base_url": "http://test.api.com",
                "calibrate_model": "test-model",
                "summary_model": "test-model",
                "key_info_model": "test-model",
                "speaker_model": "test-model",
                "min_summary_threshold": 500,
                "segmentation": {},
                "structured_calibration": {},
            }
        }
        self.cache_dir = "./test_cache_dir"

    @patch('video_transcript_api.utils.llm.core.llm_client.LLMClient.call')
    def test_short_text_skips_summary(self, mock_call):
        """测试短文本跳过总结"""
        # 模拟校对响应
        calibrate_response = Mock()
        calibrate_response.text = "这是校对后的短文本。"
        calibrate_response.structured_output = {
            "names": [], "places": [], "terms": [],
            "brands": [], "abbreviations": {}
        }
        mock_call.return_value = calibrate_response

        coordinator = LLMCoordinator(
            config_dict=self.config_dict,
            cache_dir=self.cache_dir,
        )

        result = coordinator.process(
            content="这是一段很短的文本。",  # < 500 字
            title="测试标题",
        )

        # 验证总结为 None
        self.assertIsNone(result["summary_text"])
        self.assertEqual(result["stats"]["summary_length"], 0)

    @patch('video_transcript_api.utils.llm.core.llm_client.LLMClient.call')
    def test_long_text_generates_summary(self, mock_call):
        """测试长文本生成总结"""
        # 模拟响应
        def mock_llm_response(*args, **kwargs):
            response = Mock()
            # 根据调用判断是校对还是总结
            if "校对" in kwargs.get("system_prompt", ""):
                response.text = "这是校对后的文本..." * 100
            elif "总结" in kwargs.get("system_prompt", ""):
                response.text = "## 概述\n这是总结内容..." * 20
            else:
                # 关键信息提取
                response.structured_output = {
                    "names": ["张三"], "places": [], "terms": [],
                    "brands": [], "abbreviations": {}
                }
            return response

        mock_call.side_effect = mock_llm_response

        coordinator = LLMCoordinator(
            config_dict=self.config_dict,
            cache_dir=self.cache_dir,
        )

        result = coordinator.process(
            content="这是一段很长的文本..." * 100,  # > 500 字
            title="测试标题",
        )

        # 验证总结生成
        self.assertIsNotNone(result["summary_text"])
        self.assertIn("概述", result["summary_text"])
        self.assertGreater(result["stats"]["summary_length"], 0)
```

### 8.3 端到端测试

#### 测试文件：`tests/llm/test_end_to_end_with_summary.py`

使用真实的 BV1JkzaBpETo 视频测试：

```python
"""端到端测试（含总结）"""

def test_e2e_with_summary():
    """测试完整流程（校对 + 总结）"""
    # 1. 加载配置
    config = get_config()

    # 2. 加载转录
    transcript = load_transcript("BV1JkzaBpETo")

    # 3. 调用 Coordinator
    coordinator = LLMCoordinator(config_dict=config, cache_dir="./data/cache")
    result = coordinator.process(
        content=transcript,
        title="特朗普盯上委内瑞拉石油？",
        author="差评",
        platform="bilibili",
        media_id="BV1JkzaBpETo",
    )

    # 4. 验证结果
    assert result["calibrated_text"]
    assert result["summary_text"]  # ← 验证总结存在
    assert len(result["summary_text"]) > 100
    assert "概述" in result["summary_text"]

    print("✅ 端到端测试通过")
```

---

## 九、实施步骤

### 阶段 1：实现核心代码（预计 3-4 小时）

#### 步骤 1.1：实现 SummaryProcessor（1-2 小时）
- [ ] 创建 `processors/summary_processor.py`
- [ ] 实现 `__init__()` 方法
- [ ] 实现 `process()` 方法
- [ ] 实现 `_select_system_prompt()` 方法
- [ ] **确保在 LLM 调用时传递 `task_type="summary"` 参数**

#### 步骤 1.2：实现 Prompt 函数（30 分钟）
- [ ] 在 `prompts/prompts.py` 中添加 `build_summary_user_prompt()`
- [ ] 验证 Prompt 格式正确

#### 步骤 1.3：改造 Coordinator（1 小时）
- [ ] 修改 `__init__()` 方法，添加 `summary_processor`
- [ ] 修改 `process()` 方法，集成总结流程
- [ ] 实现辅助方法（`_extract_speaker_count()` 等）

#### 步骤 1.4：修改 transcription.py（30 分钟）
- [ ] 修改结果适配逻辑
- [ ] 修改总结保存逻辑
- [ ] 验证集成正确

### 阶段 2：编写测试（预计 1 小时）

#### 步骤 2.1：单元测试（30 分钟）
- [ ] `test_summary_processor.py`
- [ ] `test_coordinator_with_summary.py`

#### 步骤 2.2：集成测试（30 分钟）
- [ ] `test_end_to_end_with_summary.py`
- [ ] 使用真实数据测试

### 阶段 3：测试和验证（预计 1-2 小时）

#### 步骤 3.1：运行测试
- [ ] 运行所有单元测试
- [ ] 运行集成测试
- [ ] 运行端到端测试

#### 步骤 3.2：完整流程验证
- [ ] 测试短文本（跳过总结）
- [ ] 测试长文本（生成总结）
- [ ] 测试有说话人文本
- [ ] 验证缓存保存正确

### 阶段 4：文档和清理（预计 30 分钟）

- [ ] 更新 `refactoring_completed.md`
- [ ] 更新 `switch_completed.md`
- [ ] 提交代码

---

## 十、风险和缓解措施

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| 总结生成失败 | 用户看不到总结 | 中 | 返回 None，graceful degradation；完善错误处理 |
| 性能下降 | 用户等待时间变长 | 高 | 接受（15秒增加）；未来可实现并行策略 |
| Prompt 效果不佳 | 总结质量差 | 低 | 复用旧架构已验证的 Prompt |
| LLM API 超时 | 流程卡住 | 中 | 使用 LLMClient 的重试和超时机制 |
| 兼容性问题 | 旧代码无法使用 | 低 | 保持返回格式完全兼容 |
| 缓存保存失败 | 总结丢失 | 低 | 完善错误处理和日志 |

---

## 十一、后续优化方向

### 11.1 性能优化

**并行策略**（可选）：
```python
def _generate_summary_if_needed(self, ...):
    # 判断是否并行
    use_parallel = len(text) > 5000  # 长文本并行

    if use_parallel:
        # 并行：基于原始文本生成总结（速度快）
        summary = self._generate_summary_parallel(original_text, ...)
    else:
        # 串行：基于校对文本生成总结（质量好）
        summary = self.summary_processor.process(text, ...)

    return summary
```

### 11.2 功能扩展

1. **增量总结**：超长文本分段总结后合并
2. **多级总结**：生成详细版和精简版两种总结
3. **个性化**：根据用户偏好调整总结风格
4. **缓存优化**：缓存基于特定文本的总结结果

### 11.3 质量提升

1. **总结质量评估**：引入质量验证器评估总结质量
2. **总结对比**：对比基于原始文本和校对文本的总结差异
3. **用户反馈**：收集用户对总结质量的反馈

---

## 十二、总结

本设计文档详细说明了在新架构中集成总结功能的完整方案。

**核心要点**：
1. ✅ 独立的 `SummaryProcessor`，职责清晰
2. ✅ 串行执行（校对 → 总结），质量优先
3. ✅ 纯文本输入模式，简化设计
4. ✅ 保留两套 Prompt，针对性强
5. ✅ 完全向后兼容，无缝集成

**实施路径**：
1. 实现核心代码（3-4 小时）
2. 编写测试（1 小时）
3. 测试验证（1-2 小时）
4. 文档更新（30 分钟）

**总计工作量**：约半天

**风险可控**，设计清晰，可以开始实施。
