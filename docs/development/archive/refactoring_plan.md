# LLM 校对流程重构方案

> **文档版本**: v1.1
> **设计时间**: 2026-01-27
> **最后更新**: 2026-01-27（优化缓存策略 + 明确文本类型支持）
> **设计原则**: 独立实现 + 共享基础组件
> **目标**: 统一校对思路，降低维护成本，提高可扩展性

---

## 一、当前分块方案确认

### 1.1 无说话人文本分段

**位置**: `text_segmentation.py` → `segment_txt_content()`

#### 1.1.1 支持的文本类型

| 文本类型 | 来源 | 标点密度 | 格式特征 | 分段策略 |
|---------|------|---------|---------|---------|
| **YouTube 字幕** | `youtube.py:get_subtitle()` | >= 5/1000 | 有标点，连续文本 | 按句子分段 |
| **SRT/VTT 字幕** | 第三方字幕文件 | >= 5/1000 | 有标点，连续文本 | 按句子分段 |
| **CapsWriter 转录** | `capswriter` 转录引擎 | < 5/1000 | 短句换行，无标点 | 按行分段 |

#### 1.1.2 标点密度检测原理

**为什么需要标点密度检测？**

CapsWriter 和正常文本（YouTube/SRT 字幕）的分段策略完全不同：

**CapsWriter 格式示例**:
```
这是第一句话
然后是第二句
接着是第三句话
这里是第四句
```
- 每行就是一个语义单元
- 几乎没有标点符号
- 标点密度 = 0/100 × 1000 = **0 < 5** ✅
- **处理**: 按行分段（每行作为片段）

**YouTube 字幕示例**:
```
这是一段完整的演讲。主要讨论了人工智能的发展。特别是大语言模型的应用。包括 ChatGPT 和 Claude 等产品。
```
- 有标点符号，连续文本
- 假设 100 字符，4 个句号
- 标点密度 = 4/100 × 1000 = **40 > 5** ✅
- **处理**: 按句子分段（按 `。！？` 切分）

**阈值验证**:

| 文本类型 | 平均句长 | 1000字句数 | 标点数 | 标点密度 | 判定 |
|---------|---------|----------|-------|---------|------|
| 正常中文（密集） | 20-30字 | 33-50句 | 33-50 | 33-50 | >= 5 ✅ |
| 正常中文（稀疏） | 50字 | 20句 | 20 | 20 | >= 5 ✅ |
| CapsWriter | 无句子概念 | - | 0 | 0 | < 5 ✅ |

**结论**: 阈值 **5/1000** 可以有效区分两种格式。

---

#### 1.1.3 分段策略详解

**策略 1: 格式检测**
```python
# text_segmentation.py:122-128
punctuation_count = content.count('。') + content.count('！') + content.count('？') + \
                    content.count('!') + content.count('?')
punctuation_density = (punctuation_count / text_length) * 1000

is_capswriter_format = punctuation_density < 5
```

**策略 2: CapsWriter 格式 - 按行分段**
```python
# 按换行符分割
lines = [line.strip() for line in content.split('\n') if line.strip()]

# 累积到 segment_size（2000）字符时结束当前段
current_segment = ""
for line in lines:
    current_segment = self._append_fragment(line, segments, current_segment)
```

**策略 3: 正常文本 - 按句子分段**
```python
# 按标点符号 。！？ 分割
sentences = re.split(r'[。！？]', content)

# 累积到 segment_size 时结束当前段
for sentence in sentences:
    fragment = sentence + "。"
    current_segment = self._append_fragment(fragment, segments, current_segment)
```

**策略 4: 通用片段处理**
```python
def _append_fragment(fragment, segments, current_segment):
    """确保单个片段不超过 max_segment_size，达到 segment_size 时落盘"""
    # 如果当前段 + 新片段 > max_segment_size（3000）→ 先落盘当前段
    # 如果当前段长度 >= segment_size（2000）→ 落盘
```

**评价**: ✅ 保证句子连贯性（按句子/行边界分段）

---

### 1.2 有说话人文本分段

**位置**: `structured_calibrator.py` → `_intelligent_chunking()`

**策略**:
1. 遍历对话列表（每个对话包含 `speaker`, `text`, `start_time`）
2. **策略 1**: 单个对话过长（> `max_chunk_length`=1500）→ 拆分该对话
3. **策略 2**: 加入当前对话会超长 → 结束当前 chunk，开始新 chunk
4. **策略 3**: 达到理想长度（>= `preferred_chunk_length`=800）→ 结束当前 chunk
5. **最后处理**: 如果最后一个 chunk 太短（< `min_chunk_length`=300）→ 合并到前一个

**配置参数**:
```python
min_chunk_length: 300       # 最小块长度
max_chunk_length: 1500      # 最大块长度
preferred_chunk_length: 800 # 理想块长度
```

**代码逻辑**:
```python
for dialog in dialogs:
    dialog_length = len(dialog['text'])

    # 单个对话太长 → 拆分
    if dialog_length > max_chunk_length:
        sub_dialogs = _split_long_dialog(dialog)
        for sub_dialog in sub_dialogs:
            chunks.append([sub_dialog])
        continue

    # 加入会超长 → 结束当前 chunk
    if current_length + dialog_length > max_chunk_length:
        chunks.append(current_chunk)
        current_chunk = [dialog]
        current_length = dialog_length
    else:
        current_chunk.append(dialog)
        current_length += dialog_length

        # 达到理想长度 → 结束 chunk
        if current_length >= preferred_chunk_length:
            chunks.append(current_chunk)
            current_chunk = []
            current_length = 0
```

**评价**: ✅ 按长度分段 + 保持对话完整性（不会在对话中间切断）

---

## 二、设计变更记录

### v1.1 更新（2026-01-27）

基于需求反馈，对原方案进行以下调整：

#### 2.1 缓存策略优化

**变更前（v1.0）**:
- 关键信息缓存：`cache_dir/key_info_cache.json`（所有视频在一个 JSON 文件）
- 说话人映射缓存：`cache_dir/speaker_mapping_cache.json`（所有视频在一个 JSON 文件）

**变更后（v1.1）**:
- 统一放到视频缓存目录：`cache_dir/platform/YYYY/YYYYMM/media_id/`
  - `key_info.json`：关键信息缓存
  - `speaker_mapping.json`：说话人映射缓存

**变更原因**:
1. **系统一致性**: 与现有转录缓存在同一目录，便于统一管理
2. **长期运行**: 分散存储避免单文件过大
3. **清理方便**: 删除视频文件夹即清理所有相关数据
4. **并发安全**: 独立文件避免并发冲突

#### 2.2 文本类型支持明确

补充说明无说话人文本处理器支持的文本类型：
- ✅ **YouTube 原生字幕**（标点密度 >= 5/1000）
- ✅ **SRT/VTT 字幕**（标点密度 >= 5/1000）
- ✅ **CapsWriter 转录**（标点密度 < 5/1000）

#### 2.3 标点密度检测说明

补充标点密度检测的原理和必要性说明，明确：
- **为什么需要**: 区分 CapsWriter 和正常文本的分段策略
- **阈值验证**: 5/1000 可以有效区分两种格式
- **检测成本**: O(n) 遍历，成本低

---

## 三、核心设计思想

### 2.1 统一的处理流程

两类文本都遵循相同的 4 步流程：

```
┌─────────────────────────────────────────────────┐
│          统一的 4 步校对流程                     │
├─────────────────────────────────────────────────┤
│                                                 │
│  步骤1: 提取关键信息                             │
│    - 从 title, description, author 提取实体     │
│    - 人名、地名、术语、品牌、缩写等               │
│    - 使用 LLM，结构化输出                        │
│    - 结果缓存（按 platform + media_id）          │
│                                                 │
│  步骤1.5: 说话人推断（仅有说话人文本）            │
│    - 提取前 1000 字符对话内容                    │
│    - 结合关键信息 + 元数据                       │
│    - 推断 Speaker1 → 真实姓名                   │
│    - 结果缓存（同关键信息）                      │
│                                                 │
│  步骤2: 分段处理                                │
│    - 无说话人：按句子/行分段                     │
│    - 有说话人：按对话长度分段                    │
│    - 触发阈值：5000 字符                        │
│                                                 │
│  步骤3: 分段校对（含质量验证）                   │
│    - 提供：原始分段 + 关键信息 + (说话人映射)    │
│    - 无说话人：用 CALIBRATE_SYSTEM_PROMPT       │
│    - 有说话人：用 CALIBRATE_WITH_SPEAKER_PROMPT │
│    - 并发处理（ThreadPoolExecutor）             │
│    - **每个分段独立验证（可选）**                │
│      - 启用条件：enable_validation=true         │
│      - 验证方式：LLM 打分（score >= 8.0）        │
│      - 不通过：该分段降级到原文                  │
│      - 通过：保留校对结果                        │
│                                                 │
│  步骤4: 合并结果                                │
│    - 合并所有分段（成功+降级的混合）             │
│    - **不再进行整体验证**                        │
│    - 直接输出最终结果                            │
│                                                 │
└─────────────────────────────────────────────────┘

**配置变更说明**：

`enable_validation` 字段行为变化：
- **v1.0（旧版）**：控制"合并后的整体打分验证"
- **v1.1（新版）**：控制"分段时的独立打分验证"
- **默认值**：`false`（不启用质量验证，直接使用校对结果）
- **推荐设置**：
  - `false`：性能优先，无验证开销
  - `true`：质量优先，每个分段独立打分，不合格降级
```

---

### 2.2 两类文本的差异

| 维度 | 无说话人文本 | 有说话人文本 |
|-----|------------|------------|
| **输入格式** | 纯文本（TXT / 字幕） | JSON（含 speaker 字段） |
| **步骤1** | 关键信息提取 | 关键信息提取 |
| **步骤1.5** | - | ✅ 说话人推断 |
| **步骤2** | 按句子/行分段 | 按对话长度分段 |
| **步骤3** | CALIBRATE_SYSTEM_PROMPT | CALIBRATE_WITH_SPEAKER_PROMPT |
| **步骤3.5** | - | ✅ **每个分段独立验证**（可选） |
| **步骤4** | 合并分段 | 合并分段（不再整体验证） |
| **输出** | 纯文本 | 纯文本 + 结构化 JSON |

---

## 四、新架构设计

### 3.1 模块结构

