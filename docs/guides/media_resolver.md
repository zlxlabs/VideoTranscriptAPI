# MediaResolverAPI 集成指南（抖音 / 小红书解析）

> 适用版本：v1（接管 **抖音 + 小红书**）。其它平台（B 站 / YouTube / 小宇宙）不受影响，仍走原生下载器。

## 这是什么

[MediaResolverAPI](https://github.com/) 是一个独立的「短视频 URL → 无水印直链 + 元数据」解析服务，
内置 TikHub 多端点降级 + Cobalt 兜底。本项目可选地把**抖音 / 小红书的解析**外包给它，从而：

- 把易碎的 TikHub 解析逻辑集中到专用服务，本仓库退化为「下载 + 转录 + LLM」；
- 抖音改为下载**完整 mp4 再由 CapsWriter 提取音轨**（而非旧版直接抓 `music.play_url` 的 mp3）——
  对套用热门 BGM 模板的口播视频，提取的是**视频自带人声**而非背景乐，转录更准。

> ⚠️ **行为变更**：开启后抖音下载体积由 mp3 增大为 mp4。长视频可能撞 `storage.max_download_size_mb`
> 上限，或 CapsWriter 一次性入内存的限制。短视频无影响。

## 何时启用

| 你的情况 | 建议 |
|---------|------|
| 抖音/小红书解析经常失败、想集中维护解析逻辑 | ✅ 启用 |
| 已部署 MediaResolverAPI 服务并有 API Key | ✅ 启用 |
| 只转录 B 站/YouTube/小宇宙 | 无需启用（默认 off，不影响） |
| 没有 MediaResolverAPI 服务 | 保持 off，继续用内置 TikHub 直连 |

默认 **关闭**。开关打开前请确认 MediaResolverAPI 服务可达。

## 前置条件

1. 一个可访问的 MediaResolverAPI 服务（自建或他人提供），拿到 `base_url` 与 `X-API-Key`。
2. 服务健康检查：

   ```bash
   curl http://<your-host>:<port>/health
   # 期望返回 {"status":"ok"}
   ```

## 配置

编辑 `config/config.jsonc`，新增/修改两段：

```jsonc
{
  // 下载器路由开关
  "downloaders": {
    "use_media_resolver": true          // 设 true 才启用；默认 false
  },

  // MediaResolverAPI 连接配置（仅 use_media_resolver=true 时生效）
  "media_resolver": {
    "base_url": "http://host.docker.internal:8000",  // 服务地址；Docker 内不能用 localhost
    "api_key": "your-x-api-key",         // X-API-Key 鉴权
    "timeout": 30,                       // 单次请求超时（秒）
    "max_retries": 2                     // 解析重试次数（网络错误/5xx 退避重试）
  }
}
```

> **Docker 注意**：容器内访问宿主机服务用 `host.docker.internal`，不要写 `localhost` / `127.0.0.1`。

配置改完无需改代码——`factory` 会在 `use_media_resolver=true` 时自动把抖音/小红书路由到
`MediaResolverDownloader`，并跳过旧的 `DouyinDownloader` / `XiaohongshuDownloader`。

## 使用

开关打开后，正常提交抖音/小红书链接即可，无需任何额外参数：

```bash
curl -X POST http://localhost:8000/api/transcribe \
  -H "Authorization: Bearer <your-auth-token>" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://v.douyin.com/xxxxxxx/"}'
```

支持的链接形态：`douyin.com` / `v.douyin.com` 短链 / `xiaohongshu.com` / `xhslink.com` 短链。

## 错误与提示对照

解析服务的失败会被映射为明确的用户提示（终态错误不重试、直达用户）：

| 场景 | 用户看到 | 是否重试 |
|------|---------|---------|
| 服务超时/连接拒绝/DNS | 解析服务暂不可用 | 是（退避） |
| API Key 错误/缺失（HTTP 401） | 鉴权失败（配置错误，请检查 `api_key`） | 否 |
| 无法识别的链接（HTTP 400） | 无法识别的链接 | 否 |
| 图文/已删除/私密等无视频内容 | 该内容无可转录视频 | 否 |
| 全部解析源失败 | 解析失败，稍后再试 | 否 |
| 服务端错误（HTTP 5xx） | 解析服务异常 | 是 |

> 注：当前 MediaResolverAPI 的 `error` 仅返回文案、无结构化 `error.code`，因此「图文/删除」类终态
> 可能被笼统归为「解析失败，稍后再试」。若你维护该服务，建议为终态返回 `error.code` 以便精确区分。

## 安全

MediaResolverAPI 返回的视频直链在下载前会经过 **SSRF 校验**（`utils/url_validator.validate_url_safe`），
阻止指向内网/云元数据端点（如 `169.254.169.254`）的恶意直链。校验不通过会终止下载并提示用户。

## 回退

若启用后遇到问题，把 `downloaders.use_media_resolver` 改回 `false` 即可立即回到内置 TikHub 直连下载器，
无需回滚代码（旧下载器在迁移期保留）。

## 故障排查

| 现象 | 排查 |
|------|------|
| 提示「鉴权失败」 | 检查 `media_resolver.api_key`；用 `curl -H "X-API-Key: <key>"` 直接打 `/api/resolve` 验证 |
| 提示「解析服务暂不可用」 | 检查 `base_url` 可达性、`/health`、Docker 内是否误用 localhost |
| 抖音下载撞大小上限 | 调高 `storage.max_download_size_mb`，或对长视频暂时关闭开关 |
| 想确认走了哪个下载器 | 看日志 `为URL创建下载器: ..., 类型: MediaResolverDownloader` |

## 相关文档

- 设计与实现决策记录：[docs/designs/media-resolver-integration.md](../designs/media-resolver-integration.md)
- 后续 v2 规划（多平台/观测/CDN 兜底）：见仓库 `TODOS.md`
