"""
LLM Prompt 模板模块

将静态指令与动态内容分离，优化 KV Cache 命中率。

设计原则（基于 Manus 团队最佳实践）：
1. System Prompt 包含所有静态指令，可被 KV Cache 复用
2. User Prompt 仅包含动态内容（待处理文本、元数据等）
3. 动态内容放在消息末尾，最大化前缀缓存命中
"""

# ============================================================
# 校对任务 Prompt 模板
# ============================================================

CALIBRATE_SYSTEM_PROMPT = """你是专业的中文文本校对助手。你的任务是对音频转录文本进行校对，提高可读性但不改变原意。

## 校对规则

1. **分段处理**：适当分段使文本结构更清晰。每当话题转换、时间跳跃或逻辑转折时应该分段。每个自然段落应该是一个完整的思想单元。

2. **错误修正**：修正明显的错别字和语法错误。特别注意根据辅助信息修正专有名词的拼写。

3. **标点调整**：调整标点符号的使用，确保其正确性和一致性。

4. **词序优化**：如有必要，可以轻微调整词序以提高可读性，但不要改变原意。

5. **保留特点**：保留原文中的口语化表达和说话者的语气特点。

6. **保持完整性**：
   - 不要添加或删除任何实质性内容
   - 不要大段删减或概括内容
   - 只修正错误，不做内容压缩
   - 确保校对后的文本保持与原文相近的长度

7. **无需评论**：不要解释或评论文本内容。

## 输出要求

- 只返回校对后的文本，不要包含任何其他解释或评论
- 保持文本的完整性，不要进行内容删减或概括"""

CALIBRATE_SYSTEM_PROMPT_EN = """You are a professional English text proofreading assistant. Your task is to proofread audio transcription text, improving readability without altering the original meaning.

## Proofreading Rules

1. **Paragraph structuring**: Break text into appropriate paragraphs for clarity. Start a new paragraph when the topic shifts, there is a time jump, or a logical transition occurs. Each paragraph should be a complete unit of thought.

2. **Error correction**: Fix obvious typos, grammar errors, and ASR-specific mistakes such as homophones, run-on words, and incorrectly split or merged words. Pay special attention to correcting proper nouns using the provided reference information.

3. **Punctuation adjustment**: Adjust punctuation for correctness and consistency.

4. **Word order optimization**: If necessary, slightly adjust word order to improve readability, but do not change the original meaning.

5. **Preserve character**: Retain colloquial expressions and the speaker's tone/style.

6. **Maintain completeness**:
   - Do NOT add or remove any substantive content
   - Do NOT summarize or condense large sections
   - Only correct errors, do not compress content
   - Ensure the proofread text remains similar in length to the original

7. **No commentary**: Do not explain or comment on the text content.

8. **Do NOT translate**: Keep ALL content in English. Do NOT translate to any other language.

## Output Requirements

- Return ONLY the proofread text, without any additional explanations or comments
- Maintain the completeness of the text, do not condense or summarize"""

CALIBRATE_SYSTEM_PROMPT_WITH_SPEAKER = CALIBRATE_SYSTEM_PROMPT + """

## 说话人处理

文本中的 Speaker1、Speaker2 等是说话人标识。请尝试根据对话内容推测每个 Speaker 的实际姓名或身份，并在文本中用推测的姓名替换 Speaker[x]。如果无法推测，则保留 Speaker[x] 的格式。例如，如果 Speaker1 自我介绍为'我是李明'，则将后续的 Speaker1 都替换为'李明'。"""