```
utils/llm/
├── core/                           # 核心基础组件（共享）
│   ├── __init__.py
│   ├── config.py                   # LLMConfig 配置类
│   ├── errors.py                   # 错误分类模块（区分可重试/不可重试错误）
│   ├── llm_client.py               # LLM API 调用封装（含智能重试）
│   ├── key_info_extractor.py      # 关键信息提取器
│   ├── speaker_inferencer.py      # 说话人推断器
│   ├── quality_validator.py       # 质量验证器
│   └── cache_manager.py            # 缓存管理器
│
├── processors/                     # 独立的处理器
│   ├── __init__.py
│   ├── plain_text_processor.py    # 无说话人文本处理器
│   └── speaker_aware_processor.py # 有说话人文本处理器
│
├── segmenters/                     # 分段器
│   ├── __init__.py
│   ├── text_segmenter.py          # 无说话人文本分段器
│   └── dialog_segmenter.py        # 有说话人文本分段器
│
├── prompts/                        # 提示词模板（保留）
│   ├── __init__.py
│   ├── prompts.py
│   └── schemas/
│       ├── key_info.py            # 新增：关键信息 Schema
│       ├── speaker_mapping.py     # 保留
│       ├── calibration.py         # 保留
│       └── validation.py          # 保留
│
├── llm.py                          # 保留：LLM API 基础调用
├── __init__.py
└── coordinator.py                  # 新增：协调器（场景路由）
```

---

### 3.2 核心组件设计

#### 3.2.1 LLMConfig（配置类）

**位置**: `core/config.py`

**职责**: 统一管理 LLM 相关配置，避免重复初始化

**设计**:
```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class LLMConfig:
    """LLM 统一配置类"""

    # API 配置
    api_key: str
    base_url: str

    # 校对模型
    calibrate_model: str
    calibrate_reasoning_effort: Optional[str] = None

    # 总结模型
    summary_model: str
    summary_reasoning_effort: Optional[str] = None

    # 关键信息提取模型
    key_info_model: str = None  # 新增：默认使用 calibrate_model
    key_info_reasoning_effort: Optional[str] = None

    # 说话人推断模型
    speaker_model: str = None  # 新增：默认使用 calibrate_model
    speaker_reasoning_effort: Optional[str] = None

    # 质量验证模型
    validator_model: str = None  # 默认使用 calibrate_model
    validator_reasoning_effort: Optional[str] = None

    # 风险模型配置
    risk_calibrate_model: Optional[str] = None
    risk_calibrate_reasoning_effort: Optional[str] = None
    risk_summary_model: Optional[str] = None
    risk_summary_reasoning_effort: Optional[str] = None
    risk_validator_model: Optional[str] = None
    risk_validator_reasoning_effort: Optional[str] = None

    # 重试配置
    max_retries: int = 3
    retry_delay: int = 5

    # 质量配置
    min_calibrate_ratio: float = 0.80
    min_summary_threshold: int = 500

    # 分段配置
    enable_threshold: int = 5000
    segment_size: int = 2000
    max_segment_size: int = 3000

    # 并发配置
    concurrent_workers: int = 10

    # 结构化校对配置
    min_chunk_length: int = 300
    max_chunk_length: int = 1500
    preferred_chunk_length: int = 800
    max_calibration_retries: int = 2
    calibration_concurrent_limit: int = 3
    enable_validation: bool = True

    # 质量阈值
    overall_score_threshold: float = 8.0
    minimum_single_score: float = 7.0

    # 风控配置
    enable_risk_model_selection: bool = False

    @classmethod
    def from_dict(cls, config_dict: dict) -> "LLMConfig":
        """从配置字典创建 LLMConfig 实例"""
        llm_config = config_dict.get("llm", {})
        segmentation_config = llm_config.get("segmentation", {})
        calibration_config = llm_config.get("structured_calibration", {})
        quality_config = calibration_config.get("quality_threshold", {})

        from . import normalize_reasoning_effort

        return cls(
            # API 配置
            api_key=llm_config["api_key"],
            base_url=llm_config["base_url"],

            # 校对模型
            calibrate_model=llm_config["calibrate_model"],
            calibrate_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("calibrate_reasoning_effort")
            ),

            # 总结模型
            summary_model=llm_config["summary_model"],
            summary_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("summary_reasoning_effort")
            ),

            # 关键信息提取模型（默认使用校对模型）
            key_info_model=llm_config.get("key_info_model", llm_config["calibrate_model"]),
            key_info_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("key_info_reasoning_effort")
            ),

            # 说话人推断模型（默认使用校对模型）
            speaker_model=llm_config.get("speaker_model", llm_config["calibrate_model"]),
            speaker_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("speaker_reasoning_effort")
            ),

            # 质量验证模型
            validator_model=calibration_config.get(
                "validator_model", llm_config["calibrate_model"]
            ),
            validator_reasoning_effort=normalize_reasoning_effort(
                calibration_config.get("validator_reasoning_effort")
            ),

            # 风险模型
            risk_calibrate_model=llm_config.get("risk_calibrate_model"),
            risk_calibrate_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("risk_calibrate_reasoning_effort")
            ),
            risk_summary_model=llm_config.get("risk_summary_model"),
            risk_summary_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("risk_summary_reasoning_effort")
            ),
            risk_validator_model=calibration_config.get("risk_validator_model"),
            risk_validator_reasoning_effort=normalize_reasoning_effort(
                calibration_config.get("risk_validator_reasoning_effort")
            ),

            # 重试配置
            max_retries=llm_config.get("max_retries", 3),
            retry_delay=llm_config.get("retry_delay", 5),

            # 质量配置
            min_calibrate_ratio=llm_config.get("min_calibrate_ratio", 0.80),
            min_summary_threshold=llm_config.get("min_summary_threshold", 500),

            # 分段配置
            enable_threshold=segmentation_config.get("enable_threshold", 5000),
            segment_size=segmentation_config.get("segment_size", 2000),
            max_segment_size=segmentation_config.get("max_segment_size", 3000),
            concurrent_workers=segmentation_config.get("concurrent_workers", 10),

            # 结构化校对配置
            min_chunk_length=calibration_config.get("min_chunk_length", 300),
            max_chunk_length=calibration_config.get("max_chunk_length", 1500),
            preferred_chunk_length=calibration_config.get("preferred_chunk_length", 800),
            max_calibration_retries=calibration_config.get("max_calibration_retries", 2),
            calibration_concurrent_limit=calibration_config.get(
                "calibration_concurrent_limit", 3
            ),
            enable_validation=calibration_config.get("enable_validation", True),

            # 质量阈值
            overall_score_threshold=quality_config.get("overall_score", 8.0),
            minimum_single_score=quality_config.get("minimum_single_score", 7.0),

            # 风控配置
            enable_risk_model_selection=llm_config.get(
                "enable_risk_model_selection", False
            ),
        )

    def select_models_for_task(
        self, has_risk: bool
    ) -> dict:
        """
        根据风险情况选择模型

        Args:
            has_risk: 是否检测到风险

        Returns:
            包含所选模型的字典
        """
        if has_risk and self.enable_risk_model_selection:
            return {
                "calibrate_model": self.risk_calibrate_model or self.calibrate_model,
                "calibrate_reasoning_effort": self.risk_calibrate_reasoning_effort or self.calibrate_reasoning_effort,
                "summary_model": self.risk_summary_model or self.summary_model,
                "summary_reasoning_effort": self.risk_summary_reasoning_effort or self.summary_reasoning_effort,
                "validator_model": self.risk_validator_model or self.validator_model,
                "validator_reasoning_effort": self.risk_validator_reasoning_effort or self.validator_reasoning_effort,
                "has_risk": True,
            }
        else:
            return {
                "calibrate_model": self.calibrate_model,
                "calibrate_reasoning_effort": self.calibrate_reasoning_effort,
                "summary_model": self.summary_model,
                "summary_reasoning_effort": self.summary_reasoning_effort,
                "validator_model": self.validator_model,
                "validator_reasoning_effort": self.validator_reasoning_effort,
                "has_risk": False,
            }
```

**使用方式**:
```python
# 初始化
config = LLMConfig.from_dict(config_dict)

# 访问配置
api_key = config.api_key
calibrate_model = config.calibrate_model

# 选择模型
selected_models = config.select_models_for_task(has_risk=False)
```

---

#### 3.2.2 KeyInfoExtractor（关键信息提取器）

**位置**: `core/key_info_extractor.py`

**职责**: 从视频元数据中提取关键信息（人名、术语、品牌等）

