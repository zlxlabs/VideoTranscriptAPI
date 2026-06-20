# 设计：引入 MediaResolverAPI 接管抖音/小红书解析

> 来源：/plan-ceo-review（2026-06-20，SELECTIVE EXPANSION → 经 spec 审查 + codex 双模型挑战后收窄）。
> 实现方案：**收窄版 — 单个 MediaResolverDownloader + factory flag**（v1 只接管抖音/小红书，
> 不抽基类、不加平台；下文出现的"基类/ResolverBackedDownloader"在 v1 即指这一个下载器类）。
> v1 范围：抖音/小红书 + 3 个 P0 加固；E1 多平台 + E3 埋点 + candidate_urls 推 v2。

## 背景与 premise

VideoTranscriptAPI 现状：`douyin.py`(298行) / `xiaohongshu.py`(661行) 各自直连 TikHub
(`base.make_api_request` → api.tikhub.io)，维护两套易碎的解析逻辑（抖音单端点、小红书
4 端点回退 + 多 CDN 候选）。MediaResolverAPI 已把"短视频 URL → 无水印直链 + 元数据"的解析
集中化，内置多端点降级引擎 + Cobalt 兜底，覆盖 8 平台。

引入它的本质：**把易碎的 TikHub 解析逻辑外包/集中到专用服务**，本仓库退化为
"下载 + 转录 + LLM"。注意"音频下载"是措辞偏差——MediaResolverAPI 是**解析器**，只返回
`video_url` 视频直链，不返回音频、不下文件；下载动作仍在本仓库 `download_file()`。

### 契约对照
- MediaResolverAPI：`POST /api/resolve`，`X-API-Key` 鉴权，入参 `{url, translate, force_refresh}`，
  返回 `{success, data:{platform, video_id, title, description, author_name/id, video_url,
  width, height, duration, provider:tikhub|cobalt, ...}, error}`。**仅 video_url，无 audio_url**。
- 本仓库契约：`VideoMetadata`(video_id/platform/title/author/description/duration) +
  `DownloadInfo`(download_url/file_ext/local_file/...)。映射直接。

### 抖音下载语义变更（核心行为变更，非"附带"）
旧 `douyin.py` 优先抓 `music.play_url`（mp3 音频）。新方案统一下完整 mp4，再由
CapsWriter `_extract_audio`(ffmpeg) 提音轨。**这是实质行为变更**：
- 正确性收益：旧版对套用热门 BGM 模板的口播视频拿到的是背景乐而非人声（潜在 bug），
  新版提取视频自带音轨 = 真实人声，转录更准。
- 代价：下载体积 mp3→mp4 显著增大；长视频可能撞 `max_download_size_mb=4096` 上限，
  且 CapsWriter PCM 一次性入内存(capswriter_client.py:469) 可能先撞内存。v2 评估流式/duration 限制。

## Scope Decisions（收窄后）

| # | Proposal | Effort | Decision | Reasoning |
|---|----------|--------|----------|-----------|
| 基线 | 抖音+小红书改用 MediaResolverAPI（单 MediaResolverDownloader + flag） | S-M | ACCEPTED v1 | 用户核心诉求 |
| P0×3 | 异常传播 + SSRF 校验位置 + 403 结构化失败 | S | ACCEPTED v1 | 正确性必做，codex 行号坐实 |
| E1 | 解锁快手/TikTok/IG/FB/Pinterest | M | DEFERRED v2 | 双模型：牵动 URLParser/前端/缓存key/测试，被低估 |
| E3 | 解析观测埋点（provider/降级/延迟接入 audit） | S | DEFERRED v2 | 收窄 v1，随 E1 一起做 |
| E2 | translate 中文描述（非中文视频） | XS | DEFERRED | 仅对跨语言内容有意义 |
| FORK4-B | resolver 返回 candidate_urls 多 CDN | S | DEFERRED v2 | 需改两个仓库 |

## 关键架构决策（4 forks，均选 A）
- **FORK1-A** MediaResolverDownloader 内部按归一化 url 缓存一次 resolve，`_fetch_metadata`
  与 `_fetch_download_info` 共享，一次网络调用喂两者。
- **FORK2-A** config flag `use_media_resolver`（默认 off）；迁移期保留旧 TikHub 直连下载器
  作回退；验证稳定后 flip on，follow-up PR 删旧代码。可逆性 4/5。