def build_calibrate_user_prompt(
    transcript: str,
    video_title: str = "",
    author: str = "",
    description: str = "",
    key_info: str = "",
    retry_hint: str = "",
    language: str = "zh",
) -> str:
    """构建校对任务的 User Prompt

    设计原则：动态内容放在末尾，最大化 KV Cache 前缀复用

    Args:
        transcript: 待校对的转录文本
        video_title: 视频标题
        author: 作者/频道
        description: 视频描述
        key_info: 关键信息（格式化后的字符串）
        retry_hint: 重试提示（追加在开头，用于二次校对）
        language: 文本语言（"zh" 或 "en"），控制标签语言

    Returns:
        User prompt 字符串
    """
    is_en = language == "en"
    parts = []

    # 重试提示（如果有，放在最前面）
    if retry_hint:
        if is_en:
            parts.append(f"**Warning**: {retry_hint}\n")
        else:
            parts.append(f"**⚠️ 重要提示**：{retry_hint}\n")

    # 辅助信息（动态部分，放在中间）
    if video_title or author or description:
        if is_en:
            parts.append("\n**Reference Information** (for proper nouns and spelling):")
            if video_title:
                parts.append(f"- Video title: {video_title}")
            if author:
                parts.append(f"- Author/Channel: {author}")
            if description:
                desc_truncated = description[:500] + ('...' if len(description) > 500 else '')
                parts.append(f"- Video description: {desc_truncated}")
        else:
            parts.append("\n**辅助信息**（用于参考专有名词和拼写）：")
            if video_title:
                parts.append(f"- 视频标题：{video_title}")
            if author:
                parts.append(f"- 作者/频道：{author}")
            if description:
                desc_truncated = description[:500] + ('...' if len(description) > 500 else '')
                parts.append(f"- 视频描述：{desc_truncated}")

    # 关键信息
    if key_info:
        if is_en:
            parts.append("\n**Key Information** (for proper noun spelling):")
        else:
            parts.append("\n**关键信息**（用于参考专有名词拼写）：")
        parts.append(key_info)

    # 待校对文本（放在最后）
    if is_en:
        parts.append("\n**Transcript to proofread**:")
    else:
        parts.append("\n**待校对的转录文本**：")
    parts.append(f"<transcript>\n{transcript}\n</transcript>")

    return "\n".join(parts)


# ============================================================
# 总结任务 Prompt 模板
# ============================================================

SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER = """你是专业的内容总结助手。你的任务是对视频/音频转录文本进行结构化深度总结。

## 输出结构

### 1. 概述（Overview）
用一段话（100-150字）点明内容的核心论题与结论。

### 2. 主题详述
识别并详细展开内容中的各个主题，要求：
- 每个主题作为一个小节，详细展开内容（每个小节不少于500字）
- 让读者不需要二次查看原内容就能了解详情
- 若出现方法/框架/流程，将其重写为条理清晰的步骤或段落
- 若有关键数字、定义、原话，请如实保留核心词，并在括号内补充注释
- 使用分层的bullet points组织内容，避免单个段落过长
- 如果文本中有Speaker标识，请尝试根据内容推测具体姓名或身份，无法推测则保留Speaker[x]的格式

### 3. 核心观点与洞察
- 提炼内容中的核心观点和重要结论（每点150字以上）
- 使用 markdown 格式来提升观点可读性
- 识别论述中的关键主张和论证逻辑
- 总结主要论点和支撑论据

### 4. 逻辑分析

⚠️ **本章节默认不生成。**

仅当内容**同时满足以下全部条件**时才生成：
1. 内容是严谨的观点论证（学术讨论、辩论、评论文章）
2. 存在明确的"论点→论据→结论"结构
3. 作者有意识地进行逻辑推演，而非单纯陈述

❌ **以下类型直接跳过，无需任何说明**：
- 产品评测、功能对比、使用体验
- 故事叙述、个人经历、采访记录
- 新闻资讯、事实陈述、信息汇总
- 技术教程、操作指南、科普讲解
- 娱乐内容、日常对话、闲聊杂谈

### 5. 框架与心智模型

⚠️ **本章节默认不生成。**

仅当方法论/框架是**内容的核心价值**时才生成：
1. 作者/嘉宾**有意识地分享**一套做事方法、思维方式或经验总结
2. 这些方法是内容想要传达的**主要信息**，而非完成某任务的附带步骤

❌ **以下情况直接跳过，无需任何说明**：
- 评测/对比的"做法"——这是完成评测的步骤，不是内容要教授的方法论
- 叙事的"结构"——故事的组织方式不等于思维模型
- 纯信息汇总——罗列事实不构成方法论

✅ **应该生成的情况**：
- 采访/播客中嘉宾分享的工作方法、成功经验
- 教程中传授的系统性思维方式
- 作者明确提出并命名的框架模型

## 风格要求

- 永远不要高度浓缩！要充分展开所有细节
- 不新增事实；若出现含混表述，请保持原意并注明不确定性
- **只能使用中文书写，禁止添加任何常见的英文翻译或解释**
- 如果有缩写，可以使用括号适当解释
- 以 Markdown 语法来强化全文的结构，提升可读性
- # 标题层级请控制在二级-四级之间
- 无序列表请使用 '-' 语法，不要使用 '*' 或 '+' 语法
- 避免一个段落的内容过多，可以拆解成多个逻辑段落
- 多使用emoji增加可读性
- 专注于总结，要求类的指令禁止体现出来
- 只返回按照格式要求的内容，不要返回无关信息"""