**设计**:
```python
from typing import Dict, List, Optional
from dataclasses import dataclass
from ..logging import setup_logger
from .llm_client import LLMClient
from .cache_manager import CacheManager

logger = setup_logger(__name__)


@dataclass
class KeyInfo:
    """关键信息数据类"""
    names: List[str]           # 人名
    places: List[str]          # 地名
    technical_terms: List[str] # 技术术语
    brands: List[str]          # 品牌/产品
    abbreviations: List[str]   # 缩写
    foreign_terms: List[str]   # 外文术语
    other_entities: List[str]  # 其他实体

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "names": self.names,
            "places": self.places,
            "technical_terms": self.technical_terms,
            "brands": self.brands,
            "abbreviations": self.abbreviations,
            "foreign_terms": self.foreign_terms,
            "other_entities": self.other_entities,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "KeyInfo":
        """从字典创建"""
        return cls(
            names=data.get("names", []),
            places=data.get("places", []),
            technical_terms=data.get("technical_terms", []),
            brands=data.get("brands", []),
            abbreviations=data.get("abbreviations", []),
            foreign_terms=data.get("foreign_terms", []),
            other_entities=data.get("other_entities", []),
        )

    def format_for_prompt(self) -> str:
        """格式化为 prompt 可用的文本"""
        parts = []

        if self.names:
            parts.append(f"人名: {', '.join(self.names)}")
        if self.places:
            parts.append(f"地名: {', '.join(self.places)}")
        if self.technical_terms:
            parts.append(f"技术术语: {', '.join(self.technical_terms)}")
        if self.brands:
            parts.append(f"品牌/产品: {', '.join(self.brands)}")
        if self.abbreviations:
            parts.append(f"缩写: {', '.join(self.abbreviations)}")
        if self.foreign_terms:
            parts.append(f"外文术语: {', '.join(self.foreign_terms)}")
        if self.other_entities:
            parts.append(f"其他: {', '.join(self.other_entities)}")

        return "\n".join(parts) if parts else "无特殊关键信息"


class KeyInfoExtractor:
    """关键信息提取器"""

    def __init__(
        self,
        llm_client: LLMClient,
        cache_manager: Optional[CacheManager] = None,
        model: str = "claude-3-5-sonnet",
        reasoning_effort: Optional[str] = None,
    ):
        """
        初始化关键信息提取器

        Args:
            llm_client: LLM 客户端
            cache_manager: 缓存管理器（可选）
            model: 使用的模型
            reasoning_effort: reasoning effort 参数
        """
        self.llm_client = llm_client
        self.cache_manager = cache_manager
        self.model = model
        self.reasoning_effort = reasoning_effort

    def extract(
        self,
        title: str,
        author: str = "",
        description: str = "",
        platform: str = "",
        media_id: str = "",
    ) -> KeyInfo:
        """
        提取关键信息

        Args:
            title: 视频标题
            author: 作者/频道
            description: 视频描述
            platform: 平台标识（用于缓存）
            media_id: 媒体 ID（用于缓存）

        Returns:
            KeyInfo 对象
        """
        # 尝试从缓存获取
        if self.cache_manager and platform and media_id:
            cached = self.cache_manager.get_key_info(platform, media_id)
            if cached:
                logger.info(f"从缓存获取关键信息: {platform}/{media_id}")
                return KeyInfo.from_dict(cached)

        # LLM 提取
        logger.info(f"使用 LLM 提取关键信息: {title}")

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(title, author, description)
        response_schema = self._get_response_schema()

        try:
            result = self.llm_client.call(
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=response_schema,
                reasoning_effort=self.reasoning_effort,
            )

            # 解析结果
            key_info = KeyInfo.from_dict(result.structured_output)

            # 缓存结果
            if self.cache_manager and platform and media_id:
                self.cache_manager.save_key_info(
                    platform, media_id, key_info.to_dict()
                )
                logger.info(f"关键信息已缓存: {platform}/{media_id}")

            logger.info(
                f"关键信息提取完成: "
                f"人名{len(key_info.names)}个, "
                f"术语{len(key_info.technical_terms)}个, "
                f"品牌{len(key_info.brands)}个"
            )

            return key_info

        except Exception as e:
            logger.error(f"关键信息提取失败: {e}")
            # 返回空的关键信息
            return KeyInfo([], [], [], [], [], [], [])

    def _build_system_prompt(self) -> str:
        """构建系统提示词"""
        return """你是一个专业的信息提取助手。你的任务是从视频元数据中提取关键信息，这些信息将用于后续的语音识别文本校对。

请尽可能全面地提取以下类型的关键信息：

1. **人名**: 视频中提到的人物姓名（主持人、嘉宾、讨论的人物等）
2. **地名**: 国家、城市、地标等
3. **技术术语**: 专业领域的术语、概念等
4. **品牌/产品**: 公司名、产品名等
5. **缩写**: 常见缩写词（如 AI、LLM、API 等）
6. **外文术语**: 保留原文的专业术语（如 fine-tuning、prompt engineering 等）
7. **其他实体**: 其他重要的专有名词

提取时注意：
- 关注容易被语音识别错误拼写的词汇
- 包含中英文混合的术语
- 包含数字、日期等关键信息
- 如果元数据中信息不足，可以基于常识推断相关实体

输出要求：
- 每个类别返回一个字符串列表
- 如果某个类别没有相关信息，返回空列表
- 去重，不要重复列举相同的实体
"""

    def _build_user_prompt(self, title: str, author: str, description: str) -> str:
        """构建用户提示词"""
        parts = []

        if title:
            parts.append(f"**视频标题**: {title}")
        if author:
            parts.append(f"**作者/频道**: {author}")
        if description:
            parts.append(f"**视频描述**: {description}")

        if not parts:
            parts.append("**元数据**: 无")

        return "\n\n".join(parts) + "\n\n请提取上述元数据中的关键信息。"

    def _get_response_schema(self) -> dict:
        """获取响应 Schema"""
        return {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "人名列表"
                },
                "places": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "地名列表"
                },
                "technical_terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "技术术语列表"
                },
                "brands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "品牌/产品名列表"
                },
                "abbreviations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "缩写列表"
                },
                "foreign_terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "外文术语列表"
                },
                "other_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "其他实体列表"
                }
            },
            "required": [
                "names", "places", "technical_terms",
                "brands", "abbreviations", "foreign_terms", "other_entities"
            ]
        }
```

---

#### 3.2.3 SpeakerInferencer（说话人推断器）

**位置**: `core/speaker_inferencer.py`

**职责**: 推断说话人真实姓名（独立功能）

**设计**:
```python
from typing import Dict, List, Optional
from ..logging import setup_logger
from .llm_client import LLMClient
from .cache_manager import CacheManager
from .key_info_extractor import KeyInfo

logger = setup_logger(__name__)


class SpeakerInferencer:
    """说话人推断器"""

    def __init__(
        self,
        llm_client: LLMClient,
        cache_manager: Optional[CacheManager] = None,
        model: str = "claude-3-5-sonnet",
        reasoning_effort: Optional[str] = None,
        sample_length: int = 1000,
    ):
        """
        初始化说话人推断器

        Args:
            llm_client: LLM 客户端
            cache_manager: 缓存管理器（可选）
            model: 使用的模型
            reasoning_effort: reasoning effort 参数
            sample_length: 采样对话的字符长度（默认 1000）
        """
        self.llm_client = llm_client
        self.cache_manager = cache_manager
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.sample_length = sample_length

    def infer(
        self,
        speakers: List[str],
        dialogs: List[Dict[str, str]],
        title: str,
        author: str = "",
        description: str = "",
        key_info: Optional[KeyInfo] = None,
        platform: str = "",
        media_id: str = "",
    ) -> Dict[str, str]:
        """
        推断说话人真实姓名

        Args:
            speakers: 说话人 ID 列表（如 ["Speaker1", "Speaker2"]）
            dialogs: 对话列表（每项包含 speaker, text, start_time）
            title: 视频标题
            author: 作者/频道
            description: 视频描述
            key_info: 关键信息（可选，用于辅助推断）
            platform: 平台标识（用于缓存）
            media_id: 媒体 ID（用于缓存）

        Returns:
            说话人映射字典 {"Speaker1": "张三", "Speaker2": "李四"}
        """
        if not speakers:
            logger.warning("说话人列表为空，跳过推断")
            return {}

        # 尝试从缓存获取
        if self.cache_manager and platform and media_id:
            cached = self.cache_manager.get_speaker_mapping(platform, media_id)
            if cached:
                logger.info(f"从缓存获取说话人映射: {platform}/{media_id}")
                return cached

        # 提取前 N 字符的对话样本
        sample_dialogs = self._extract_sample_dialogs(dialogs, speakers)

        if not sample_dialogs:
            logger.warning("无有效对话样本，无法推断说话人")
            return {speaker: speaker for speaker in speakers}

        # LLM 推断
        logger.info(f"使用 LLM 推断说话人: {speakers}")

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(
            speakers, sample_dialogs, title, author, description, key_info
        )
        response_schema = self._get_response_schema(speakers)

        try:
            result = self.llm_client.call(
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=response_schema,
                reasoning_effort=self.reasoning_effort,
            )

            # 解析结果
            speaker_mapping = result.structured_output.get("speaker_mapping", {})

            # 验证：确保所有 speaker 都有映射
            for speaker in speakers:
                if speaker not in speaker_mapping:
                    speaker_mapping[speaker] = speaker

            # 缓存结果
            if self.cache_manager and platform and media_id:
                self.cache_manager.save_speaker_mapping(
                    platform, media_id, speaker_mapping
                )
                logger.info(f"说话人映射已缓存: {platform}/{media_id}")

            logger.info(f"说话人推断完成: {speaker_mapping}")

            return speaker_mapping

        except Exception as e:
            logger.error(f"说话人推断失败: {e}")
            # 返回原始映射
            return {speaker: speaker for speaker in speakers}

    def _extract_sample_dialogs(
        self, dialogs: List[Dict[str, str]], speakers: List[str]
    ) -> Dict[str, List[str]]:
        """
        提取前 N 字符的对话样本（按说话人分组）

        Args:
            dialogs: 完整对话列表
            speakers: 说话人列表

        Returns:
            {speaker: [text1, text2, ...]}
        """
        sample_by_speaker = {speaker: [] for speaker in speakers}
        total_chars = 0

        for dialog in dialogs:
            if total_chars >= self.sample_length:
                break

            speaker = dialog.get("speaker", "")
            text = dialog.get("text", "").strip()

            if speaker in sample_by_speaker and text:
                sample_by_speaker[speaker].append(text)
                total_chars += len(text)

        # 过滤空列表
        sample_by_speaker = {
            speaker: texts
            for speaker, texts in sample_by_speaker.items()
            if texts
        }

        return sample_by_speaker

    def _build_system_prompt(self) -> str:
        """构建系统提示词"""
        return """你是一个专业的说话人身份推断助手。你的任务是根据视频元数据和对话内容，推断匿名说话人标识（如 Speaker1, Speaker2）的真实姓名或身份。

推断依据：
1. 视频标题、作者、描述中提到的人名
2. 对话中的自我介绍（通常在开头几分钟）
3. 对话内容中透露的身份线索
4. 提供的关键信息中的人名列表

推断原则：
- 优先使用对话中明确的自我介绍
- 如果无法确定，使用视频元数据中的人名
- 如果仍无法推断，保留原始标识（如 Speaker1）
- 可以使用身份描述（如"主持人"、"嘉宾"）
- 确保不同的 Speaker 映射到不同的姓名/身份

输出要求：
- 返回 speaker_mapping 字典
- 键：原始 Speaker ID（如 "Speaker1"）
- 值：推断的姓名或身份（如 "张三" 或 "主持人"）
"""

    def _build_user_prompt(
        self,
        speakers: List[str],
        sample_dialogs: Dict[str, List[str]],
        title: str,
        author: str,
        description: str,
        key_info: Optional[KeyInfo],
    ) -> str:
        """构建用户提示词"""
        parts = []

        # 元数据
        if title:
            parts.append(f"**视频标题**: {title}")
        if author:
            parts.append(f"**作者/频道**: {author}")
        if description:
            parts.append(f"**视频描述**: {description}")

        # 关键信息中的人名
        if key_info and key_info.names:
            parts.append(f"**关键人名**: {', '.join(key_info.names)}")

        parts.append("\n**对话样本**（前 1000 字符左右）:")

        # 对话样本
        for speaker in speakers:
            texts = sample_dialogs.get(speaker, [])
            if texts:
                sample_text = " ".join(texts[:5])  # 最多 5 条
                parts.append(f"\n[{speaker}]:")
                parts.append(f"{sample_text}")

        parts.append("\n请推断每个 Speaker 的真实姓名或身份。")

        return "\n".join(parts)

    def _get_response_schema(self, speakers: List[str]) -> dict:
        """获取响应 Schema"""
        return {
            "type": "object",
            "properties": {
                "speaker_mapping": {
                    "type": "object",
                    "description": "说话人映射字典",
                    "additionalProperties": {"type": "string"}
                }
            },
            "required": ["speaker_mapping"]
        }
```

