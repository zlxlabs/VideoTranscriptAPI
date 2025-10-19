# 企业微信通知器使用指南

## 📌 重要提示

> **⚠️ 使用前必读**
>
> - ✅ **推荐**：全局使用单个 `WeComNotifier` 实例
> - ❌ **避免**：频繁创建多个实例（会导致频控失效、资源浪费）
> - 📖 详见下方"最佳实践"章节

## 🎯 快速开始

### 安装

```bash
# 在你的项目中安装（开发模式）
pip install -e D:\MyFolders\Developments\0Python\251017_WecomRobotPython
```

### 最简单的例子

```python
from wecom_notifier import WeComNotifier

# 1. 初始化
notifier = WeComNotifier()

# 2. 发送消息
result = notifier.send_text(
    webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR-KEY",
    content="Hello 企业微信！"
)

# 3. 检查结果
if result.is_success():
    print("发送成功！")
else:
    print(f"发送失败: {result.error}")
```

## 📚 功能详解

### 1. 文本消息

#### 基础文本
```python
notifier.send_text(
    webhook_url=WEBHOOK_URL,
    content="这是一条普通消息"
)
```

#### 带@all
```python
notifier.send_text(
    webhook_url=WEBHOOK_URL,
    content="紧急通知！",
    mentioned_list=["@all"]  # @所有人
)
```

#### @特定用户
```python
notifier.send_text(
    webhook_url=WEBHOOK_URL,
    content="任务分配通知",
    mentioned_list=["user1", "user2"],  # 按用户ID
    mentioned_mobile_list=["13800138000"]  # 按手机号
)
```

### 2. Markdown消息

#### 基础Markdown
```python
markdown_content = """
# 项目上线通知

## 更新内容
- **新功能**: 用户导出
- **优化**: 性能提升50%

## 测试结果
| 测试项 | 结果 |
|--------|------|
| 单元测试 | 通过 |
| 集成测试 | 通过 |

[查看详情](https://example.com)
"""

notifier.send_markdown(
    webhook_url=WEBHOOK_URL,
    content=markdown_content
)
```

#### Markdown + @all
```python
notifier.send_markdown(
    webhook_url=WEBHOOK_URL,
    content="# 重要通知\n\n服务器将在30分钟后维护",
    mention_all=True  # 会额外发送一条@all的text消息
)
```

### 3. 图片消息

#### 通过文件路径
```python
notifier.send_image(
    webhook_url=WEBHOOK_URL,
    image_path="report.png"
)
```

#### 通过Base64
```python
notifier.send_image(
    webhook_url=WEBHOOK_URL,
    image_base64="iVBORw0KGgoAAAANS...",  # base64字符串
    mention_all=True
)
```

### 4. 同步vs异步

#### 异步发送（默认，推荐）
```python
# 立即返回，不等待发送完成
result = notifier.send_text(
    webhook_url=WEBHOOK_URL,
    content="异步消息",
    async_send=True  # 默认值
)

# 可以选择等待
result.wait(timeout=30)  # 最多等30秒
if result.is_success():
    print("发送成功")
```

#### 同步发送
```python
# 阻塞等待发送完成
result = notifier.send_text(
    webhook_url=WEBHOOK_URL,
    content="同步消息",
    async_send=False
)

# 立即知道结果
if result.is_success():
    print("确认发送成功")
else:
    print(f"发送失败: {result.error}")
```

### 5. 长文本自动分段

```python
# 超过4096字节会自动分段
long_text = "\n".join([f"第{i}行" for i in range(1000)])

result = notifier.send_text(
    webhook_url=WEBHOOK_URL,
    content=long_text
)

# 会自动分成多条消息发送
# 每条消息会带有"（续上页）"或"（未完待续）"标记
```

### 6. 表格智能分段

```python
# 超长表格会保留表头分段
table = """
| ID | 名称 | 数据 |
|----|------|------|
""" + "\n".join([f"| {i} | Item{i} | Data{i} |" for i in range(200)])

notifier.send_markdown(
    webhook_url=WEBHOOK_URL,
    content=table
)

# 每个分段都会保留表头
# 自动添加续页提示
```

### 7. 并发发送

```python
# 异步发送多条消息
results = []

for i in range(10):
    result = notifier.send_text(
        webhook_url=WEBHOOK_URL,
        content=f"消息 {i}",
        async_send=True
    )
    results.append(result)

# 等待所有完成
for result in results:
    result.wait()
    print(f"状态: {result.is_success()}")
```

