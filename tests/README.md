# 测试说明

本项目的测试已按功能分类组织到不同的子文件夹中，提升项目的可读性和维护性。

## 文件夹结构

### unit/ - 单元测试
测试各个组件的独立功能，使用 unittest 框架。

- `test_downloader.py` - 测试下载器工厂和各平台下载器
- `test_transcriber.py` - 测试转录器功能
- `test_generic_basic.py` - 通用下载器基础测试（网络请求已 mock）

### integration/ - 集成测试
测试组件之间的集成和端到端功能。

- `test_url.py` - 测试URL转录功能的端到端测试
- `test_api.py` - 测试TikHub API响应解析

### performance/ - 性能测试
测试系统的性能和并发能力。

- `test_concurrent.py` - 并发测试：API的并发处理能力

### manual/ - 手动测试脚本
用于开发和调试的手动测试工具，需要真实网络/外部服务，默认不参与
`pytest -q`（已在 pyproject.toml 的 norecursedirs 中排除）。

- `test_transcribe.py` - 测试音视频文件转录功能
- `llm_test.py` - 测试LLM文本校对和总结功能
- `test_generic_url.py` - 通用下载器URL测试（含 input() 交互式提示，且会请求本地 API 服务）
- `test_download_improvement.py` - 下载器改进测试（真实下载 soundhelix.com 大文件）
- `test_summary_e2e_simple.py` - LLM 总结端到端测试（需要真实 LLM API 凭据）
- `test_wechat_notification_flow.py` - 企业微信通知全流程测试（发送真实 webhook 消息）
- `test_debug_webhook_order.py` - webhook 消息顺序调试脚本（无断言，仅供人工核对日志）
- `test_feishu_real.py` - 飞书通知集成测试（发送真实 webhook 消息）
- `test_bilibili_official_api_real.py` - Bilibili 官方 API 元数据抓取测试（真实请求 bilibili.com）

## 运行测试

部署脚本的纯本地 mock 回归（不会连接 Docker daemon、registry 或远程服务器）：

```bash
uv run python -m pytest -q tests/deployment/test_pull_and_deploy.py
```

### 运行单元测试
```bash
# 运行所有单元测试
python -m pytest tests/unit/

# 运行特定的单元测试
python -m pytest tests/unit/test_downloader.py
```

### 运行集成测试
```bash
# 运行所有集成测试
python -m pytest tests/integration/

# 运行URL测试
python tests/integration/test_url.py <video_url>
```

### 运行性能测试
```bash
# 运行并发测试
python tests/performance/test_concurrent.py
```

### 运行手动测试
```bash
# 测试音视频转录
python tests/manual/test_transcribe.py <audio_file_path>

# 测试LLM功能
python tests/manual/llm_test.py <text_file_path>
```

## 注意事项

1. 运行测试前请确保已安装所有依赖项
2. 集成测试和手动测试可能需要外部服务（如CapsWriter服务器）
3. 性能测试可能需要较长时间完成
4. 手动测试脚本通常需要命令行参数