---

#### 3.2.4 CacheManager（缓存管理器）

**位置**: `core/cache_manager.py`

**职责**: 管理关键信息和说话人映射的缓存

**设计**:
```python
import json
import os
from typing import Dict, Optional
from ..logging import setup_logger

logger = setup_logger(__name__)


class CacheManager:
    """缓存管理器（关键信息和说话人映射）"""

    def __init__(self, cache_dir: str):
        """
        初始化缓存管理器

        Args:
            cache_dir: 缓存目录路径（与现有系统一致）
        """
        self.cache_dir = Path(cache_dir)

    def _get_video_cache_dir(self, platform: str, media_id: str) -> Path:
        """
        获取视频缓存目录（复用现有逻辑）

        目录结构: cache_dir/platform/YYYY/YYYYMM/media_id

        Args:
            platform: 平台名称（如 youtube, bilibili）
            media_id: 媒体 ID

        Returns:
            视频缓存目录路径
        """
        import datetime

        date = datetime.datetime.now()
        year = date.strftime("%Y")
        year_month = date.strftime("%Y%m")

        # 构建路径：cache_dir/platform/YYYY/YYYYMM/media_id
        return self.cache_dir / platform / year / year_month / media_id

    # 关键信息缓存

    def get_key_info(self, platform: str, media_id: str) -> Optional[Dict]:
        """
        获取关键信息缓存

        Args:
            platform: 平台名称
            media_id: 媒体 ID

        Returns:
            关键信息字典，如果不存在则返回 None
        """
        cache_dir = self._get_video_cache_dir(platform, media_id)
        key_info_file = cache_dir / "key_info.json"

        if key_info_file.exists():
            try:
                with open(key_info_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"加载关键信息缓存失败 {key_info_file}: {e}")
                return None
        return None

    def save_key_info(self, platform: str, media_id: str, key_info: Dict):
        """
        保存关键信息缓存

        Args:
            platform: 平台名称
            media_id: 媒体 ID
            key_info: 关键信息字典
        """
        cache_dir = self._get_video_cache_dir(platform, media_id)
        cache_dir.mkdir(parents=True, exist_ok=True)

        key_info_file = cache_dir / "key_info.json"
        try:
            with open(key_info_file, "w", encoding="utf-8") as f:
                json.dump(key_info, f, ensure_ascii=False, indent=2)
            logger.debug(f"关键信息缓存已保存: {key_info_file}")
        except Exception as e:
            logger.error(f"保存关键信息缓存失败 {key_info_file}: {e}")

    # 说话人映射缓存

    def get_speaker_mapping(self, platform: str, media_id: str) -> Optional[Dict]:
        """
        获取说话人映射缓存

        Args:
            platform: 平台名称
            media_id: 媒体 ID

        Returns:
            说话人映射字典，如果不存在则返回 None
        """
        cache_dir = self._get_video_cache_dir(platform, media_id)
        mapping_file = cache_dir / "speaker_mapping.json"

        if mapping_file.exists():
            try:
                with open(mapping_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"加载说话人映射缓存失败 {mapping_file}: {e}")
                return None
        return None

    def save_speaker_mapping(
        self, platform: str, media_id: str, speaker_mapping: Dict
    ):
        """
        保存说话人映射缓存

        Args:
            platform: 平台名称
            media_id: 媒体 ID
            speaker_mapping: 说话人映射字典
        """
        cache_dir = self._get_video_cache_dir(platform, media_id)
        cache_dir.mkdir(parents=True, exist_ok=True)

        mapping_file = cache_dir / "speaker_mapping.json"
        try:
            with open(mapping_file, "w", encoding="utf-8") as f:
                json.dump(speaker_mapping, f, ensure_ascii=False, indent=2)
            logger.debug(f"说话人映射缓存已保存: {mapping_file}")
        except Exception as e:
            logger.error(f"保存说话人映射缓存失败 {mapping_file}: {e}")
```

**缓存策略优化（与现有系统一致）**:

- **缓存目录结构**:
  ```
  cache_dir/
    └── youtube/
        └── 2026/
            └── 202601/
                └── abc123/
                    ├── transcript_funasr.json       # 原有：FunASR 转录
                    ├── llm_calibrated.txt           # 原有：校对文本
                    ├── llm_summary.txt              # 原有：总结文本
                    ├── llm_processed.json           # 原有：结构化数据
                    ├── key_info.json                # 新增：关键信息缓存
                    └── speaker_mapping.json         # 新增：说话人映射缓存
  ```

- **优势**:
  - ✅ **统一管理**: 与现有转录缓存在同一目录
  - ✅ **易于清理**: 删除视频文件夹即清理所有相关数据
  - ✅ **避免冲突**: 每个视频独立文件，无并发冲突
  - ✅ **便于查看**: 直接查看文件夹内容即可了解所有缓存
  - ✅ **长期运行**: 分散存储，不会出现单文件过大问题
  - ✅ **系统一致**: 完全复用现有 `_get_video_cache_dir()` 逻辑

- **缓存时机**:
  - 提取成功后立即写入对应视频目录
  - 下次处理相同视频时直接读取

- **缓存失效**:
  - 手动清理：删除视频缓存目录
  - 自动清理：复用现有 `cleanup_old_cache()` 机制（按时间过期）

---

#### 3.2.5 错误分类模块（Errors）

**位置**: `core/errors.py`

**职责**: 提供 LLM 错误分类功能，区分可重试和不可重试的错误

**设计思路**:

重试机制的关键在于区分错误类型：
- **不可重试错误（FatalError）**: 认证失败、配置错误等，重试无意义
- **可重试错误（RetryableError）**: 超时、服务器错误等，重试可能成功

**错误分类策略**:

| 错误类型 | 示例 | 处理方式 |
|---------|------|---------|
| **认证错误** | 401, Invalid API key | FatalError - 立即失败 |
| **权限错误** | 403, Permission denied | FatalError - 立即失败 |
| **资源不存在** | 404, Model not found | FatalError - 立即失败 |
| **配置错误** | Invalid request, Bad request | FatalError - 立即失败 |
| **超时错误** | Connection timeout, Read timeout | RetryableError - 可重试 |
| **服务器错误** | 502, 503, 504 | RetryableError - 可重试 |
| **速率限制** | 429, Too Many Requests | RetryableError - 可重试 |
| **未知错误** | 其他 | RetryableError - 默认可重试 |

**设计**:

```python
"""错误分类模块"""


class LLMError(Exception):
    """LLM 错误基类"""
    pass


class RetryableError(LLMError):
    """
    可重试错误

    包括：超时、服务器错误、速率限制等
    """
    pass


class FatalError(LLMError):
    """
    不可重试错误

    包括：认证失败、权限拒绝、资源不存在、配置错误等
    """
    pass


def classify_error(error: Exception) -> type:
    """
    将异常分类为可重试或不可重试错误

    Args:
        error: 原始异常对象

    Returns:
        RetryableError 或 FatalError 类型
    """
    error_msg = str(error).lower()

    # 不可重试的错误模式
    fatal_patterns = [
        # 认证相关
        '401', 'unauthorized', 'auth', 'invalid api key',
        # 权限相关
        '403', 'forbidden', 'permission denied',
        # 资源不存在
        '404', 'not found',
        # 配置错误
        'invalid request', 'invalid parameter', 'invalid model',
        'bad request', '400',
    ]

    # 检查是否匹配不可重试模式
    for pattern in fatal_patterns:
        if pattern in error_msg:
            return FatalError

    # 默认为可重试错误
    return RetryableError
```

**使用示例**:

```python
from .errors import classify_error, FatalError, RetryableError

try:
    result = call_llm_api(...)
except Exception as e:
    error_type = classify_error(e)

    if error_type == FatalError:
        logger.error("致命错误，停止重试")
        raise
    else:
        logger.warning("可重试错误，继续重试")
        # 重试逻辑
```

**优势**:
- ✅ 避免无效重试（认证错误等）
- ✅ 减少等待时间（快速失败）
- ✅ 提高系统健壮性
- ✅ 易于扩展（添加新的错误模式）

---

#### 3.2.6 LLMClient（LLM 客户端封装 - 含智能重试）

**位置**: `core/llm_client.py`

**职责**: 统一封装 LLM API 调用，实现智能重试和错误处理

**核心特性**:
1. **错误分类**: 自动识别可重试/不可重试错误
2. **指数退避**: 重试延迟递增（5s → 10s → 20s → 40s → 60s）
3. **快速失败**: 致命错误立即返回，不浪费时间
4. **日志完善**: 详细记录重试过程

**指数退避策略**:

| 重试次数 | 延迟计算 | 实际延迟 |
|---------|---------|---------|
| 第 1 次 | 5 × 2⁰ | 5 秒 |
| 第 2 次 | 5 × 2¹ | 10 秒 |
| 第 3 次 | 5 × 2² | 20 秒 |
| 第 4 次 | 5 × 2³ | 40 秒 |
| 第 5 次 | min(5 × 2⁴, 60) | 60 秒（最大限制） |