### 8. 多Webhook管理

```python
# 同一个notifier实例可以管理多个webhook
webhooks = {
    "开发群": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=DEV-KEY",
    "测试群": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TEST-KEY",
    "生产群": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=PROD-KEY",
}

# 每个webhook自动独立管理频率限制
for name, url in webhooks.items():
    notifier.send_text(
        webhook_url=url,
        content=f"发送到{name}"
    )
```

### 9. 自定义配置

```python
notifier = WeComNotifier(
    max_retries=5,         # HTTP请求最大重试次数（默认3）
    retry_delay=3.0,       # 重试延迟秒数（默认2.0）
    log_level="DEBUG"      # 日志级别：DEBUG/INFO/WARNING/ERROR
)
```

## 🔍 常见场景

### 场景1：定时任务通知

```python
def send_task_notification(task_name, status, details):
    """发送任务通知"""
    notifier = WeComNotifier()

    content = f"""# 定时任务通知

**任务名称**: {task_name}
**执行状态**: {status}

## 详细信息
{details}
"""

    result = notifier.send_markdown(
        webhook_url=WEBHOOK_URL,
        content=content,
        mention_all=(status == "失败")  # 失败时@all
    )

    return result.is_success()

# 使用
send_task_notification("数据同步", "成功", "同步了1000条记录")
```

### 场景2：异常告警

```python
def send_error_alert(error_msg, traceback_str):
    """发送错误告警"""
    notifier = WeComNotifier()

    # 第一条：简要告警（@all）
    notifier.send_text(
        webhook_url=WEBHOOK_URL,
        content=f"❌ 系统异常：{error_msg}",
        mentioned_list=["@all"]
    )

    # 第二条：详细堆栈
    notifier.send_text(
        webhook_url=WEBHOOK_URL,
        content=f"详细堆栈:\n{traceback_str}"
    )

# 使用
try:
    # 你的代码
    risky_operation()
except Exception as e:
    import traceback
    send_error_alert(str(e), traceback.format_exc())
```

### 场景3：数据报表

```python
def send_daily_report(data):
    """发送每日数据报表"""
    notifier = WeComNotifier()

    # 生成表格
    table = f"""# 每日数据报表

| 指标 | 今日 | 昨日 | 增长率 |
|------|------|------|--------|
| 用户数 | {data['users_today']} | {data['users_yesterday']} | {data['user_growth']}% |
| 订单数 | {data['orders_today']} | {data['orders_yesterday']} | {data['order_growth']}% |
| 销售额 | ¥{data['revenue_today']} | ¥{data['revenue_yesterday']} | {data['revenue_growth']}% |

生成时间: {data['timestamp']}
"""

    notifier.send_markdown(
        webhook_url=WEBHOOK_URL,
        content=table
    )
```

### 场景4：批量通知（带频率控制）

```python
def send_batch_notifications(user_list):
    """批量发送通知（自动频率控制）"""
    notifier = WeComNotifier()

    for user in user_list:
        # 不用担心超频，会自动限速
        notifier.send_text(
            webhook_url=WEBHOOK_URL,
            content=f"Hi {user['name']}，你的任务已分配",
            mentioned_list=[user['userid']],
            async_send=True  # 异步，不阻塞
        )

    print(f"已提交{len(user_list)}条通知到队列")
```

## 💡 最佳实践

### ✅ 推荐：使用单例模式

**为什么需要单例？**

每个 `WeComNotifier` 实例会为每个 webhook 创建独立的：
- 工作线程（处理消息队列）
- 频率控制器（20条/分钟）

如果创建多个实例，它们无法协调频率限制，容易触发服务端频控。

**正确做法：全局单例**

```python
# config.py 或应用初始化文件
from wecom_notifier import WeComNotifier

# 创建全局实例
NOTIFIER = WeComNotifier(
    max_retries=5,
    log_level="INFO"
)

# 如果有多个 webhook，也只需一个实例
WEBHOOKS = {
    "dev": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=DEV-KEY",
    "prod": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=PROD-KEY"
}
```

```python
# 在其他模块中使用
from config import NOTIFIER, WEBHOOKS

def send_notification(message):
    """发送通知到开发群"""
    NOTIFIER.send_text(
        webhook_url=WEBHOOKS["dev"],
        content=message
    )

def send_alert(message):
    """发送告警到生产群"""
    NOTIFIER.send_text(
        webhook_url=WEBHOOKS["prod"],
        content=message,
        mentioned_list=["@all"]
    )
```