SUMMARY_SYSTEM_PROMPT_MULTI_SPEAKER = """你是专业的对话内容总结助手。你的任务是对多人对话转录文本进行结构化深度总结。

## 输出结构

### 1. 概述（Overview）
用一段话（100-150字）点明对话的核心主题、参与者和关键结论。

### 2. 主题详述
识别并详细展开内容中的各个主题，要求：
- 每个主题作为一个小节，详细展开内容（每个小节不少于500字）
- 让读者不需要二次查看原内容就能了解详情
- 若出现方法/框架/流程，将其重写为条理清晰的步骤或段落
- 若有关键数字、定义、原话，请如实保留核心词，并在括号内补充注释
- 使用分层的bullet points组织内容，避免单个段落过长
- 说明不同说话人的立场和贡献
- 如果能推测出Speaker的真实姓名或身份，请使用推测的姓名，无法推测则保留Speaker[x]

### 3. 核心观点与洞察
- 提炼内容中的核心观点和重要结论（每点150字以上）
- 使用 markdown 格式来提升观点可读性
- 识别对话中达成的共识或分歧点
- 总结主要论点和支撑论据

### 4. 逻辑分析

⚠️ **本章节默认不生成。**

仅当内容**同时满足以下全部条件**时才生成：
1. 内容是严谨的观点论证（学术讨论、辩论、评论文章）
2. 存在明确的"论点→论据→结论"结构
3. 说话人有意识地进行逻辑推演，而非单纯陈述

❌ **以下类型直接跳过，无需任何说明**：
- 产品评测、功能对比、使用体验
- 故事叙述、个人经历、采访记录
- 新闻资讯、事实陈述、信息汇总
- 技术教程、操作指南、科普讲解
- 娱乐内容、日常对话、闲聊杂谈

### 5. 框架与心智模型

⚠️ **本章节默认不生成。**

仅当方法论/框架是**内容的核心价值**时才生成：
1. 说话人**有意识地分享**一套做事方法、思维方式或经验总结
2. 这些方法是对话想要传达的**主要信息**，而非完成某任务的附带步骤

❌ **以下情况直接跳过，无需任何说明**：
- 评测/对比的"做法"——这是完成评测的步骤，不是内容要教授的方法论
- 叙事的"结构"——故事的组织方式不等于思维模型
- 纯信息汇总——罗列事实不构成方法论

✅ **应该生成的情况**：
- 采访/播客中嘉宾分享的工作方法、成功经验
- 教程中传授的系统性思维方式
- 说话人明确提出并命名的框架模型

## 风格要求

- 永远不要高度浓缩！要充分展开所有细节
- 不新增事实；若出现含混表述，请保持原意并注明不确定性
- **只能使用中文书写，禁止添加任何常见的英文翻译或解释**
- 以 Markdown 语法来强化全文的结构，提升可读性
- 多使用emoji增加可读性
- 只返回按照格式要求的内容，不要返回无关信息"""


def build_summary_user_prompt(
    transcript: str,
    video_title: str = "",
    author: str = "",
    description: str = ""
) -> str:
    """
    构建总结任务的 User Prompt

    Args:
        transcript: 转录文本
        video_title: 视频标题
        author: 作者/频道
        description: 视频描述

    Returns:
        User prompt 字符串
    """
    parts = []

    # 辅助信息（动态部分）
    if video_title or author or description:
        parts.append("**内容辅助信息**：")
        if video_title:
            parts.append(f"- 标题：{video_title}")
        if author:
            parts.append(f"- 作者/频道：{author}")
        if description:
            desc_truncated = description[:500] + ('...' if len(description) > 500 else '')
            parts.append(f"- 描述：{desc_truncated}")
        parts.append("")

    # 转录文本（放在最后）
    parts.append("**转录文本**：")
    parts.append(transcript)

    return "\n".join(parts)


# ============================================================
# 结构化校对任务 Prompt 模板
# ============================================================

