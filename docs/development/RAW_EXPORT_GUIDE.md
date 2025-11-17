# 原始文件导出功能使用指南

## 功能概述

本功能允许用户通过在查看链接后添加参数，直接获取原始的校对文本、总结文本或转录文本文件。采用 **GitHub Raw 模式**，既支持浏览器直接查看，也支持下载工具下载。

## 核心特性

- **GitHub Raw 模式**：与 GitHub 的 raw 文件查看行为一致
- **浏览器直接查看**：打开链接即可在浏览器中查看纯文本
- **下载工具支持**：IDM、迅雷等下载工具可直接下载，文件名自动设置
- **中文文件名支持**：正确处理中文文件名的 URL 编码
- **无需额外认证**：与查看页面保持一致的访问权限

---

## URL 格式

### 基础查看链接

```
https://your-domain.com/view/{view_token}
```

### 原始文件导出链接

```bash
# 获取校对文本
https://your-domain.com/view/{view_token}?raw=calibrated

# 获取总结文本
https://your-domain.com/view/{view_token}?raw=summary

# 获取原始转录
https://your-domain.com/view/{view_token}?raw=transcript
```

---

## 支持的导出类型

| 参数值 | 说明 | 对应文件 | 文件格式 |
|-------|------|----------|---------|
| `calibrated` | LLM 校对后的文本 | `llm_calibrated.txt` | TXT |
| `summary` | LLM 生成的总结 | `llm_summary.txt` | TXT |
| `transcript` | 原始转录文本 | `transcript_funasr.json` 或 `transcript_capswriter.txt` | JSON/TXT |

---

## 使用示例

### 1. 浏览器直接查看

**场景**：想快速查看校对文本，进行复制

```
https://transcript.example.com/view/view_abc123xyz?raw=calibrated
```

**效果**：
- 浏览器直接显示纯文本内容
- 可以全选复制（Ctrl+A, Ctrl+C）
- 页面简洁，无任何格式

### 2. 下载工具下载

**场景**：使用 IDM 或迅雷下载文件保存到本地

```
https://transcript.example.com/view/view_abc123xyz?raw=calibrated
```

**操作**：
1. 复制链接
2. 在 IDM/迅雷中新建下载任务
3. 粘贴链接
4. 自动识别文件名：`深度学习入门教程-校对文本-哔哩哔哩.txt`

### 3. 浏览器右键另存为

**场景**：直接在浏览器中保存文件

```
https://transcript.example.com/view/view_abc123xyz?raw=summary
```

**操作**：
1. 在浏览器中打开链接
2. 右键 → "另存为"
3. 文件名自动填充：`视频标题-总结文本-平台.txt`

### 4. 命令行下载

**场景**：使用 curl 或 wget 批量下载

```bash
# 使用 curl
curl "https://transcript.example.com/view/view_abc123xyz?raw=calibrated" -o output.txt

# 使用 wget
wget "https://transcript.example.com/view/view_abc123xyz?raw=calibrated"
```

---

## 文件命名规则

### 格式

```
{视频标题}-{内容类型}-{平台}.txt
```

### 示例

| 视频标题 | 平台 | 导出类型 | 文件名 |
|---------|------|---------|--------|
| 深度学习入门教程 | bilibili | calibrated | `深度学习入门教程-校对文本-哔哩哔哩.txt` |
| Python Tutorial | youtube | summary | `Python Tutorial-总结文本-YouTube.txt` |
| 美食制作技巧 | xiaohongshu | transcript | `美食制作技巧-原始转录-小红书.txt` |
| 播客节目 | xiaoyuzhou | calibrated | `播客节目-校对文本-小宇宙.txt` |

### 特殊字符处理

文件名中的非法字符会被自动清理：

| 原始字符 | 替换为 |
|---------|--------|
| `/` `\` `|` `:` `*` `?` `"` `<` `>` | `_` |
| 首尾空格和点 | 移除 |