- **FORK3-A** 终态(图文/删除/私密)抛 `NonVideoContentError`，不重试，用户见"该内容无可转录视频"。
- **FORK4-A** 接受单链；本仓库不再多 CDN 重试，依赖 download_file 常规重试 + 403 重解析。

## Error & Rescue Registry
```
ResolverClient#resolve     | 失败场景          | 异常类                  | 重试? | 用户看到
---------------------------|-------------------|-------------------------|-------|----------------
超时/连接拒绝/DNS          | 服务不可达        | NetworkError(复用)      | Y退避 | 解析服务暂不可用
HTTP 401                   | api_key 错/缺      | ResolverAuthError(新)   | N     | 告警(配置错)
HTTP 400                   | url 无法识别      | InvalidURLError(新)     | N     | 无法识别的链接
200 success=false 终态     | 图文/删除/私密    | NonVideoContentError(新)| N     | 该内容无可转录视频
200 success=false 全源失败 | TikHub+Cobalt 挂  | ResolverResolveError(新)| N     | 解析失败,稍后再试
200 缺 video_url/JSON异常  | 响应畸形          | ResolverResponseError(新)| N    | 记录全文+失败
HTTP 500                   | 服务端错          | ResolverServerError(新) | Y     | 解析服务异常
```

## 实现契约补全（实现前必须钉死）
1. **缓存 key 鸡蛋悖论**：基类 `get_metadata/get_download_info` 用 `extract_video_id` 做 key，
   但 resolver 入参是 url、video_id 在响应里。→ 用**归一化 url** 作缓存 key，绕开 `extract_video_id`
   （`extract_video_id` 仍实现，仅短链展开/正则供日志）。**注意**：这需要在下载器层覆写这两个
   public 方法，不只是加缓存。
2. **新增 `_resolve_cache[normalized_url] = raw_resolve_response`**，两个 `_fetch_*` 从它派生。
3. **`file_ext` 推断**：优先 `video_url` 路径后缀；取不到默认 `mp4`。
4. **`get_subtitle`** 返回 `None`（resolver 无字幕）。
5. **resolver `success=false` 终态判定契约（⚠️ 实现前对 MediaResolverAPI 核对）**：需 `error`
   里有可判定字段（如 `error.code` 枚举）区分"图文/删除/私密"(NonVideoContentError) 与"全源失败"
   (ResolverResolveError)；若服务只回文案，先按文案/HTTP 粗分并提 issue 给 MediaResolverAPI 加 code。
6. **`translate=false`/`force_refresh=false`** 默认；`force_refresh=true` 仅在下载 403/失效时重解析。
7. **HTTP 客户端** `MediaResolverClient` 用 `requests`，超时/退避复用现有风格。
8. **异常落位**：新异常加到 `errors/network.py`、`errors/download.py` 并在 `errors/__init__.py` 导出。
9. **配置 schema**（config.jsonc）：
   ```jsonc
   "downloaders": { "use_media_resolver": false },
   "media_resolver": {
     "base_url": "http://host.docker.internal:8000",  // Docker 内不能用 localhost
     "api_key": "your-x-api-key", "timeout": 30, "max_retries": 2
   }
   ```

## P0 加固（v1 必做，codex 行号坐实）
1. **调用层终态异常传播**：`process_transcription()`(transcription.py:687/741) 现吞掉异常走默认失败路径；
   需让 `NonVideoContentError`/`ResolverAuthError` 等终态异常直达用户提示，否则 Error Registry 形同虚设。
2. **SSRF 校验下沉**：现只校验用户传入 `download_url`(transcription.py:359/1194)；resolver 返回的直链
   下载前未校验。须在 client 返回 `DownloadInfo` 前统一用 `utils/url_validator.validate_url_safe` 校验。
3. **download_file 403 结构化失败（eng 决策 #1-A：resolver 下载器自包一层）**：
   不改 `BaseDownloader.download_file` 签名（避免波及 youtube/bilibili/小宇宙/generic）。
   MediaResolverDownloader 自己包一层下载：内部捕获 403/失效 → `force_refresh=true` 重解析 → 再下；
   仍失败抛 `DownloadFailedError`。爆炸半径仅限新类。

## eng 决策（架构层补充）
- **#1-A** download_file 403 改造收敛到 resolver 下载器内部（见上 P0-3），不动共享基类。
- **#2-A** factory 路由顺序：`use_media_resolver=on` 时，MediaResolverDownloader 必须排在
  `DouyinDownloader`/`XiaohongshuDownloader` **之前**（factory 取第一个 `can_handle`=True）；
  推荐 flag=on 时直接不实例化旧两个，避免双重命中。flag=off 走旧路径。两条路由都要回归测试。