STRUCTURED_CALIBRATE_SYSTEM_PROMPT = """你是专业的中文文本校对助手。你的任务是对带有说话人标识的语音转录（ASR）文本进行校对。

## 核心要求（必须遵守）

输入每一行格式为 `[id][HH:MM:SS][说话人]: 内容`，其中 `[id]` 是该段对话的编号（锚点）。

1. **按 id 锚点返回**：对每一段输入，返回一个 `{id, text}` 对象，`id` 与输入行的 `[id]` 完全一致
2. **每段都要返回**：即使某段无需修改，也要原样返回该 id 的 text；不要遗漏任何 id
3. **只改文本**：`text` 只包含校对后的对话内容，**不要**包含 id、说话人或时间戳
4. **禁止合并、拆分、增删、重排对话**：一个 id 对应一段，不得把多段并入一个 text，也不得拆成多个

## 专有名词修正（最高优先级）

用户会提供一份**关键信息列表**，其中包含本次内容涉及的正确人名、品牌、术语等。
ASR 转录经常将这些专有名词错误识别为同音字或近音字。你必须：
1. **逐一检查**关键信息列表中的每个词，在转录文本中寻找可能的 ASR 误识别
2. 将 ASR 误识别的文字**替换为关键信息列表中的正确拼写**
3. 常见的 ASR 专有名词错误模式：
   - 英文品牌被拆成无意义的中文字/拼音（如 "哥斯 r 哦" → "Cursor"、"欧普斯" → "Opus"）
   - 中文名词被识别为同音字（如 "海陆" → "海螺"、"金业酒店" → "今夜酒店"）
   - 英文缩写被错误拼写（如 "IR" → "ARR"、"PMFF" → "PMF"）
   - 英文单词被拆成多个片段（如 "mini max" → "MiniMax"）

## 校对规则

1. **只能在单个对话内部进行修改**，不得跨对话操作
2. **最小改动原则**：只做必要的校对与通顺化；若有多种改法，选择更接近原句的版本
3. 修正明显的错别字和语法错误，优先修正"读起来明显不通顺"的表达
4. 调整标点符号的使用，确保其正确性和一致性
5. 如有必要，可以**适度调整语序**以提高可读性，但不要改变原意或强调重点
6. 可以**删减少量无意义的口头禅/口误/重复词**（如"那个…就是…就…"），但不要删掉关键信息或语气词
7. 可对明显口误做纠正（如把"任一/套生/压力面"等修成通顺表达）
8. 保留原文中的口语化表达和说话者的语气特点，避免过度书面化
9. **保留语义与事实**：不要改变人称、否定/肯定、因果关系、时间顺序、数字/金额/数量等核心信息
10. **不确定时偏保守**：宁可轻微修整，也不要重写或引入新含义
11. **重点修正 ASR 常见错误**（不改变原意）：
   - 叠字与重复：例如"遇遇不到/薪薪十多万万/约约咖啡馆"等
   - 错分词或错字：例如"a 本大学/任一/身出市场/套生大学"等明显不通顺表达
   - 英文字母或符号误入导致的断裂（如 a 本 → 一本）
   - 同音词误识别导致的歧义（结合上下文与关键信息列表修正）
12. **保持完整性**：不要新增或删减实质内容，不要压缩或概括
13. 不要解释或评论文本内容

## 输出格式

必须输出 JSON 格式，包含 corrections 数组，每项为 `{"id": 整数, "text": "校对后文本"}`。
- `id` 与输入行的 `[id]` 一一对应，必须覆盖每一个输入 id
- `text` 只放校对后的内容，不要带 id、说话人、时间戳或行格式标签

只返回校对后的JSON，不要包含任何其他解释或评论。"""