**示例**：
- `Title with / and :` → `Title with _ and _-校对文本-YouTube.txt`

### 长标题处理

超过 50 个字符的标题会被截断并添加省略号：

```
这是一个非常非常非常非常非常非常非常非常非常非常长的标题...
↓
这是一个非常非常非常非常非常非常非常非常非常非常长的标题...-校对文本-哔哩哔哩.txt
```

---

## 响应格式

### 成功响应

```http
HTTP/1.1 200 OK
Content-Type: text/plain; charset=utf-8
Content-Disposition: inline; filename*=UTF-8''%E6%B7%B1%E5%BA%A6%E5%AD%A6%E4%B9%A0%E5%85%A5%E9%97%A8-%E6%A0%A1%E5%AF%B9%E6%96%87%E6%9C%AC-%E5%93%94%E5%93%A9%E5%93%94%E5%93%A9.txt
X-Content-Type-Options: nosniff

【文件内容】
```

**关键响应头：**
- `Content-Type: text/plain` - 浏览器识别为纯文本
- `Content-Disposition: inline` - 浏览器尝试直接显示（而非下载）
- `filename*=UTF-8''...` - RFC 2231 编码，支持中文文件名

### 错误响应

#### 1. 任务处理中（202）

```http
HTTP/1.1 202 Accepted
Content-Type: text/plain; charset=utf-8

⏳ 校对文本正在生成中，请稍后再试...

请刷新页面或稍后访问此链接。
```

#### 2. 文件不存在（404）

```http
HTTP/1.1 404 Not Found
Content-Type: text/plain; charset=utf-8

❌ 校对文本文件不存在

该任务可能未启用相关功能。
```

#### 3. 文件已清理（410）

```http
HTTP/1.1 410 Gone
Content-Type: text/plain; charset=utf-8

❌ 该文件已被清理

如需重新获取，请重新提交转录任务。
```

#### 4. 不支持的导出类型（400）

```http
HTTP/1.1 400 Bad Request
Content-Type: text/plain; charset=utf-8

❌ 不支持的导出类型: invalid_type

支持的类型: calibrated, summary, transcript
```

---

## 技术实现

### 1. 核心函数

#### `sanitize_filename(filename: str) -> str`

清理文件名中的非法字符，确保跨平台兼容性。

```python
sanitize_filename("Title with / and :")
# 返回: "Title with _ and _"
```

#### `generate_download_filename(title: str, platform: str, content_type: str) -> str`

生成标准化的下载文件名。

```python
generate_download_filename("深度学习入门", "bilibili", "calibrated")
# 返回: "深度学习入门-校对文本-哔哩哔哩.txt"
```

#### `handle_file_export(view_data: Dict, export_type: str) -> Response`

处理文件导出请求，返回 FastAPI Response 对象。

### 2. 路由修改

```python
@app.get("/view/{view_token}")
async def view_transcript(
    view_token: str,
    request: Request,
    raw: Optional[str] = None  # 新增参数
):
    view_data = cache_manager.get_view_data_by_token(view_token)

    # 如果请求原始文件
    if raw:
        return handle_file_export(view_data, raw)

    # 否则返回 HTML 页面
    return render_html_page(view_data)
```

---

## 最佳实践

### 1. 分享链接

当分享校对文本时：

```
# ✅ 推荐：直接分享 raw 链接
https://transcript.example.com/view/view_abc123?raw=calibrated

# ❌ 不推荐：让用户自己添加参数
https://transcript.example.com/view/view_abc123
```

### 2. 批量下载

使用脚本批量下载多个任务的校对文本：

```bash
#!/bin/bash

# view_tokens.txt 包含所有 view_token
while read token; do
  curl "https://transcript.example.com/view/${token}?raw=calibrated" \
    -o "${token}.txt"
done < view_tokens.txt
```

### 3. API 集成

在其他系统中集成：

