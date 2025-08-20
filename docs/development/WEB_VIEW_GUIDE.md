# Web查看功能使用指南

## 功能概述

本系统现已支持为每个转录任务生成永久的web查看链接，用户可以通过链接在浏览器中查看转录结果，无需通过企业微信接收长文本。

## 核心特性

- **永久有效**：查看链接永不过期
- **合并展示**：上半部分显示LLM总结，下半部分显示完整转录
- **Markdown渲染**：支持格式化显示，包含emoji和代码高亮
- **响应式设计**：支持手机和桌面浏览器
- **无需认证**：查看链接可直接访问

## 配置方法

### 1. 修改 config.json

```json
{
  "web": {
    "base_url": "https://your-domain.com",
    "enable_view_links": true
  }
}
```

**配置说明：**
- `base_url`: 外部访问的根域名，用于生成查看链接
- `enable_view_links`: 是否启用查看链接功能

### 2. 生产环境配置示例

```json
{
  "web": {
    "base_url": "https://transcript.your-company.com",
    "enable_view_links": true
  }
}
```

### 3. 开发环境配置示例

```json
{
  "web": {
    "base_url": "http://localhost:8000",
    "enable_view_links": true
  }
}
```

## 工作流程

### 1. 任务提交
用户通过API提交转录任务时，系统自动：
- 生成全局唯一的 `task_id` (格式: `task_xxxxxxxxx`)
- 生成永久的 `view_token` (格式: `view_xxxxxxxxx`)
- 将任务信息保存到数据库

### 2. 转录处理
转录完成后：
- 保存转录结果到缓存系统
- 更新数据库中的任务状态

### 3. LLM处理
LLM处理完成后，系统会依次发送：
1. **校对文本** - 通过企业微信发送
2. **总结文本** - 通过企业微信发送  
3. **查看链接** - 单独发送一条消息，包含完整查看链接

### 4. 用户查看
用户点击链接后看到：
- 页面顶部：视频标题、作者、原始链接
- 上半部分：📝 内容总结（Markdown渲染）
- 下半部分：📄 完整转录文本（Markdown渲染）

## 查看链接格式

```
https://your-domain.com/view/view_xxxxxxxxxxxxxxxxx
```

## 页面状态

系统支持多种页面状态：

### 1. 处理中页面 (`processing.html`)
- 显示转录进度
- 自动刷新功能
- 30秒后自动重新加载

### 2. 正常内容页面 (`transcript.html`)  
- 显示完整的转录内容
- 支持Markdown渲染
- 响应式设计

### 3. 文件清理页面 (`cleaned.html`)
- 提示底层文件已被清理
- 提供原始视频链接
- 引导用户重新提交任务

### 4. 错误页面 (`error.html`)
- 显示错误信息
- 提供解决建议
- 重新加载功能

## 企业微信通知格式

LLM处理完成后，会收到如下格式的通知：

```
🔗 【查看链接】视频标题

点击查看完整转录：https://your-domain.com/view/view_xxxxx
```

## 安全考虑

### 1. 权限隔离
- 查看链接无需认证即可访问
- 仅能查看对应任务的内容
- 无法修改或重新提交任务

### 2. 防枚举攻击
- view_token 使用32字节随机字符串
- 搜索空间极大，难以被暴力破解

### 3. 缓存管理
- 系统会定期清理旧的转录文件
- 查看链接永久有效，但内容可能被清理
- 被清理后会显示相应提示页面

## 测试功能

运行测试脚本验证功能：

```bash
python test_core_features.py
```

测试内容包括：
- 数据库初始化
- 任务创建和UUID生成
- View token生成和查询
- 任务状态管理
- Markdown渲染
- 基础URL配置
- 查看页面数据获取
- 企业微信链接生成

## 故障排查

### 1. 查看链接无法访问
- 检查 config.json 中的 base_url 配置
- 确认API服务器正在运行
- 验证防火墙和端口设置

### 2. 页面显示异常
- 检查模板文件是否完整
- 验证Markdown依赖包是否安装
- 查看服务器日志错误信息

### 3. 企业微信不发送链接
- 确认 config.json 中 enable_view_links 为 true
- 检查LLM处理是否完成
- 验证企业微信webhook配置

## 依赖包

新增的依赖包已添加到 requirements.txt：

```
markdown>=3.4.0
pymdown-extensions>=10.0.0
jinja2>=3.0.0
```

安装命令：
```bash
pip install -r requirements.txt
```

## 技术架构

- **后端**: FastAPI + SQLite + Jinja2
- **前端**: HTML5 + CSS3 (响应式设计)
- **渲染**: 服务端Markdown渲染
- **存储**: 基于现有的智能缓存系统
- **通知**: 集成现有的企业微信系统

## 兼容性

- **浏览器**: 支持所有现代浏览器
- **移动设备**: 响应式设计，支持手机访问
- **文本编码**: UTF-8，支持中文和emoji
- **平台**: Windows/Linux/macOS 跨平台兼容