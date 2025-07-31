# 视频转录API

一个基于Python的视频转录API服务，支持从多个平台（抖音、Bilibili、小红书、YouTube）下载视频并转录为文字。使用CapsWriter-Offline作为转录引擎。

## 功能特点

- 提供API接口，接收视频URL，返回视频的转录文本
- 支持多种平台：抖音、Bilibili、小红书、YouTube、小宇宙播客
- 支持两种获取转录方式：
  - 直接下载平台提供的字幕（如YouTube）
  - 下载视频/音频，使用CapsWriter-Offline客户端发送至服务器进行转录
- 支持并发处理多个转录任务
- 提供企业微信通知功能，实时通知任务状态
- 完善的日志系统，方便问题排查

## 项目结构

```
.
├── api/                      # API服务模块
│   ├── __init__.py
│   └── server.py             # API服务实现
├── downloaders/              # 视频下载模块
│   ├── __init__.py
│   ├── base.py               # 下载器基类
│   ├── douyin.py             # 抖音下载器
│   ├── bilibili.py           # B站下载器
│   ├── xiaohongshu.py        # 小红书下载器
│   ├── youtube.py            # YouTube下载器
│   ├── xiaoyuzhou.py         # 小宇宙播客下载器
│   └── factory.py            # 下载器工厂
├── transcriber/              # 视频转录模块
│   ├── __init__.py
│   ├── transcriber.py        # 转录器实现（基于CapsWriter-Offline）
│   ├── capswriter_client.py  # CapsWriter精简客户端
│   └── funasr_client.py      # FunASR说话人识别客户端
├── utils/                    # 工具模块
│   ├── __init__.py
│   ├── logger.py             # 日志工具
│   └── wechat.py             # 企业微信通知
├── tests/                    # 测试模块
│   ├── __init__.py
│   ├── README.md             # 测试说明文档
│   ├── unit/                 # 单元测试
│   │   ├── __init__.py
│   │   ├── test_downloader.py    # 下载器测试
│   │   └── test_transcriber.py   # 转录器测试
│   ├── integration/          # 集成测试
│   │   ├── __init__.py
│   │   ├── test_url.py           # URL端到端测试
│   │   └── test_api.py           # API集成测试
│   ├── performance/          # 性能测试
│   │   ├── __init__.py
│   │   └── test_concurrent.py    # 并发测试
│   └── manual/               # 手动测试脚本
│       ├── __init__.py
│       ├── test_transcribe.py    # 转录功能测试
│       └── llm_test.py           # LLM功能测试
├── config.json               # 配置文件
├── requirements.txt          # 项目依赖
├── run_tests.py              # 测试运行脚本
├── main.py                   # 主程序入口
└── README.md                 # 项目说明
```

## 安装与配置

### 环境要求

- Python 3.8+
- CapsWriter-Offline 服务器
- 足够的磁盘空间用于存储临时视频文件

### 安装步骤

1. 克隆代码仓库

```bash
git clone <repository-url>
cd 视频转录API
```

2. 创建虚拟环境

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

3. 安装依赖

```bash
pip install -r requirements.txt
```

4. 启动CapsWriter-Offline服务器（需要单独部署）

CapsWriter-Offline服务器需要单独部署，本项目只包含客户端。请参考CapsWriter-Offline项目文档启动服务器。

5. 修改配置文件

编辑`config.json`配置文件，设置相关参数：

```json
{
  "api": {
    "port": 8000,
    "host": "0.0.0.0",
    "auth_token": "your_api_token_here"
  },
  "tikhub": {
    "api_key": "your_tikhub_api_key_here"
  },
  "capswriter": {
    "path": "./CapsWriter-Offline",
    "server_url": "ws://localhost:6006"
  },
  // 其他配置...
}
```

### 配置说明

- `api`: API服务配置
  - `port`: 服务端口
  - `host`: 服务主机地址
  - `auth_token`: API访问令牌
- `tikhub`: TikHub API配置
  - `api_key`: TikHub API密钥
- `capswriter`: CapsWriter-Offline配置
  - `path`: CapsWriter-Offline目录路径
  - `server_url`: CapsWriter-Offline服务器URL
- `concurrent`: 并发配置
  - `max_workers`: 最大并发任务数
  - `queue_size`: 队列大小
- `storage`: 存储配置
  - `temp_dir`: 临时文件目录
  - `output_dir`: 输出文件目录
- `wechat`: 企业微信配置
  - `webhook`: 企业微信webhook地址
- `log`: 日志配置
  - `level`: 日志级别
  - `format`: 日志格式
  - `file`: 日志文件路径
  - `max_size`: 日志文件大小限制
  - `backup_count`: 日志文件备份数量
- `llm`: 大语言模型配置
  - `api_key`: LLM API密钥
  - `base_url`: LLM API基础URL
  - `calibrate_model`: 校对文本使用的模型
  - `summary_model`: 内容总结使用的模型
  - `max_retries`: 最大重试次数（默认2次）
  - `retry_delay`: 重试间隔秒数（默认5秒）

## 使用方法

### 启动API服务

```bash
python main.py --start
```

### API使用示例

#### 请求转录

```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your_api_token_here" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597","use_speaker_recognition": true}'
```

响应:

```json
{
  "code": 202,
  "message": "任务已提交",
  "data": {
    "task_id": "task_1"
  }
}
```

#### 查询任务状态

```bash
curl -X GET "http://localhost:8000/api/task/task_1" \
  -H "Authorization: Bearer your_api_token_here"
```

响应:

```json
{
  "code": 200,
  "message": "转录成功",
  "data": {
    "video_title": "视频标题",
    "author": "视频作者",
    "transcript": "视频的转录文本内容...",
    "srt_path": "./output/youtube_sample_id.srt",
    "lrc_path": "./output/youtube_sample_id.lrc",
    "json_path": "./output/youtube_sample_id.json"
  }
}
```

### 使用测试脚本

#### 测试单个URL

```bash
python tests/integration/test_url.py url "https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597"
```

#### 测试URL列表

```bash
python tests/integration/test_url.py url_list url_list.txt -o results.json
```

#### 测试音频文件

```bash
python tests/manual/test_transcribe.py path/to/audio/file.mp3
```

#### 运行自动化测试

```bash
# 运行单元测试
python -m pytest tests/unit/

# 运行所有测试
python run_tests.py
```

## 关于语音转文字服务集成

本项目支持两种语音转文字服务：

### 1. CapsWriter-Offline
使用精简的客户端（capswriter_client.py）连接到CapsWriter-Offline服务器。服务器默认在`ws://localhost:6006`监听连接，可以通过配置文件修改。

### 2. FunASR Speaker Recognition Server
支持说话人识别功能的转录服务，适用于需要区分多个说话人的场景。通过funasr_client.py连接到FunASR服务器。

转录流程：

1. API接收视频URL请求
2. 下载器从平台获取视频或音频文件
3. 转录器根据配置选择合适的客户端
4. 客户端将文件发送给对应服务器处理
5. 处理结果保存到输出目录
6. 生成所需格式文件并返回转录结果

## 运行测试

```bash
python run_tests.py
```

## 许可证

[License Name]

## 作者

[作者名]