## v2（后续 PR）
- E1 多平台（快手/TikTok/IG/FB/Pinterest）：扩 URLParser、factory、前端校验、各平台回归。
- E3 解析观测埋点接入 audit（provider/降级/延迟）。
- FORK4-B resolver 返回 candidate_urls，恢复客户端多 CDN。
- 删除旧 TikHub 下载器（v1 验证稳定后）。
- 抖音长视频内存评估（流式/duration 限制）。

## NOT in scope
- E2 translate 中文描述（仅跨语言有意义）。
- B站/YouTube/小宇宙 → 保留原生 BBDown/yt-dlp（质量更高、可拿字幕，不走 resolver）。

## What already exists（复用）
- `BaseDownloader` 契约、`VideoMetadata`/`DownloadInfo`、`download_file()` 重试与媒体校验、`factory` 路由。
- `capswriter_client._extract_audio`(ffmpeg) → 视频提音轨已就绪。
- audit 系统 → E3 埋点对接（v2）。
- `utils/url_validator.validate_url_safe` + download_url 内网源白名单 → video_url 校验对接。

## 实现任务（v1）
T1 MediaResolverClient（POST /api/resolve, X-API-Key, requests, 超时/退避）
T2 MediaResolverDownloader（can_handle 抖音+小红书, _resolve_cache, 映射, file_ext, get_subtitle=None, 覆写两 public 方法）
T3 config flag + media_resolver 配置段 + factory 按 flag 路由（旧下载器迁移期保留）
T4 新异常入 errors 包并导出
T5 [P0] process_transcription 终态异常传播
T6 [P0] SSRF 校验下沉到 resolver 返回的 video_url
T7 [P0+FORK4] download_file 403 结构化失败 + force_refresh 重解析
T8 对 MediaResolverAPI 核对 success=false 的 error 判定契约
T9 测试：client mock + 终态/auth/超时 + can_handle + 抖音/小红书各一条真链端到端回归

## 实现状态（2026-06-20，TDD 落地）

v1 全部实现任务已按 TDD 落地并通过单测（156 用例绿）。落点：

| 任务 | 状态 | 落点 | 测试 |
|------|------|------|------|
| T1 MediaResolverClient | ✅ | `downloaders/media_resolver_client.py` | `tests/unit/test_media_resolver_client.py`(24) |
| T2 MediaResolverDownloader | ✅ | `downloaders/media_resolver.py` | `tests/unit/test_media_resolver_downloader.py`(18) |
| T3 config flag + factory | ✅ | `downloaders/factory.py` + `config.example.jsonc` | `tests/unit/test_downloader_factory.py`(新增路由用例) |
| T4 新异常 | ✅ | `errors/network.py`(Auth/Server) + `errors/download.py`(InvalidURL/NonVideo/Resolve/Response) | `tests/unit/test_errors.py` |
| T5 [P0] 终态异常传播 | ✅ | `api/services/transcription.py`(元数据/下载信息 3 处 except) | `tests/unit/test_resolver_terminal_propagation.py`(3) |
| T6 [P0] SSRF 下沉 | ✅ | `media_resolver._fetch_download_info` 调 `validate_url_safe` | downloader 测试 `TestSSRF` |
| T7 [P0+FORK4] 403 重解析 | ✅ | `media_resolver.download_file` 覆写（force_refresh 重解析重下） | downloader 测试 `TestDownloadReResolve` |

### 关键实现说明
- **缓存 key**：用 `_normalize_url`（去空白/小写 scheme+host/去 fragment 与末尾斜杠）作 key，
  覆写 `get_metadata`/`get_download_info`；`_resolve_cache[normalized_url]` 一次 resolve 喂两个 `_fetch_*`。
- **403 重解析**：`_video_url_to_page[video_url]=normalized_page_url` 反查映射；下载失败时反查页面 url
  做一次 `force_refresh=true` 重解析；无法反查（非 resolver 直链）则抛 `DownloadFailedError`（爆炸半径仅本类）。
- **SSRF**：resolver 直链 SSRF 校验失败时抛 `ResolverResponseError`（终态），阻断下载并向用户透传。
- **终态传播**：`_TERMINAL_RESOLVER_ERRORS`（Auth/InvalidURL/NonVideo/Resolve/Response）在三处 except 重抛，
  冒泡到外层 handler 以 `str(exc)` 反馈用户；可重试网络类（NetworkError/ResolverServerError）保持原 soft-fail。