```python
import requests

def download_calibrated_text(view_token):
    """下载校对文本"""
    url = f"https://transcript.example.com/view/{view_token}?raw=calibrated"
    response = requests.get(url)

    if response.status_code == 200:
        return response.text
    elif response.status_code == 202:
        print("文本正在生成中，请稍后重试")
    elif response.status_code == 404:
        print("文件不存在")
    else:
        print(f"错误: {response.status_code}")

    return None
```

---

## 常见问题

### Q1: 为什么有时候浏览器会下载，而不是显示？

**A**: 这取决于浏览器的设置。我们使用 `Content-Disposition: inline`，大多数现代浏览器会尝试显示纯文本文件。如果浏览器下载了文件，可以直接用文本编辑器打开。

### Q2: 中文文件名在某些下载工具中显示乱码怎么办？

**A**: 我们使用了 RFC 2231 标准的 `filename*=UTF-8''...` 编码，主流的下载工具（IDM、迅雷、浏览器）都能正确识别。如果遇到乱码，可以：
1. 更新下载工具到最新版本
2. 手动重命名文件

### Q3: 如何获取 JSON 格式的转录数据？

**A**: 使用 `?raw=transcript` 参数。如果存在 FunASR 转录结果（`transcript_funasr.json`），会优先返回 JSON 格式；否则返回 TXT 格式的 CapsWriter 转录。

### Q4: 可以同时获取多个文件吗？

**A**: 目前不支持一次请求获取多个文件。未来可能会添加打包下载功能（`?raw=all`），返回包含所有文件的 ZIP 压缩包。

### Q5: 导出功能需要额外的认证吗？

**A**: 不需要。与查看页面保持一致，只要有 `view_token` 就可以访问。`view_token` 本身是 32 字节的安全随机字符串，很难被枚举。

---

## 安全考虑

### 1. 访问控制

- **无需额外认证**：导出功能与查看页面共享相同的访问策略
- **view_token 强度**：使用 `secrets.token_urlsafe(32)` 生成，搜索空间为 2^256
- **防枚举攻击**：实际上不可能通过暴力破解获取有效的 view_token

### 2. 内容安全

- **MIME 类型固定**：强制设置为 `text/plain`，防止浏览器误解析
- **X-Content-Type-Options: nosniff**：防止浏览器嗅探内容类型
- **编码固定**：强制 UTF-8 编码，防止编码注入攻击

### 3. 速率限制

目前未实现速率限制，但可以在未来添加：
- 基于 IP 的访问频率限制
- 基于 view_token 的下载次数统计

---

## 性能优化

### 1. 缓存策略

建议在 Nginx/CDN 层添加缓存：

```nginx
location ~ ^/view/[^/]+$ {
    # 对 raw 参数的请求启用缓存
    if ($arg_raw) {
        add_header Cache-Control "public, max-age=3600";
    }
}
```

### 2. 文件大小

- 校对文本和总结文本通常较小（几 KB 到几百 KB）
- 原始转录 JSON 可能较大（几 MB）
- 考虑对大文件启用 gzip 压缩

---

## 更新日志

### v1.0.0 (2025-11-17)

- ✨ 新增原始文件导出功能
- ✨ 支持 GitHub Raw 模式（浏览器查看 + 下载工具下载）
- ✨ 智能文件名生成（视频标题-内容类型-平台）
- ✨ 中文文件名 URL 编码支持
- ✨ 完善的错误处理和状态码

---

## 未来计划

### 短期

- [ ] 添加访问日志和统计
- [ ] 支持 HEAD 请求（快速检查文件是否存在）
- [ ] 添加 ETag 支持（缓存优化）

### 长期

- [ ] 支持打包下载（`?raw=all` 返回 ZIP）
- [ ] 支持自定义文件名（`?raw=calibrated&filename=custom.txt`）
- [ ] 支持格式转换（如 JSON → CSV）
- [ ] API 密钥访问模式（可选的额外安全层）