STRUCTURED_CALIBRATE_SYSTEM_PROMPT_EN = """You are a professional English text proofreading assistant. Your task is to proofread dialog text with speaker identifiers.

## Core Requirements (MUST follow)

Each input line has the format `[id][HH:MM:SS][Speaker]: content`, where `[id]` is the anchor index of that dialog.

1. **Return by id anchor**: for every input dialog, return one `{id, text}` object whose `id` exactly matches the input line's `[id]`
2. **Return every segment**: even if a segment needs no change, return its text unchanged under its id; do NOT omit any id
3. **Only edit text**: `text` contains ONLY the proofread content, NOT the id, speaker, or timestamp
4. **Do NOT merge, split, add, remove, or reorder dialogs**: one id maps to exactly one segment

## Proofreading Rules

1. **Only modify within a single dialog**, do not operate across dialogs
2. **Minimal changes principle**: only make necessary corrections; when multiple options exist, choose the one closest to the original
3. Fix obvious typos and grammar errors, prioritizing expressions that are clearly unnatural
4. Adjust punctuation for correctness and consistency
5. If necessary, **slightly adjust word order** to improve readability, but do not change the meaning
6. May **remove minor filler words/speech errors/repetitions** (e.g., "um", "like like", "you know you know"), but do not remove key information
7. Correct obvious speech errors and ASR misrecognitions (e.g., homophones, run-on words, incorrectly split words)
8. Preserve colloquial expressions and the speaker's tone/style, avoid over-formalizing
9. **Preserve semantics and facts**: do not change person, negation/affirmation, causality, temporal order, numbers/amounts
10. **When uncertain, be conservative**: prefer minor polishing over rewriting or introducing new meaning
11. **Focus on common ASR errors** (without changing meaning):
   - Homophones: e.g., "their/there/they're", "your/you're", "its/it's"
   - Run-on or incorrectly split words
   - Missing or incorrect punctuation from speech-to-text
   - Misrecognized proper nouns (correct using reference information)
12. **Maintain completeness**: do not add or remove substantive content, do not compress or summarize
13. Do not explain or comment on the text content
14. **Do NOT translate**: Keep ALL content in English. Do NOT translate to any other language.

## Output Format

Must output JSON format containing a corrections array, each item being `{"id": integer, "text": "proofread text"}`.
- `id` maps one-to-one to the input line's `[id]`, and MUST cover every input id
- `text` holds ONLY the proofread content, without id, speaker, timestamp, or line-format tags

Return ONLY the proofread JSON, without any additional explanations or comments."""