**设计**:
```python
import time
from typing import Dict, Optional
from dataclasses import dataclass
from ..logging import setup_logger
from ..llm import call_llm_api, LLMCallError, StructuredResult
from .errors import classify_error, RetryableError, FatalError

logger = setup_logger(__name__)


@dataclass
class LLMResponse:
    """LLM 响应数据类"""
    text: str  # 纯文本响应
    structured_output: Optional[Dict] = None  # 结构化输出（如果有）


class LLMClient:
    """LLM 客户端（含智能重试）"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        max_retries: int = 3,
        retry_delay: int = 5,
    ):
        """
        初始化 LLM 客户端

        Args:
            api_key: API Key
            base_url: API Base URL
            max_retries: 最大重试次数
            retry_delay: 基础重试延迟（秒），实际延迟会指数增长
        """
        self.api_key = api_key
        self.base_url = base_url
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def call(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: Optional[Dict] = None,
        reasoning_effort: Optional[str] = None,
    ) -> LLMResponse:
        """
        调用 LLM API（带智能重试）

        Args:
            model: 模型名称
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            response_schema: 响应 Schema（可选，用于结构化输出）
            reasoning_effort: reasoning effort 参数（可选）

        Returns:
            LLMResponse 对象

        Raises:
            FatalError: 不可重试的错误
            RetryableError: 重试多次后仍失败
        """
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    logger.info(f"LLM API 调用重试: 第 {attempt}/{self.max_retries} 次")

                # 调用底层 API
                result = self._actual_call(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_schema=response_schema,
                    reasoning_effort=reasoning_effort,
                )

                if attempt > 0:
                    logger.info(f"LLM API 调用重试成功")

                return result

            except Exception as e:
                last_error = e

                # 错误分类
                error_type = classify_error(e)

                # 致命错误，直接抛出
                if error_type == FatalError:
                    logger.error(f"LLM API 调用失败（不可重试）: {e}")
                    raise FatalError(f"不可重试的错误: {e}") from e

                # 可重试错误
                if attempt < self.max_retries:
                    # 计算延迟时间（指数退避）
                    delay = self._calculate_delay(attempt)
                    logger.warning(
                        f"LLM API 调用失败（可重试）: {e}, "
                        f"等待 {delay:.1f}s 后重试 ({attempt + 1}/{self.max_retries})"
                    )
                    time.sleep(delay)
                else:
                    # 所有重试都失败
                    logger.error(f"LLM API 调用失败（重试 {self.max_retries} 次后放弃）: {e}")
                    raise RetryableError(
                        f"重试 {self.max_retries} 次后仍失败: {e}"
                    ) from e

    def _calculate_delay(self, attempt: int) -> float:
        """
        计算重试延迟时间（指数退避）

        Args:
            attempt: 当前重试次数（从 0 开始）

        Returns:
            延迟时间（秒）

        Examples:
            假设 retry_delay = 5
            - attempt 0: 5 * 2^0 = 5s
            - attempt 1: 5 * 2^1 = 10s
            - attempt 2: 5 * 2^2 = 20s
            - attempt 3: 5 * 2^3 = 40s
            - attempt 4: min(5 * 2^4, 60) = 60s（最多 60s）
        """
        delay = self.retry_delay * (2 ** attempt)
        # 限制最大延迟为 60 秒
        return min(delay, 60.0)

    def _actual_call(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: Optional[Dict],
        reasoning_effort: Optional[str],
    ) -> LLMResponse:
        """
        实际的 API 调用（不包含重试逻辑）
        """
        try:
            result = call_llm_api(
                model=model,
                prompt=user_prompt,
                api_key=self.api_key,
                base_url=self.base_url,
                response_schema=response_schema,
                system_prompt=system_prompt,
                max_retries=0,  # 底层不重试，由 LLMClient 统一处理
                retry_delay=0,
                reasoning_effort=reasoning_effort,
            )

            # 判断返回类型
            if isinstance(result, StructuredResult):
                return LLMResponse(
                    text=result.text,
                    structured_output=result.structured_output,
                )
            else:
                return LLMResponse(text=result)

        except LLMCallError as e:
            logger.debug(f"LLM API 调用异常: {e}")
            raise
        except Exception as e:
            logger.error(f"未知错误: {e}")
            raise LLMCallError(f"LLM 调用异常: {e}", e)
```

**重试流程示意**:

```
┌─────────────────────────────────────────┐
│     LLMClient.call()                    │
│                                         │
│  第 1 次尝试 ───┬─→ 成功 → 返回结果     │
│                 │                       │
│                 └─→ 失败 ──┬─→ FatalError → 立即抛出
│                            │
│                            └─→ RetryableError
│                                    ↓
│  等待 5 秒（指数退避）              │
│                                    ↓
│  第 2 次尝试 ───┬─→ 成功 → 返回结果     │
│                 │                       │
│                 └─→ 失败 → 等待 10 秒   │
│                                    ↓
│  第 3 次尝试 ───┬─→ 成功 → 返回结果     │
│                 │                       │
│                 └─→ 失败 → 抛出 RetryableError
│                                         │
└─────────────────────────────────────────┘
```

---

### 3.3 处理器设计

#### 3.3.1 PlainTextProcessor（无说话人文本处理器）

**位置**: `processors/plain_text_processor.py`

**职责**: 处理无说话人文本的校对流程

**设计**:
```python
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

from ..logging import setup_logger
from ..core.config import LLMConfig
from ..core.llm_client import LLMClient
from ..core.key_info_extractor import KeyInfoExtractor, KeyInfo
from ..core.quality_validator import QualityValidator
from ..segmenters.text_segmenter import TextSegmenter
from ..prompts.prompts import (
    CALIBRATE_SYSTEM_PROMPT,
    build_calibrate_user_prompt,
)

logger = setup_logger(__name__)


class PlainTextProcessor:
    """无说话人文本处理器"""

    def __init__(
        self,
        config: LLMConfig,
        llm_client: LLMClient,
        key_info_extractor: KeyInfoExtractor,
        quality_validator: QualityValidator,
    ):
        """
        初始化无说话人文本处理器

        Args:
            config: LLM 配置
            llm_client: LLM 客户端
            key_info_extractor: 关键信息提取器
            quality_validator: 质量验证器
        """
        self.config = config
        self.llm_client = llm_client
        self.key_info_extractor = key_info_extractor
        self.quality_validator = quality_validator
        self.segmenter = TextSegmenter(config)

    def process(
        self,
        text: str,
        title: str,
        author: str = "",
        description: str = "",
        platform: str = "",
        media_id: str = "",
        selected_models: Optional[Dict] = None,
    ) -> Dict:
        """
        处理无说话人文本

        Args:
            text: 原始文本
            title: 视频标题
            author: 作者
            description: 描述
            platform: 平台标识
            media_id: 媒体 ID
            selected_models: 选定的模型（可选）

        Returns:
            处理结果字典
        """
        logger.info(f"开始处理无说话人文本: {title}, 长度: {len(text)}")

        # 步骤1: 提取关键信息
        key_info = self.key_info_extractor.extract(
            title=title,
            author=author,
            description=description,
            platform=platform,
            media_id=media_id,
        )

        # 步骤2: 分段
        need_segmentation = len(text) > self.config.enable_threshold

        if need_segmentation:
            segments = self.segmenter.segment(text)
            logger.info(f"文本已分段: {len(segments)} 个段落")
        else:
            segments = [text]
            logger.info("文本长度未超过阈值，不分段")

        # 步骤3: 分段校对
        calibrated_segments = self._calibrate_segments(
            segments=segments,
            key_info=key_info,
            title=title,
            description=description,
            selected_models=selected_models,
        )

        # 合并校对结果
        calibrated_text = "\n\n".join(calibrated_segments)

        # 步骤4: 质量判断（长度检查）
        calibrated_text = self.quality_validator.validate_by_length(
            original=text,
            calibrated=calibrated_text,
            min_ratio=self.config.min_calibrate_ratio,
        )

        logger.info(
            f"无说话人文本处理完成: "
            f"原始长度{len(text)}, 校对后{len(calibrated_text)}"
        )

        return {
            "校对文本": calibrated_text,
            "key_info": key_info.to_dict(),
            "stats": {
                "original_length": len(text),
                "calibrated_length": len(calibrated_text),
                "segment_count": len(segments),
            }
        }

    def _calibrate_segments(
        self,
        segments: List[str],
        key_info: KeyInfo,
        title: str,
        description: str,
        selected_models: Optional[Dict],
    ) -> List[str]:
        """
        校对分段文本（并发处理）

        Args:
            segments: 分段列表
            key_info: 关键信息
            title: 视频标题
            description: 描述
            selected_models: 选定的模型

        Returns:
            校对后的分段列表
        """
        model = selected_models["calibrate_model"] if selected_models else self.config.calibrate_model
        reasoning_effort = selected_models.get("calibrate_reasoning_effort") if selected_models else self.config.calibrate_reasoning_effort

        # 格式化关键信息
        key_info_text = key_info.format_for_prompt()

        calibrated_segments = [None] * len(segments)

        def calibrate_single_segment(index: int, segment: str):
            """校对单个分段"""
            try:
                logger.info(f"校对分段 {index + 1}/{len(segments)}, 长度: {len(segment)}")

                # 构建 prompt
                user_prompt = build_calibrate_user_prompt(
                    transcript=segment,
                    video_title=title,
                    description=description,
                    key_info=key_info_text,  # 新增参数
                    min_ratio=self.config.min_calibrate_ratio,
                )

                # 调用 LLM
                response = self.llm_client.call(
                    model=model,
                    system_prompt=CALIBRATE_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    reasoning_effort=reasoning_effort,
                )

                calibrated_segments[index] = response.text
                logger.info(f"分段 {index + 1} 校对完成")

            except Exception as e:
                logger.error(f"分段 {index + 1} 校对失败: {e}")
                calibrated_segments[index] = segment  # 降级到原文

        # 并发处理
        max_workers = min(len(segments), self.config.concurrent_workers)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(calibrate_single_segment, i, seg)
                for i, seg in enumerate(segments)
            ]

            for future in concurrent.futures.as_completed(futures):
                future.result()  # 等待完成

        return calibrated_segments
```

---

#### 3.3.2 SpeakerAwareProcessor（有说话人文本处理器）

**位置**: `processors/speaker_aware_processor.py`

**职责**: 处理有说话人文本的校对流程