### T8 success=false 契约 — ✅ 已在线核对（2026-06-20，服务 `http://100.107.95.24:8206`）
经 OpenAPI + 实打核对，确认契约（与客户端实现一致）：
- **鉴权头是 `X-API-Key`**（securitySchemes APIKeyHeader）；无/错 key → **HTTP 401**
  `{"detail":"Invalid or missing API key"}` → `ResolverAuthError` ✓（实打验证通过）。
- **无法识别的 URL → HTTP 400** `{"detail":"Unsupported URL or could not extract video ID: ..."}`
  → `InvalidURLError` ✓（实打验证通过）。
- **body 缺 url → HTTP 422**（FastAPI 校验）→ 客户端归 `ResolverResponseError`（可接受）。
- **`error` 字段是纯字符串**（`anyOf:[string,null]`），**当前版本无 `error.code`**。因此 success=false
  的判定**只能走文案兜底**——这正是设计预案。实打确认业务失败文案形如
  `"All providers failed for platform 'douyin', video_id '...'. Errors: [tikhub: ...]"`，
  客户端默认归 `ResolverResolveError`（"解析失败，稍后再试"）✓。
- 客户端保留的 `error.code` 判定分支（`_NON_VIDEO_CODES`/`_RESOLVE_FAIL_CODES`）是**前向兼容**：
  当前服务未回 code、走不到，但服务日后加 code 即自动生效。**建议给 MediaResolverAPI 提 issue**：
  为"图文/已删除/私密"等终态返回结构化 `error.code`，使其能精确归 `NonVideoContentError`
  （当前这类终态会被笼统归为 `ResolverResolveError`，提示"稍后再试"而非"该内容无可转录视频"，不致命）。
- **`data` 字段名核对**：`platform/video_id/title/description/author_name/author_id/video_url/
  width/height/duration/provider/translated_description`，与 downloader 映射一致（`author_name`/
  `video_url`/`duration`/`provider`）✓。`translate` 服务端默认 true，客户端 v1 显式传 false ✓。
- **平台覆盖**：`/api/platforms` = douyin/xiaohongshu(tikhub)、tiktok/instagram/youtube(tikhub+cobalt)、
  kuaishou(tikhub)、pinterest/facebook(cobalt)，印证 v2 E1 可解锁多平台。

### 抖音/小红书真链端到端回归 — ✅ 已通过（2026-06-20）
用真实 `MediaResolverDownloader` 打活服务，走 resolve → 元数据 → 下载信息(SSRF) → 实际下载 → ffprobe 校验：
- **抖音** `v.douyin.com/WrWPEfUxJlY` → vid=7306330118669012250、3.32MB、15s、含音频+视频流 ✓
- **小红书** `explore/690c64e2...` → vid=690c64e2...、6.39MB、16.97s、含音频+视频流 ✓
- **FORK1-A 缓存命中**实证：`get_download_info` 复用 `get_metadata` 的 resolve（日志"使用缓存 resolve 结果"，一次网络调用）✓
- 抖音本条 `title` 为空、`duration` 为 null（resolver 该条数据如此，非集成 bug；转录不依赖）。
- 转录阶段（CapsWriter `_extract_audio` 从 mp4 提音轨 → ASR）为既有未改管线，新代码产出的 mp4 已含音频流，可直接喂入。

**flag 仍默认 off**：本地已验证就绪，是否在生产 flip on 属行为变更，由用户决定（FORK2-A 可逆 4/5）。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | clean | 5 提, 2 accepted v1, 3 deferred v2 |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | issues_found | 3 P0 + 范围收窄, 已折叠 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | clean | 2 issues (#1 403改造收敛, #2 factory顺序), 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | 无 UI scope |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CODEX:** outside voice 坐实 3 个 P0（异常传播 transcription.py:687/741、SSRF 位置 :359/1194、403 丢状态码 base.py:285），全部并入 v1。
- **CROSS-MODEL:** spec 审查(6/10) + codex 双模型一致建议收窄 v1；用户采纳，E1/E3 推 v2。
- **VERDICT:** CEO + ENG CLEARED — ready to implement。v1 范围：抖音/小红书 + 单 MediaResolverDownloader + factory flag + 3 P0 加固。

NO UNRESOLVED DECISIONS