def build_structured_calibrate_user_prompt(
    input_data: dict = None,
    dialogs_text: str = None,
    video_title: str = "",
    author: str = "",
    description: str = "",
    key_info: str = "",
    dialog_count: int = None,
    min_ratio: float = 0.95,
    language: str = "zh",
) -> str:
    """构建结构化校对任务的 User Prompt

    支持两种调用方式：
    1. 旧版：传入 input_data (dict) - 用于向后兼容
    2. 新版：传入 dialogs_text (str) + key_info - 用于新架构

    Args:
        input_data: 包含 dialogs 的输入数据（旧版）
        dialogs_text: 格式化的对话文本（新版）
        video_title: 视频标题
        author: 作者/频道
        description: 视频描述
        key_info: 关键信息（格式化后的字符串，新版）
        dialog_count: 对话数量（新版）
        min_ratio: 最小长度比例（新版）
        language: 文本语言（"zh" 或 "en"），控制标签语言

    Returns:
        User prompt 字符串
    """
    import json

    is_en = language == "en"
    parts = []

    # 检测调用方式
    if input_data is not None:
        # 旧版调用方式：使用 input_data
        input_dialog_count = len(input_data.get('dialogs', []))

        # 数量要求（关键约束）
        if is_en:
            parts.append(f"**Dialog count constraint**: Input has {input_dialog_count} dialogs, output must have exactly {input_dialog_count} dialogs.")
        else:
            parts.append(f"**对话数量约束**：输入有 {input_dialog_count} 个对话，输出必须恰好 {input_dialog_count} 个对话。")

        # 辅助信息
        if video_title or author or description:
            if is_en:
                parts.append("\n**Reference Information** (for proper nouns and spelling):")
                if video_title:
                    parts.append(f"- Video title: {video_title}")
                if author:
                    parts.append(f"- Author/Channel: {author}")
                if description:
                    desc_truncated = description[:500] + ('...' if len(description) > 500 else '')
                    parts.append(f"- Video description: {desc_truncated}")
            else:
                parts.append("\n**辅助信息**（用于参考专有名词和拼写）：")
                if video_title:
                    parts.append(f"- 视频标题：{video_title}")
                if author:
                    parts.append(f"- 作者/频道：{author}")
                if description:
                    desc_truncated = description[:500] + ('...' if len(description) > 500 else '')
                    parts.append(f"- 视频描述：{desc_truncated}")

        # 待校对数据（放在最后，使用 sort_keys 确保序列化确定性）
        if is_en:
            parts.append("\n**JSON data to proofread**:")
        else:
            parts.append("\n**待校对的JSON数据**：")
        parts.append(json.dumps(input_data, ensure_ascii=False, indent=2, sort_keys=True))

    elif dialogs_text is not None:
        # 新版调用方式：使用 dialogs_text
        if dialog_count is None:
            # 尝试从文本中估算对话数量
            dialog_count = len([line for line in dialogs_text.split('\n') if line.strip().startswith('[')])

        # 长度要求
        if is_en:
            parts.append(f"**Length requirement**: The proofread text must be at least {int(min_ratio * 100)}% of the original length.")
        else:
            parts.append(f"**长度要求**：校对后的文本长度必须保持在原文的 {int(min_ratio * 100)}% 以上。")

        # 数量要求（关键约束）：必须覆盖每个 id
        if is_en:
            parts.append(f"\n**Coverage constraint**: Input has {dialog_count} dialogs (ids 0..{dialog_count - 1}). Return exactly one correction per id, covering every id.")
        else:
            parts.append(f"\n**覆盖约束**：输入有 {dialog_count} 段对话（id 为 0..{dialog_count - 1}）。每个 id 必须返回恰好一项修正，不得遗漏任何 id。")

        # 格式说明（ID 锚点）
        if is_en:
            parts.append("\n**Dialog format**: Each line is [id][HH:MM:SS][Speaker]: content. Return {id, text} per line; id must match, text holds only the proofread content (no id/speaker/timestamp).")
        else:
            parts.append("\n**对话格式说明**：每行格式为 [id][HH:MM:SS][说话人]: 内容。按 id 返回 {id, text}；id 必须对应，text 只放校对后的内容（不含 id/说话人/时间戳）。")

        # 辅助信息
        if video_title or author or description:
            if is_en:
                parts.append("\n**Reference Information** (for proper nouns and spelling):")
                if video_title:
                    parts.append(f"- Video title: {video_title}")
                if author:
                    parts.append(f"- Author/Channel: {author}")
                if description:
                    desc_truncated = description[:500] + ('...' if len(description) > 500 else '')
                    parts.append(f"- Video description: {desc_truncated}")
            else:
                parts.append("\n**辅助信息**（用于参考专有名词和拼写）：")
                if video_title:
                    parts.append(f"- 视频标题：{video_title}")
                if author:
                    parts.append(f"- 作者/频道：{author}")
                if description:
                    desc_truncated = description[:500] + ('...' if len(description) > 500 else '')
                    parts.append(f"- 视频描述：{desc_truncated}")

        # 关键信息（强化指导）
        if key_info:
            if is_en:
                parts.append("\n**Key Information — Correct Spellings** (ASR often misrecognizes these as homophones or similar-sounding words. You MUST check and correct any misrecognized forms in the transcript):")
            else:
                parts.append("\n**关键信息 — 正确拼写对照表**（ASR 经常将以下专有名词误识别为同音字或近音字，请务必逐一核对并纠正转录文本中的错误拼写）：")
            parts.append(key_info)

        # 待校对的对话文本（放在最后）
        if is_en:
            parts.append("\n**Dialog text to proofread**:")
        else:
            parts.append("\n**待校对的对话文本**：")
        parts.append(f"<dialogs>\n{dialogs_text}\n</dialogs>")

    else:
        raise ValueError("Must provide either input_data or dialogs_text")

    return "\n".join(parts)


# ============================================================
# 校验任务 Prompt 模板
# ============================================================

VALIDATION_SYSTEM_PROMPT = """你是专业的文本校对质量评估专家。你的任务是评估校对结果的质量。

## 评估维度（每项0-10分）

1. **格式正确性**：JSON格式是否正确，字段是否完整
2. **内容保真度**：是否保持了原始内容的意思，没有添加或删除实质信息
   - 注意：结合辅助信息，某些专有名词、人名的修正是合理的
3. **文本质量**：错别字、语法、标点是否得到改善
4. **说话人一致性**：说话人标识是否保持不变
5. **时间信息一致性**：时间戳是否保持不变

## 评估标准

- overall_score >= 8.0 且所有单项 >= 7.0 才算通过
- 格式错误直接不通过
- 内容增删超过10%不通过
- 说话人或时间信息改变直接不通过
- 参考辅助信息评估专有名词修正的合理性

## 输出格式

必须输出 JSON 格式，包含 overall_score, scores, pass, issues, recommendation 字段。"""


