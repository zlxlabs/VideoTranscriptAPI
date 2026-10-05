# LLM 功能测试

## 测试说明

本目录包含 LLM 相关功能的测试脚本。

## 测试文件

### test_summary_prompt_improvement.py

测试改进后的总结 prompt，验证是否成功避免了英文解释中文的问题。

**测试目标：**
- 验证生成的总结文本中不包含英文括号注释（如 "宗庆后(Zong Qinghou)"）
- 确保专有名词直接使用中文，无英文翻译

**使用方法：**
```bash
# 在项目根目录下运行
python tests/llm/test_summary_prompt_improvement.py
```

**输出：**
- `output/test_calibrated.txt`: 校对后的文本
- `output/test_summary.txt`: 生成的总结文本
- 控制台输出：英文注释统计和验证结果

**测试数据：**
使用 `data/cache/youtube/2025/202510/g5Q8NK5fXSE/llm_calibrated.txt` 的前 3000 字符

## Prompt 改进说明

### 修改前的问题

原始 prompt 包含以下指令：
- "专有名词保留原文，必要时括号内给出解释"
- "专有名词保留原文，并在括号给出中文释义（若能直译）"

这导致 LLM 错误理解为"给中文专有名词添加英文解释"，产生大量类似：
- 宗庆后(Zong Qinghou)
- 娃哈哈模式(new Yuan Shikai model)
- 枢密使(Privy Councilor)

### 修改后的改进

新 prompt 明确要求：
- **仅使用中文书写，禁止添加任何英文翻译或解释**
- 专有名词直接使用中文，不要在括号内添加英文原文或音译

修改位置（新架构）：
- `src/video_transcript_api/utils/llm/prompts/__init__.py` 中的 Summary Prompt 模板

## 预期结果

运行测试后，应该看到：
- ✅ "SUCCESS: No English annotations found in summary!"
- 或者英文注释数量显著减少（接近零）
- 生成的总结文本纯中文，阅读流畅，无英文干扰

## 注意事项

1. 需要正确配置 `config/config.jsonc` 中的 LLM API 密钥
2. 测试会调用真实的 LLM API，会产生费用
3. 由于使用前 3000 字符测试，总结质量可能受影响，但足以验证 prompt 改进效果