**优点**：
- ✅ 单个实例管理所有 webhook，资源高效
- ✅ 每个 webhook 独立的队列和频控，互不影响
- ✅ 避免多实例竞争导致的频控问题

### ❌ 错误：频繁创建实例

**错误示例1：每次调用都创建**
```python
# ❌ 不要这样做
def send_message(msg):
    notifier = WeComNotifier()  # 每次都创建新实例！
    notifier.send_text(WEBHOOK_URL, msg)
    # 实例销毁，线程也会停止
```

**问题**：
- 每次调用创建新线程，浪费资源
- 实例销毁时线程也停止，可能丢失未发送的消息
- 频控器无法累积，无法有效限速

**错误示例2：多个实例发送同一个 webhook**
```python
# ❌ 不要这样做
notifier1 = WeComNotifier()
notifier2 = WeComNotifier()

# 两个实例发送到同一个 webhook
notifier1.send_text(WEBHOOK_URL, "消息1")  # 线程1处理
notifier2.send_text(WEBHOOK_URL, "消息2")  # 线程2处理
```

**问题**：
- 两个独立的工作线程并发发送，无法保证顺序
- 两个独立的频控器，可能同时发送超过20条/分钟
- 触发服务端频控（45009错误）

### 🔄 使用上下文管理器（可选）

如果只是临时使用，可以添加上下文管理器：

```python
class WeComNotifierContext:
    """上下文管理器包装"""
    def __init__(self, **kwargs):
        self.notifier = WeComNotifier(**kwargs)

    def __enter__(self):
        return self.notifier

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.notifier.stop_all()
        return False

# 使用
with WeComNotifierContext() as notifier:
    notifier.send_text(WEBHOOK_URL, "消息1")
    notifier.send_text(WEBHOOK_URL, "消息2")
# 退出时自动清理资源
```

### 🧵 线程生命周期说明

**工作线程何时启动？**
```python
notifier = WeComNotifier()  # 此时还没有线程

# 第一次发送到某个 webhook 时，创建并启动工作线程
notifier.send_text(WEBHOOK_URL_A, "消息")  # 为 WEBHOOK_URL_A 创建线程

# 第一次发送到另一个 webhook 时，创建另一个工作线程
notifier.send_text(WEBHOOK_URL_B, "消息")  # 为 WEBHOOK_URL_B 创建线程

# 同一个 webhook 的后续消息，复用已有线程
notifier.send_text(WEBHOOK_URL_A, "消息2")  # 复用 WEBHOOK_URL_A 的线程
```

**工作线程何时停止？**
- 显式调用 `notifier.stop_all()`
- `WeComNotifier` 实例被垃圾回收（`__del__`）
- 主程序退出（daemon 线程自动终止）

**关键点**：
- 每个 webhook 只创建一次工作线程
- 线程会持续运行，处理消息队列
- 标记为 daemon，不会阻止程序退出

### 📊 多实例问题示例

**问题演示**：
```python
import threading

# 创建3个实例
notifier1 = WeComNotifier()
notifier2 = WeComNotifier()
notifier3 = WeComNotifier()

# 查看线程数
print(f"初始线程数: {threading.active_count()}")

# 都向同一个 webhook 发送
notifier1.send_text(WEBHOOK_URL, "消息1")  # 创建线程1
notifier2.send_text(WEBHOOK_URL, "消息2")  # 创建线程2
notifier3.send_text(WEBHOOK_URL, "消息3")  # 创建线程3

print(f"当前线程数: {threading.active_count()}")
# 输出：当前线程数: 4（主线程 + 3个工作线程）
```

**正确做法**：
```python
# 只创建一个实例
notifier = WeComNotifier()

# 所有消息共享同一个队列和线程
notifier.send_text(WEBHOOK_URL, "消息1")
notifier.send_text(WEBHOOK_URL, "消息2")
notifier.send_text(WEBHOOK_URL, "消息3")

print(f"当前线程数: {threading.active_count()}")
# 输出：当前线程数: 2（主线程 + 1个工作线程）
```

### 🎯 实际项目集成示例