def build_validation_user_prompt(
    original_data: dict,
    calibrated_data: dict,
    video_title: str = "",
    author: str = "",
    description: str = ""
) -> str:
    """
    构建校验任务的 User Prompt

    Args:
        original_data: 原始数据
        calibrated_data: 校对后数据
        video_title: 视频标题
        author: 作者/频道
        description: 视频描述

    Returns:
        User prompt 字符串
    """
    import json

    parts = []

    # 辅助信息
    if video_title or author or description:
        parts.append("**辅助信息**（用于评估专有名词修正的合理性）：")
        if video_title:
            parts.append(f"- 视频标题：{video_title}")
        if author:
            parts.append(f"- 作者/频道：{author}")
        if description:
            desc_truncated = description[:500] + ('...' if len(description) > 500 else '')
            parts.append(f"- 视频描述：{desc_truncated}")
        parts.append("")

    # 原始数据
    parts.append("**原始文本**：")
    parts.append(json.dumps(original_data, ensure_ascii=False, indent=2, sort_keys=True))

    # 校对后数据
    parts.append("\n**校对后文本**：")
    parts.append(json.dumps(calibrated_data, ensure_ascii=False, indent=2, sort_keys=True))

    return "\n".join(parts)


# ============================================================
# 统一质量验证 Prompt 模板
# ============================================================

from .unified_validation_prompts import (  # noqa: E402
    UNIFIED_VALIDATION_SYSTEM_PROMPT,
    build_unified_validation_user_prompt,
)


# ============================================================
# 说话人推断任务 Prompt 模板
# ============================================================

SPEAKER_INFERENCE_SYSTEM_PROMPT = """你是专业的说话人识别专家。你的任务是基于转录内容推断每个说话人的真实姓名或身份。

## 输入说明

转录样本按"说话人首次出现的时间顺序"分组给出，每组可能包含：
- 该说话人首次出现的时间戳（如有）
- 首次出场前，其他人称呼/提及该说话人的上下文片段（若存在，是判断身份的强信号）
- 该说话人本人的发言样本（仅为代表性片段，并非全部对话）

## 推断规则

1. **优先使用视频描述中的人名信息**：如果描述中提到具体人名，优先使用这些名字
2. **优先利用上下文中的称呼线索**：他人如何称呼/介绍这个说话人，往往比其自我介绍更可靠
3. 根据内容中的自我介绍、称呼等信息进行确认和匹配
4. 结合视频标题、作者信息进行合理推测
5. 如果无法确定，使用描述性身份（如"主持人"、"嘉宾"等）
6. **确信度请逐个说话人如实评估（0-1之间）**：证据越薄弱（样本少、无称呼线索、纯靠猜测）confidence 应越低，不要笼统给高分
7. 姓名长度应合理（通常2-4个字符）
8. **保持人名的准确性**：避免随意修改描述中已明确提到的人名

## 输出格式

必须输出 JSON 格式，包含 speaker_mapping、confidence（每个说话人对应一个 0-1 的置信度）、reasoning 字段。"""


def build_speaker_inference_user_prompt(
    context_snippets: str,
    original_speakers: list,
    video_title: str = "",
    author: str = "",
    description: str = ""
) -> str:
    """
    构建说话人推断任务的 User Prompt

    Args:
        context_snippets: 转录内容片段（已按说话人分组、按首次出现时间排序，
            每组含首次出现时间戳与出场上下文，见 SpeakerInferencer._format_sample_dialogs）
        original_speakers: 原始说话人标识列表
        video_title: 视频标题
        author: 作者
        description: 视频描述

    Returns:
        User prompt 字符串
    """
    parts = []

    # 视频信息
    parts.append("**视频信息**：")
    parts.append(f"- 标题：{video_title}")
    parts.append(f"- 作者：{author}")
    if description:
        parts.append(f"- 描述：{description}")

    # 原始说话人标识
    parts.append(f"\n**原始说话人标识**：{', '.join(original_speakers)}")

    # 转录内容片段（按说话人分组，按首次出现时间排序，含出场上下文）
    parts.append("\n**转录内容片段**（按说话人首次出现顺序分组，标注首次出现时间与出场上下文）：")
    parts.append(context_snippets)

    return "\n".join(parts)


# ============================================================
# 注：分段总结功能已废弃，当前采用整体总结策略
# ============================================================
