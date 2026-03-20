# 文档中心

欢迎来到视频转录API项目的文档中心。本文档提供项目各类文档的快速导航和概述。

---

## 📂 目录结构

```
docs/
├── guides/              # 使用指南（面向用户）
│   ├── api/              # API 指南
│   ├── wechat_notification.md  # 企业微信通知使用指南
│   └── multi_user_setup.md    # 多用户系统配置
├── development/         # 开发文档（面向开发者）
│   ├── llm/              # LLM 开发指南
│   │   ├── engineering_guide.md           # LLM 工程指南（核心参考）
│   │   ├── refactoring_plan.md           # 重构方案设计
│   │   ├── refactoring_completed.md     # 重构完成报告
│   │   ├── summary_feature_design.md     # 总结功能设计
│   │   ├── structured_calibration_guide.md # 结构化校对指南
│   │   ├── gemini_openai_compat.md       # Gemini OpenAI 兼容性
│   │   ├── json_output.md                # JSON 输出说明
│   │   └── manus_context_engineering.md  # Manus 上下文工程
│   ├── concurrency.md    # 并发处理架构
│   ├── logging.md        # 日志系统指南
│   ├── risk_control.md   # 风控模块指南
│   ├── web_view.md       # Web 视图指南
│   └── archive/          # 文档归档（已完成的文档）
│       └── README.md     # 归档说明
├── features/            # 功能特性文档
│   └── raw_export.md     # 原始导出功能
└── samples/             # 示例文件
    └── platform_responses/  # 平台响应示例
```

---

## 📖 使用指南

### API 指南

- [API Quick Start](guides/api/quickstart.md)
  - 下游客户端接入指南：认证、提交任务、轮询状态、获取结果
  - 包含 curl 和 Python 完整示例

- [FunASR 客户端 API](guides/api/funasr_spk_server_client_api.md)
  - WebSocket API 接口说明
  - 输出格式（JSON/SRT）
  - 客户端使用示例

- [YouTube 下载 API](guides/api/youtube_client_guide.md)
  - YouTube 下载器配置
  - 参数说明

- [BBDown 指南](guides/api/bbdown_guide.md)
  - Bilibili 视频下载工具
  - 使用方法

### 系统配置

- [企业微信通知](guides/wechat_notification.md)
  - WeComNotifier 使用指南
  - 全局单例最佳实践
  - 频率控制和错误处理
  - 内容审核功能

- [多用户系统](guides/multi_user_setup.md)
  - 多用户鉴权配置
  - 用户管理
  - 使用统计

---

## 🔧 开发文档

### LLM 相关

- [LLM 工程指南](docs/development/llm/engineering_guide.md)
  - 基础架构设计
  - Prompt 工程与 Prefix Cache 优化
  - 结构化输出（JSON）
  - Reasoning Effort 配置
  - 错误处理与可观测性

- [LLM 重构方案](docs/development/archive/refactoring_plan.md)
  - 模块化架构设计方案
  - 统一校对思路
  - 核心组件设计

- [LLM 重构完成报告](docs/development/archive/refactoring_completed.md)
  - 重构实施总结
  - 测试结果
  - 使用示例

- [LLM 总结功能设计](docs/development/archive/summary_feature_design.md)
  - 总结功能恢复设计
  - 架构对比
  - 实施步骤

- [结构化校对指南](docs/development/llm/structured_calibration_guide.md)
  - 结构化校对流程
  - 配置说明
  - 使用示例

- [Manus 上下文工程](docs/development/llm/manus_context_engineering.md)
  - KV 缓存优化
  - 状态机管理
  - 文件系统作为上下文
  - 注意力操控

- [Gemini OpenAI 兼容](docs/development/llm/gemini_openai_compat.md)
  - Gemini API 的 OpenAI 兼容模式
  - Thinking 配置

- [JSON 输出模式](docs/development/llm/json_output.md)
  - JSON Schema 结构化输出
  - 模式选择与配置
  - Self-Correction 重试机制

### 并发处理

- [并发处理架构](development/concurrency.md)
  - 双队列架构设计
  - LLM 并发调度
   - 转录文本处理流水线
   - 性能优化
 
### 其他开发
 
- [日志系统](development/logging.md)
  - Loguru 使用指南
  - 日志配置
  - FunASR 日志说明
 
### 文档归档

- [文档归档目录](development/archive/)
  - 已完成的文档（LLM 重构、架构优化、模块迁移）
  - 归档说明和维护原则

  **重要归档文档**：
  - [LLM 重构方案](development/archive/refactoring_plan.md)
  - [LLM 重构完成报告](development/archive/refactoring_completed.md)
  - [LLM 总结功能设计](development/archive/summary_feature_design.md)
  - [架构优化方案](development/archive/architecture_optimization_plan.md)
  - [模块迁移方案](development/archive/module_migration_plan.md)
  - Loguru 使用指南
  - 日志配置
  - FunASR 日志说明

- [风控模块](development/risk_control.md)
  - 敏感词管理
  - 内容审核策略
  - 配置方法

- [Web 视图](development/web_view.md)
  - 模板渲染
  - 静态资源

---

## ✨ 功能特性

### 功能文档

- [原始导出功能](features/raw_export.md)
  - 原始数据导出
  - 使用场景

---

## 📊 示例文件

### 平台响应示例

`samples/platform_responses/` 目录包含各平台的 API 响应示例：

- Bilibili 响应示例
- Douyin 响应示例
- Xiaohongshu 响应示例
- YouTube 响应示例

这些示例可用于：
- 了解各平台 API 响应格式
- 编写测试用例
- 验证解析逻辑

---

## 🛠️ 快速链接

### 安装与配置
- [项目 README](../README.md) - 项目介绍和快速开始
- [系统架构](architecture.md) - 架构设计、模块详解、处理流程
- [配置示例](../config/config.example.jsonc) - 配置文件模板

### 测试
- [运行测试](../scripts/run_tests.py) - 测试运行脚本
- [单元测试](../tests/unit/) - 单元测试目录
- [集成测试](../tests/integration/) - 集成测试目录

### 代码
- [源代码](../src/) - 项目源代码
- [工具脚本](../scripts/) - 工具和实用脚本

---

## 📝 文档贡献

如果您想为文档做出贡献：

1. 确保文档放在正确的目录中
2. 使用清晰的标题和结构
3. 添加必要的代码示例
4. 更新本索引文件

---

## 🔍 搜索文档

### 快速搜索

使用以下关键词快速定位文档：

| 关键词 | 相关文档 |
|--------|---------|
| LLM | `development/llm/` |
| 并发 | `development/concurrency.md` |
| 日志 | `development/logging.md` |
| 企业微信 | `guides/wechat_notification.md` |
| API | `guides/api/` |
| 平台 | `development/platforms/` |

---

## 📞 获取帮助

如果文档中没有找到您需要的信息：

1. 查看项目的 [README](../README.md)
2. 检查配置示例文件
3. 查看源代码中的注释
4. 提交 Issue 寻求帮助

---

## 📄 许可证

本文档遵循项目的开源协议。详细信息请查看项目根目录的 LICENSE 文件。