**设计**:
```python
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

from ..logging import setup_logger
from ..core.config import LLMConfig
from ..core.llm_client import LLMClient
from ..core.key_info_extractor import KeyInfoExtractor, KeyInfo
from ..core.speaker_inferencer import SpeakerInferencer
from ..core.quality_validator import QualityValidator
from ..segmenters.dialog_segmenter import DialogSegmenter
from ..prompts.prompts import (
    CALIBRATE_SYSTEM_PROMPT_WITH_SPEAKER,
    build_calibrate_user_prompt,
)

logger = setup_logger(__name__)


class SpeakerAwareProcessor:
    """有说话人文本处理器"""

    def __init__(
        self,
        config: LLMConfig,
        llm_client: LLMClient,
        key_info_extractor: KeyInfoExtractor,
        speaker_inferencer: SpeakerInferencer,
        quality_validator: QualityValidator,
    ):
        """
        初始化有说话人文本处理器

        Args:
            config: LLM 配置
            llm_client: LLM 客户端
            key_info_extractor: 关键信息提取器
            speaker_inferencer: 说话人推断器
            quality_validator: 质量验证器
        """
        self.config = config
        self.llm_client = llm_client
        self.key_info_extractor = key_info_extractor
        self.speaker_inferencer = speaker_inferencer
        self.quality_validator = quality_validator
        self.segmenter = DialogSegmenter(config)

    def process(
        self,
        dialogs: List[Dict],
        title: str,
        author: str = "",
        description: str = "",
        platform: str = "",
        media_id: str = "",
        selected_models: Optional[Dict] = None,
    ) -> Dict:
        """
        处理有说话人文本

        Args:
            dialogs: 对话列表（每项包含 speaker, text, start_time）
            title: 视频标题
            author: 作者
            description: 描述
            platform: 平台标识
            media_id: 媒体 ID
            selected_models: 选定的模型

        Returns:
            处理结果字典
        """
        total_length = sum(len(d.get("text", "")) for d in dialogs)
        logger.info(f"开始处理有说话人文本: {title}, 对话数: {len(dialogs)}, 总长度: {total_length}")

        # 步骤1: 提取关键信息
        key_info = self.key_info_extractor.extract(
            title=title,
            author=author,
            description=description,
            platform=platform,
            media_id=media_id,
        )

        # 步骤1.5: 说话人推断
        speakers = list(set(d.get("speaker", "") for d in dialogs if d.get("speaker")))
        speaker_mapping = self.speaker_inferencer.infer(
            speakers=speakers,
            dialogs=dialogs,
            title=title,
            author=author,
            description=description,
            key_info=key_info,
            platform=platform,
            media_id=media_id,
        )

        # 步骤2: 分段
        chunks = self.segmenter.segment(dialogs)
        logger.info(f"对话已分段: {len(chunks)} 个 chunk")

        # 步骤3: 分段校对
        calibrated_chunks = self._calibrate_chunks(
            chunks=chunks,
            key_info=key_info,
            speaker_mapping=speaker_mapping,
            title=title,
            description=description,
            selected_models=selected_models,
        )

        # 合并校对结果
        calibrated_dialogs = []
        for chunk in calibrated_chunks:
            calibrated_dialogs.extend(chunk)

        # 步骤4: 质量判断
        # 4.1 长度检查（每个分段）
        original_text = self._build_text_from_dialogs(dialogs)
        calibrated_text = self._build_text_from_dialogs(calibrated_dialogs)

        if len(calibrated_text) < len(original_text) * self.config.min_calibrate_ratio:
            logger.warning(f"校对文本长度不足，降级到原文")
            calibrated_dialogs = dialogs
            calibrated_text = original_text

        # 4.2 全量打分（可选）
        if self.config.enable_validation:
            validation_result = self.quality_validator.validate_by_score(
                original=dialogs,
                calibrated=calibrated_dialogs,
                video_metadata={"title": title, "author": author, "description": description},
                selected_models=selected_models,
            )

            if not validation_result["passed"]:
                logger.warning(f"质量验证未通过，降级到原文")
                calibrated_dialogs = dialogs
                calibrated_text = original_text

        logger.info(
            f"有说话人文本处理完成: "
            f"原始长度{len(original_text)}, 校对后{len(calibrated_text)}"
        )

        return {
            "校对文本": calibrated_text,
            "结构化数据": {
                "dialogs": calibrated_dialogs,
                "speaker_mapping": speaker_mapping,
            },
            "key_info": key_info.to_dict(),
            "stats": {
                "original_length": len(original_text),
                "calibrated_length": len(calibrated_text),
                "dialog_count": len(dialogs),
                "chunk_count": len(chunks),
            }
        }

    def _calibrate_chunks(
        self,
        chunks: List[List[Dict]],
        key_info: KeyInfo,
        speaker_mapping: Dict[str, str],
        title: str,
        description: str,
        selected_models: Optional[Dict],
    ) -> List[List[Dict]]:
        """
        校对分块对话（并发处理）

        Args:
            chunks: 分块列表
            key_info: 关键信息
            speaker_mapping: 说话人映射
            title: 视频标题
            description: 描述
            selected_models: 选定的模型

        Returns:
            校对后的分块列表
        """
        model = selected_models["calibrate_model"] if selected_models else self.config.calibrate_model
        reasoning_effort = selected_models.get("calibrate_reasoning_effort") if selected_models else self.config.calibrate_reasoning_effort

        # 格式化关键信息
        key_info_text = key_info.format_for_prompt()

        calibrated_chunks = [None] * len(chunks)

        def calibrate_single_chunk(index: int, chunk: List[Dict]):
            """校对单个 chunk"""
            try:
                chunk_length = sum(len(d.get("text", "")) for d in chunk)
                logger.info(f"校对 chunk {index + 1}/{len(chunks)}, 对话数: {len(chunk)}, 长度: {chunk_length}")

                # 构建 prompt（包含对话结构）
                chunk_text = self._format_chunk_for_prompt(chunk, speaker_mapping)

                user_prompt = build_calibrate_user_prompt(
                    transcript=chunk_text,
                    video_title=title,
                    description=description,
                    key_info=key_info_text,
                    speaker_mapping=speaker_mapping,  # 新增参数
                    min_ratio=self.config.min_calibrate_ratio,
                )

                # 调用 LLM（结构化输出）
                response_schema = self._get_calibration_schema()
                response = self.llm_client.call(
                    model=model,
                    system_prompt=CALIBRATE_SYSTEM_PROMPT_WITH_SPEAKER,
                    user_prompt=user_prompt,
                    response_schema=response_schema,
                    reasoning_effort=reasoning_effort,
                )

                # 解析结构化输出
                calibrated_dialogs = response.structured_output.get("calibrated_dialogs", [])

                # 确保数量一致
                if len(calibrated_dialogs) != len(chunk):
                    logger.warning(f"chunk {index + 1} 校对结果数量不匹配，降级到原文")
                    calibrated_chunks[index] = chunk
                else:
                    calibrated_chunks[index] = calibrated_dialogs

                logger.info(f"chunk {index + 1} 校对完成")

            except Exception as e:
                logger.error(f"chunk {index + 1} 校对失败: {e}")
                calibrated_chunks[index] = chunk  # 降级到原文

        # 并发处理
        max_workers = min(len(chunks), self.config.calibration_concurrent_limit)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(calibrate_single_chunk, i, chunk)
                for i, chunk in enumerate(chunks)
            ]

            for future in concurrent.futures.as_completed(futures):
                future.result()

        return calibrated_chunks

    def _format_chunk_for_prompt(self, chunk: List[Dict], speaker_mapping: Dict[str, str]) -> str:
        """格式化 chunk 为 prompt 可用的文本"""
        lines = []
        for dialog in chunk:
            speaker = dialog.get("speaker", "")
            text = dialog.get("text", "")
            mapped_speaker = speaker_mapping.get(speaker, speaker)
            lines.append(f"[{mapped_speaker}]: {text}")
        return "\n".join(lines)

    def _build_text_from_dialogs(self, dialogs: List[Dict]) -> str:
        """从对话列表构建纯文本"""
        lines = []
        for dialog in dialogs:
            speaker = dialog.get("speaker", "")
            text = dialog.get("text", "")
            lines.append(f"[{speaker}]: {text}")
        return "\n".join(lines)

    def _get_calibration_schema(self) -> dict:
        """获取校对响应 Schema"""
        return {
            "type": "object",
            "properties": {
                "calibrated_dialogs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "speaker": {"type": "string"},
                            "text": {"type": "string"},
                            "start_time": {"type": "string"}
                        },
                        "required": ["speaker", "text"]
                    }
                }
            },
            "required": ["calibrated_dialogs"]
        }
```

---

### 3.4 协调器设计

**位置**: `coordinator.py`

**职责**: 场景路由，决定使用哪个处理器

