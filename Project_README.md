# 视频转录API

一个基于Python的视频转录API服务，支持从多个平台（抖音、Bilibili、小红书、YouTube）下载视频并转录为文字。使用CapsWriter-Offline作为转录引擎。

## 功能特点

- 提供API接口，接收视频URL，返回视频的转录文本
- 支持多种平台：抖音、Bilibili、小红书、YouTube
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
│   └── factory.py            # 下载器工厂
├── transcriber/              # 视频转录模块
│   ├── __init__.py
│   ├── transcriber.py        # 转录器实现（基于CapsWriter-Offline）
│   └── srt_converter.py      # 字幕格式转换器
├── utils/                    # 工具模块
│   ├── __init__.py
│   ├── logger.py             # 日志工具
│   └── wechat.py             # 企业微信通知
├── scripts/                  # 脚本工具
│   ├── __init__.py
│   └── test_url.py           # URL测试脚本
├── tests/                    # 测试模块
│   ├── __init__.py
│   ├── test_downloader.py    # 下载器测试
│   └── test_transcriber.py   # 转录器测试
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

4. 克隆或下载CapsWriter-Offline

```bash
git clone https://github.com/path/to/CapsWriter-Offline.git
```

5. 启动CapsWriter-Offline服务器

```bash
cd CapsWriter-Offline
python start_server.py
```

6. 修改配置文件

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
  -d '{"url":"https://www.youtube.com/watch?v=sample_id"}'
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
python scripts/test_url.py url "https://www.youtube.com/watch?v=sample_id"
```

#### 测试URL列表

```bash
python scripts/test_url.py url_list url_list.txt -o results.json
```

#### 测试音频文件

```bash
python scripts/test_url.py audio path/to/audio/file.mp3
```

## 关于CapsWriter-Offline集成

本项目使用CapsWriter-Offline作为转录引擎，而不是直接使用Whisper等模型。CapsWriter-Offline是一个客户端-服务器架构的转录工具，本项目使用其客户端功能将文件发送到CapsWriter-Offline服务器进行转录。

使用前必须先确保CapsWriter-Offline服务器已经启动并正常运行。服务器默认在`ws://localhost:6006`监听连接，可以通过配置文件修改。

转录流程：

1. API接收视频URL请求
2. 下载器从平台获取视频或音频文件
3. 转录器调用CapsWriter-Offline客户端功能
4. CapsWriter-Offline客户端将文件发送给服务器处理
5. 处理结果（SRT、JSON等）保存到输出目录
6. 生成LRC格式文件并返回转录结果

## 运行测试

```bash
python run_tests.py
```

## 许可证

[License Name]

## 作者

[作者名]