**Flask 应用**：
```python
# app/__init__.py
from flask import Flask
from wecom_notifier import WeComNotifier

# 全局实例
notifier = WeComNotifier()

def create_app():
    app = Flask(__name__)
    # ... 其他配置
    return app

# app/tasks.py
from app import notifier
from config import WEBHOOK_URL

def send_task_notification(task_id, status):
    notifier.send_text(
        webhook_url=WEBHOOK_URL,
        content=f"任务 {task_id} {status}"
    )
```

**Django 应用**：
```python
# myproject/settings.py
from wecom_notifier import WeComNotifier

WECOM_NOTIFIER = WeComNotifier()
WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK_URL")

# myapp/tasks.py (Celery任务)
from django.conf import settings

def send_notification(message):
    settings.WECOM_NOTIFIER.send_text(
        webhook_url=settings.WECOM_WEBHOOK,
        content=message
    )
```

**通用脚本**：
```python
# utils/notifier.py
from wecom_notifier import WeComNotifier
import os

# 模块级单例
_notifier = None

def get_notifier():
    """获取全局 notifier 实例"""
    global _notifier
    if _notifier is None:
        _notifier = WeComNotifier()
    return _notifier

# 使用
from utils.notifier import get_notifier

notifier = get_notifier()
notifier.send_text(WEBHOOK_URL, "消息")
```

## ⚠️ 注意事项

### 1. Webhook安全
- ❌ 不要将webhook地址提交到公开仓库
- ✅ 使用环境变量存储
- ✅ 使用配置文件（加入.gitignore）

```python
import os
WEBHOOK_URL = os.getenv("WECOM_WEBHOOK_URL")
```

### 2. 实例管理（重要！）
- ✅ **推荐**：全局使用单个 `WeComNotifier` 实例
- ❌ **避免**：频繁创建新实例或多实例并发
- ❌ **避免**：在函数内部创建实例后立即销毁
- 📖 详见上方"最佳实践"章节

### 2.1 包内导入约定（避免重复实例）
- 在包内代码中，务必使用包内绝对或相对导入，避免同一模块以不同名称被加载两次，从而导致单例失效。
- 错误示例：`from utils.wechat import send_long_text_wechat`
- 正确示例：
  - 包内绝对导入：`from ..utils.wechat import send_long_text_wechat`
  - 同包相对导入：`from .wechat import WechatNotifier`
 这样可以确保 `wechat.py` 只加载一次，其全局 `WeComNotifier` 实例不会被重复初始化。

### 3. 频率限制
- 企业微信限制：20条/分钟/webhook
- 本项目自动处理：
  - **本地预防**：滑动窗口算法限速
  - **服务端频控智能重试**：等待65秒后重试，最多5次
- 即使 webhook 被其他程序触发频控，消息也会等待后成功发送
- 详见 README.md 的"频率控制（双层保护）"章节

### 4. 消息长度
- 限制：4096字节/条
- 本项目自动分段，无需手动处理
- 分段间隔默认1000ms

### 5. @all功能
- `text`格式原生支持
- `markdown_v2`和`image`需额外发送text消息
- 本项目自动处理

### 6. 错误处理
```python
result = notifier.send_text(...)

if not result.is_success():
    # 发送失败，查看错误
    print(f"错误: {result.error}")

    # 可以实现备用通知方式
    send_email_alert(result.error)
```

## 🐛 故障排查

### 问题1：发送失败
```python
# 检查webhook是否有效
result = notifier.send_text(
    webhook_url=WEBHOOK_URL,
    content="测试消息",
    async_send=False  # 同步模式便于调试
)

if not result.is_success():
    print(result.error)  # 查看具体错误
```

### 问题2：消息顺序混乱
- 确认：同一消息的分段是连续的
- 不同消息可能交错（这是正常的）
- 如需严格顺序，使用同步模式

### 问题3：超过频率限制
- 检查是否有其他程序也在使用同一webhook
- 本项目会自动等待，但外部调用会绕过限制

### 问题4：日志太多
```python
# 减少日志输出
notifier = WeComNotifier(log_level="WARNING")

# 或使用自定义logger
import logging
my_logger = logging.getLogger("my_app")
notifier = WeComNotifier(logger=my_logger)
```

## 📖 更多信息

- [README.md](README.md) - 项目介绍
- [tests/](tests/) - 测试示例
- [examples/basic_usage.py](examples/basic_usage.py) - 完整示例

---

有问题？欢迎提issue：https://github.com/yourusername/wecom-notifier/issues