**设计**:
```python
from typing import Dict, Any
from .logging import setup_logger
from .core.config import LLMConfig
from .core.llm_client import LLMClient
from .core.key_info_extractor import KeyInfoExtractor
from .core.speaker_inferencer import SpeakerInferencer
from .core.quality_validator import QualityValidator
from .core.cache_manager import CacheManager
from .processors.plain_text_processor import PlainTextProcessor
from .processors.speaker_aware_processor import SpeakerAwareProcessor
from ..risk_control.text_sanitizer import TextSanitizer

logger = setup_logger(__name__)


class CalibrationCoordinator:
    """校对协调器（场景路由）"""

    def __init__(self, config_dict: Dict[str, Any]):
        """
        初始化协调器

        Args:
            config_dict: 配置字典
        """
        # 初始化配置
        self.config = LLMConfig.from_dict(config_dict)

        # 初始化基础组件
        self.llm_client = LLMClient(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            max_retries=self.config.max_retries,
            retry_delay=self.config.retry_delay,
        )

        # 初始化缓存管理器
        cache_dir = config_dict.get("cache", {}).get("cache_dir", "./cache")
        self.cache_manager = CacheManager(cache_dir=cache_dir)

        # 初始化关键信息提取器
        self.key_info_extractor = KeyInfoExtractor(
            llm_client=self.llm_client,
            cache_manager=self.cache_manager,
            model=self.config.key_info_model or self.config.calibrate_model,
            reasoning_effort=self.config.key_info_reasoning_effort,
        )

        # 初始化说话人推断器
        self.speaker_inferencer = SpeakerInferencer(
            llm_client=self.llm_client,
            cache_manager=self.cache_manager,
            model=self.config.speaker_model or self.config.calibrate_model,
            reasoning_effort=self.config.speaker_reasoning_effort,
            sample_length=1000,
        )

        # 初始化质量验证器
        self.quality_validator = QualityValidator(
            llm_client=self.llm_client,
            config=self.config,
        )

        # 初始化处理器
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

        # 初始化风控
        self.text_sanitizer = TextSanitizer(config_dict)

    def process(self, llm_task: Dict[str, Any]) -> Dict[str, Any]:
        """
        处理校对任务（场景路由）

        Args:
            llm_task: 任务字典，包含：
                - task_id
                - transcript (纯文本)
                - transcription_data (JSON，可选)
                - use_speaker_recognition
                - video_title
                - author
                - description
                - platform
                - media_id

        Returns:
            处理结果字典
        """
        task_id = llm_task["task_id"]
        use_speaker_recognition = llm_task.get("use_speaker_recognition", False)
        title = llm_task["video_title"]
        author = llm_task["author"]
        description = llm_task.get("description", "")
        platform = llm_task.get("platform", "")
        media_id = llm_task.get("media_id", "")

        logger.info(f"协调器开始处理任务: {task_id}, 说话人识别: {use_speaker_recognition}")

        # 风险检测
        has_risk, _ = self._detect_risk(title, author, description)
        selected_models = self.config.select_models_for_task(has_risk)

        # 场景路由
        if use_speaker_recognition and llm_task.get("transcription_data"):
            # 有说话人文本
            logger.info(f"使用 SpeakerAwareProcessor 处理: {task_id}")

            dialogs = self._extract_dialogs_from_data(llm_task["transcription_data"])

            result = self.speaker_aware_processor.process(
                dialogs=dialogs,
                title=title,
                author=author,
                description=description,
                platform=platform,
                media_id=media_id,
                selected_models=selected_models,
            )
        else:
            # 无说话人文本
            logger.info(f"使用 PlainTextProcessor 处理: {task_id}")

            result = self.plain_text_processor.process(
                text=llm_task["transcript"],
                title=title,
                author=author,
                description=description,
                platform=platform,
                media_id=media_id,
                selected_models=selected_models,
            )

        # 添加模型信息
        result["models_used"] = selected_models

        logger.info(f"协调器处理完成: {task_id}")

        return result

    def _detect_risk(self, title: str, author: str, description: str) -> tuple:
        """检测元数据风险"""
        has_risk = False
        sensitive_words = []

        if self.config.enable_risk_model_selection:
            # 检测标题
            title_clean, title_words = self.text_sanitizer.sanitize(title, "title")
            # 检测作者
            author_clean, author_words = self.text_sanitizer.sanitize(author, "author")
            # 检测描述
            desc_clean, desc_words = self.text_sanitizer.sanitize(description, "general")

            sensitive_words = title_words + author_words + desc_words
            has_risk = len(sensitive_words) > 0

            if has_risk:
                logger.warning(f"检测到风险内容，敏感词: {sensitive_words}")

        return has_risk, sensitive_words

    def _extract_dialogs_from_data(self, transcription_data: Dict) -> List[Dict]:
        """从 transcription_data 提取对话列表"""
        segments = transcription_data.get("segments", [])
        dialogs = []

        for segment in segments:
            dialogs.append({
                "speaker": segment.get("speaker", ""),
                "text": segment.get("text", ""),
                "start_time": segment.get("start_time", ""),
            })

        return dialogs
```

---

## 五、配置文件变更

### 4.1 新增配置项

在 `config/config.jsonc` 中新增：

```jsonc
{
  "llm": {
    // ... 现有配置 ...

    // 新增：关键信息提取模型（可选，默认使用 calibrate_model）
    "key_info_model": "claude-3-5-sonnet",
    "key_info_reasoning_effort": "medium",

    // 新增：说话人推断模型（可选，默认使用 calibrate_model）
    "speaker_model": "claude-3-5-sonnet",
    "speaker_reasoning_effort": "medium"
  }
}
```

---

## 六、Prompt 变更

### 5.1 校对 Prompt 新增参数

**位置**: `prompts/prompts.py`

**修改**:
```python
def build_calibrate_user_prompt(
    transcript: str,
    video_title: str = "",
    author: str = "",
    description: str = "",
    key_info: str = "",  # 新增：关键信息
    speaker_mapping: Optional[Dict[str, str]] = None,  # 新增：说话人映射
    min_ratio: float = 0.95,
    retry_hint: str = ""
) -> str:
    """
    构建校对用户提示词

    Args:
        transcript: 待校对的文本
        video_title: 视频标题
        author: 作者
        description: 描述
        key_info: 关键信息（格式化后的字符串）
        speaker_mapping: 说话人映射（仅有说话人文本）
        min_ratio: 最小长度比例
        retry_hint: 重试提示

    Returns:
        用户提示词
    """
    parts = []

    if video_title:
        parts.append(f"**视频标题**: {video_title}")
    if author:
        parts.append(f"**作者/频道**: {author}")
    if description:
        parts.append(f"**视频描述**: {description}")

    # 新增：关键信息
    if key_info:
        parts.append(f"\n**关键信息（容易拼写错误的专有名词、人名等）**:\n{key_info}")

    # 新增：说话人映射
    if speaker_mapping:
        mapping_text = ", ".join([f"{k} → {v}" for k, v in speaker_mapping.items()])
        parts.append(f"\n**说话人映射**: {mapping_text}")

    parts.append(f"\n**待校对文本**:\n{transcript}")

    parts.append(f"\n请根据上述信息对文本进行校对，确保校对后的文本长度不少于原文的 {int(min_ratio*100)}%。")

    if retry_hint:
        parts.append(f"\n**重试提示**: {retry_hint}")

    return "\n\n".join(parts)
```

---

## 七、迁移路径

### 7.1 渐进式迁移（推荐）

#### 阶段 1：准备阶段（1-2 天）

1. 创建新的目录结构
2. 实现基础组件：
   - `LLMConfig`
   - `LLMClient`
   - `CacheManager`（**注意**：使用视频缓存目录结构，而非独立 JSON 文件）

**验证**：单元测试通过

**关键变更**：
- `CacheManager` 使用 `_get_video_cache_dir()` 方法获取缓存目录
- 缓存文件位置：`cache_dir/platform/YYYY/YYYYMM/media_id/key_info.json`
- 与现有转录缓存（`transcript_funasr.json`, `llm_calibrated.txt` 等）在同一目录

---

#### 阶段 2：核心组件实现（3-5 天）

1. 实现 `KeyInfoExtractor`
2. 实现 `SpeakerInferencer`
3. 实现 `QualityValidator`
4. 修改 `prompts.py`（新增参数）

**验证**：集成测试通过（单独测试各组件）

---

#### 阶段 3：处理器实现（5-7 天）

1. 实现 `PlainTextProcessor`
2. 实现 `SpeakerAwareProcessor`
3. 保留原有代码，新旧并行

**验证**：对比测试（新旧处理器结果一致性）

---

#### 阶段 4：协调器实现（2-3 天）

1. 实现 `CalibrationCoordinator`
2. 修改 API 层调用（从 `EnhancedLLMProcessor` 切换到 `CalibrationCoordinator`）

**验证**：端到端测试

---

#### 阶段 5：清理与优化（2-3 天）

1. 移除旧代码（`llm_enhanced.py`, `llm_segmented.py`, `structured_calibrator.py`）
2. 更新文档
3. 性能优化

**验证**：回归测试 + 性能基准测试

---

### 6.2 兼容性策略

在迁移期间，保持新旧代码并行：

```python
# api/services/transcription.py

from video_transcript_api.utils.llm.coordinator import CalibrationCoordinator
from video_transcript_api.utils.llm.llm_enhanced import EnhancedLLMProcessor

# 配置开关
USE_NEW_CALIBRATION = config.get("llm", {}).get("use_new_calibration", False)

if USE_NEW_CALIBRATION:
    processor = CalibrationCoordinator(config)
else:
    processor = EnhancedLLMProcessor(config)

result = processor.process(llm_task)
```

---

## 八、测试策略

### 7.1 单元测试

```
tests/unit/llm/
├── test_config.py             # LLMConfig 测试
├── test_llm_client.py         # LLMClient 测试
├── test_key_info_extractor.py # KeyInfoExtractor 测试
├── test_speaker_inferencer.py # SpeakerInferencer 测试
├── test_quality_validator.py  # QualityValidator 测试
├── test_cache_manager.py      # CacheManager 测试
└── test_prompts.py            # Prompt 构建测试
```

### 7.2 集成测试

```
tests/integration/llm/
├── test_plain_text_processor.py      # 无说话人文本处理器测试
├── test_speaker_aware_processor.py   # 有说话人文本处理器测试
└── test_coordinator.py               # 协调器测试
```

### 7.3 对比测试

```python
# tests/comparison/test_calibration_comparison.py

def test_plain_text_consistency():
    """对比新旧处理器的结果一致性"""
    # 旧处理器
    old_processor = EnhancedLLMProcessor(config)
    old_result = old_processor.process_llm_task(llm_task)

    # 新处理器
    new_processor = CalibrationCoordinator(config)
    new_result = new_processor.process(llm_task)

    # 对比
    assert len(old_result["校对文本"]) > 0
    assert len(new_result["校对文本"]) > 0
    # 允许一定差异（因为 LLM 输出不完全确定）
    similarity = compute_similarity(old_result["校对文本"], new_result["校对文本"])
    assert similarity > 0.90  # 90% 相似度
```

---

## 九、优势总结

### 8.1 代码质量提升

| 指标 | 重构前 | 重构后 | 改善 |
|------|-------|-------|------|
| 代码重复率 | ~40% | < 10% | ✅ -75% |
| 单个文件行数 | 2100+ 行 | < 500 行 | ✅ -76% |
| 类职责数量 | 7+ 个 | 1-2 个 | ✅ -71% |
| 配置初始化次数 | 3 次 | 1 次 | ✅ -67% |
| 场景判断复杂度 | 4 层嵌套 | 1 层 | ✅ -75% |

---

### 8.2 功能增强

| 功能 | 重构前 | 重构后 |
|------|-------|-------|
| 关键信息提取 | ❌ 无 | ✅ 全面提取（人名、术语等） |
| 关键信息缓存 | ❌ 无 | ✅ 与视频缓存统一管理 |
| 说话人推断 | ✅ 有 | ✅ 独立功能 + 缓存 |
| 两类文本统一 | ❌ 流程不一致 | ✅ 统一 4 步流程 |
| 缓存管理 | ⚠️ 分散存储 | ✅ 统一目录结构 |
| 可扩展性 | ⚠️ 难扩展 | ✅ 易扩展（新增处理器） |

**缓存策略优化详解**:

| 维度 | 原方案（分散 JSON） | 优化方案（统一目录） | 优势 |
|-----|------------------|------------------|------|
| **存储位置** | `cache_dir/key_info_cache.json`<br>`cache_dir/speaker_mapping_cache.json` | `cache_dir/platform/YYYY/YYYYMM/media_id/`<br>`  ├─ key_info.json`<br>`  └─ speaker_mapping.json` | 与现有缓存统一 |
| **查找效率** | 需要解析整个 JSON 文件 | 直接读取对应文件夹 | 快速访问 |
| **并发安全** | 需要文件锁保护 | 独立文件，无冲突 | 天然安全 |
| **清理方式** | 需要单独管理缓存文件 | 删除视频文件夹即清理所有 | 统一管理 |
| **长期运行** | 单文件会持续增长 | 分散存储，按月归档 | 可持续 |
| **系统一致性** | 与现有结构不一致 | 完全复用现有逻辑 | 降低复杂度 |

---

### 8.3 维护性提升

1. **职责清晰**: 每个类只做一件事
2. **易于测试**: 组件独立，可单独测试
3. **易于扩展**: 新增场景只需添加新处理器
4. **代码复用**: 基础组件可被多个处理器共享
5. **配置统一**: 一次配置，多处使用

---

## 十、风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 迁移引入 bug | 中 | 高 | 对比测试 + 回归测试 |
| 性能下降 | 低 | 中 | 性能基准测试 + 优化 |
| 缓存失效 | 低 | 低 | 缓存键设计合理 + 测试 |
| LLM 成本增加 | 中 | 中 | 关键信息提取缓存 + 监控 |
| 开发周期超期 | 中 | 中 | 分阶段迁移 + 并行开发 |

---

## 十一、后续优化方向

### 10.1 性能优化

1. **批量推断**: 多个视频的关键信息提取批量处理
2. **智能缓存**: 基于内容哈希的分段缓存
3. **异步处理**: 使用 asyncio 提升并发性能

### 10.2 功能增强

1. **多语言支持**: 检测语言，使用不同的 prompt
2. **自定义关键信息**: 用户手动补充关键词
3. **质量反馈**: 用户对校对结果打分，优化 prompt

---

## 十二、方案总结

### 12.1 核心变更点

1. **统一处理流程**: 两类文本都遵循"关键信息提取 → 分段 → 校对 → 质量判断"的 4 步流程
2. **关键信息提取**: 使用 LLM 全面提取人名、术语、品牌等，提升校对准确性
3. **缓存策略优化**: 关键信息和说话人映射缓存与视频缓存统一管理
4. **说话人推断独立**: 作为独立功能，可复用并缓存结果
5. **代码重复消除**: 从 40% 降低到 < 10%
6. **配置统一管理**: 使用 `LLMConfig` 数据类避免重复初始化

### 12.2 关键设计决策

| 设计点 | 决策 | 理由 |
|-------|------|------|
| **两类文本实现** | 独立处理器 + 共享基础组件 | 各自优化，互不干扰 |
| **关键信息提取** | 使用 LLM | 准确、灵活，全面提取 |
| **提取结果缓存** | 是（视频缓存目录） | 节省成本，提升性能，统一管理 |
| **说话人推断** | 独立功能 | 职责清晰，可单独调用和测试 |
| **标点密度检测** | 保留（阈值 5/1000） | 有效区分 CapsWriter 和正常文本 |
| **质量判断** | 分段用长度，全量用打分 | 平衡准确性和成本 |

### 12.3 预期收益

**代码质量**:
- 代码重复率：-75%（从 40% 降到 < 10%）
- 单文件行数：-76%（从 2100+ 降到 < 500）
- 类职责数量：-71%（从 7+ 降到 1-2）

**功能增强**:
- ✅ 关键信息提取（新功能）
- ✅ 缓存优化（统一管理）
- ✅ 说话人推断（独立 + 缓存）
- ✅ YouTube 字幕支持明确

**维护性**:
- ✅ 职责清晰，易于测试
- ✅ 易于扩展新场景
- ✅ 配置统一，减少错误

### 12.4 实施建议

**优先级**:
1. **阶段 1-2**（基础组件 + 核心组件）：立即执行，低风险高收益
2. **阶段 3**（处理器实现）：充分测试，对比新旧结果
3. **阶段 4-5**（协调器 + 清理）：逐步切换，保留兼容性

**风险控制**:
- 新旧代码并行，通过配置开关切换
- 对比测试验证一致性（相似度 > 90%）
- 分阶段迁移，每阶段独立验证

---

## 十三、重试机制优化（v1.2 新增）

### 13.1 更新概述

**更新时间**: 2026-01-27

**更新内容**:
1. ✅ 新增错误分类模块（`core/errors.py`）
2. ✅ 升级 LLMClient 重试机制（智能重试 + 指数退避）
3. ✅ 区分可重试/不可重试错误
4. ✅ 避免无效重试，提升系统健壮性

### 13.2 核心改进

#### 13.2.1 错误分类

**问题**: 原方案中所有错误统一重试，导致：
- 认证错误（401）也重试 3 次，浪费 15 秒
- 配置错误无法快速发现

**解决方案**: 引入错误分类机制

| 错误类型 | 处理策略 | 示例 |
|---------|---------|------|
| **FatalError** | 立即失败，不重试 | 401, 403, 404, 配置错误 |
| **RetryableError** | 智能重试 | 超时, 502, 503, 429 |

**收益**:
- ⚡ 快速失败：认证错误从 15s 缩短到 < 1s
- 💰 节省成本：减少无效 API 调用
- 🛡️ 更健壮：配置错误立即暴露

#### 13.2.2 指数退避

**问题**: 原方案使用固定延迟（5 秒），导致：
- 服务器过载时，固定延迟不够
- 无法给服务器足够恢复时间

**解决方案**: 实现指数退避策略

```
第 1 次重试: 等待 5s  (5 × 2⁰)
第 2 次重试: 等待 10s (5 × 2¹)
第 3 次重试: 等待 20s (5 × 2²)
第 4 次重试: 等待 40s (5 × 2³)
最大延迟: 60s
```

**收益**:
- ✅ 提高成功率：给服务器更多恢复时间
- ✅ 避免雪崩：延长重试间隔，减轻服务器压力
- ✅ 自适应：自动适应不同错误场景

### 13.3 实施计划

| 步骤 | 内容 | 预计时间 | 风险 |
|-----|------|---------|------|
| **Step 1** | 创建 `core/errors.py` | 30 分钟 | 低 |
| **Step 2** | 更新 `LLMClient` 实现 | 1 小时 | 低 |
| **Step 3** | 更新 `core/__init__.py` 导出 | 5 分钟 | 低 |
| **Step 4** | 编写单元测试 | 1 小时 | 低 |
| **Step 5** | 集成测试验证 | 30 分钟 | 中 |

**总计**: 约 **3 小时**

### 13.4 测试验证

#### 测试用例 1：致命错误快速失败

```python
def test_fatal_error_no_retry(mocker):
    """测试 401 错误不重试"""
    client = LLMClient(api_key="test", base_url="http://test", max_retries=3)

    mocker.patch.object(
        client,
        '_actual_call',
        side_effect=Exception("401 Unauthorized")
    )

    # 应该立即抛出 FatalError
    with pytest.raises(FatalError):
        client.call(model="test", system_prompt="test", user_prompt="test")

    # 只调用 1 次，没有重试
    assert client._actual_call.call_count == 1
```

#### 测试用例 2：可重试错误指数退避

```python
def test_retryable_error_with_backoff(mocker):
    """测试超时错误使用指数退避"""
    client = LLMClient(api_key="test", base_url="http://test", max_retries=2)

    mocker.patch.object(
        client,
        '_actual_call',
        side_effect=Exception("Connection timeout")
    )

    # 应该重试 2 次后抛出 RetryableError
    with pytest.raises(RetryableError):
        client.call(model="test", system_prompt="test", user_prompt="test")

    # 调用 3 次（初始 1 次 + 重试 2 次）
    assert client._actual_call.call_count == 3
```

#### 测试用例 3：延迟计算

```python
def test_calculate_delay():
    """测试指数退避延迟计算"""
    client = LLMClient(api_key="test", base_url="http://test", retry_delay=5)

    assert client._calculate_delay(0) == 5.0   # 5 * 2^0
    assert client._calculate_delay(1) == 10.0  # 5 * 2^1
    assert client._calculate_delay(2) == 20.0  # 5 * 2^2
    assert client._calculate_delay(3) == 40.0  # 5 * 2^3
    assert client._calculate_delay(4) == 60.0  # min(80, 60)
```

### 13.5 性能对比

#### 场景 1：认证错误（401）

| 指标 | 优化前 | 优化后 | 改善 |
|-----|--------|--------|------|
| 重试次数 | 3 次 | 0 次 | -100% |
| 总耗时 | 15 秒 | < 1 秒 | -93% |
| API 调用 | 4 次 | 1 次 | -75% |

#### 场景 2：超时错误（3 次重试全部超时）

| 指标 | 优化前 | 优化后 | 改善 |
|-----|--------|--------|------|
| 第 1 次延迟 | 5 秒 | 5 秒 | 0% |
| 第 2 次延迟 | 5 秒 | 10 秒 | +100% |
| 第 3 次延迟 | 5 秒 | 20 秒 | +300% |
| 总耗时 | 15 秒 | 35 秒 | +133% |
| **成功率** | **低** | **高** | **✅** |

**说明**: 虽然总耗时增加，但成功率显著提高（给服务器更多恢复时间）

### 13.6 向后兼容

**配置文件**: ✅ 无需修改
- 现有的 `max_retries` 和 `retry_delay` 配置保持不变
- 新逻辑完全兼容现有配置

**API 接口**: ✅ 无破坏性变更
- `LLMClient.call()` 接口保持不变
- 异常类型从 `LLMCallError` 变为 `FatalError/RetryableError`
- 上层代码可以继续捕获 `LLMError` 基类

**迁移成本**: ✅ 零成本
- 不需要修改任何调用代码
- 不需要更新配置文件
- 直接部署即可生效

### 13.7 未来优化方向

本次更新**不包含**以下功能（留待后续迭代）：

| 功能 | 优先级 | 预计收益 | 复杂度 |
|-----|-------|---------|--------|
| **速率限制** | P1 | 防止 API 过载 | 中 |
| **Jitter（抖动）** | P2 | 避免雷鸣羊群效应 | 低 |
| **自适应重试** | P3 | 根据历史成功率调整策略 | 高 |
| **重试指标监控** | P2 | 可观测性 | 中 |

**原因**: 保持改动最小化，优先验证核心逻辑的有效性

---

**评审人**: Claude Sonnet 4.5
**设计时间**: 2026-01-27
**最后更新**: 2026-01-27（v1.2 - 添加智能重试机制）
**文档版本**: v1.2
