# 仓专属事实

这份文档收录 VideoTranscriptAPI 的仓专属运行事实、偏好和部署指针，给之后在本仓工作的会话读。
条目于 2026-09-03 从 Claude Code 自动记忆迁出；技术细节按归档原文保留，不另建 `memory/` 目录。
读者先看每条开头的记录日期——这些事实可能已经过期。

## n305 Docker 部署

这条事实归档 frontmatter 无 `modified` 字段；正文最晚写明的日期是 2026-07-09（GHCR 凭据续期），踩坑记录于 2026-05-18。

### 部署目标
- SSH 别名: `n305`（在 `~/.ssh/config` 中定义）
- 部署路径: `/opt/media/VideoTranscriptAPI`
- 配置来源: `docker/deploy_targets.json`

### Docker 部署
- 镜像: `ghcr.io/zj1123581321/video-transcript-api:latest`
- 构建/推送: `docker/push_to_ghcr.sh`（GHA 也会触发，本地手推也可以）
- 拉取/部署: `docker/pull_and_deploy.sh`
- compose: `docker/docker-compose.yml`

### 关键配置
- 时区: `TZ=Asia/Shanghai`
- **宿主端口: `8200:8000`**（**不是** repo 默认的 `8000:8000`！portainer 在 6 天前占了 8000，
  user 手工把 VTA 改到 8200。imflow 的 `/opt/tools/imflow/config.yaml` 写死了
  `http://192.0.2.13:8200`，可作交叉验证。）
- 外部入口: `https://sum.lexgogo.site`（Cloudflare Tunnel → n305:8200）
- 数据卷: `config/` 和 `data/` 挂载到容器
- 健康端点: `curl http://localhost:8200/health` 返回 `{"status":"healthy",...}`

### 部署时务必注意
**不要把 repo 里的 `docker/docker-compose.yml` 直接 scp 覆盖到服务器**——
服务器上的 compose 是服务器侧自有版本（user 手工改过端口）。docker-deploy skill
会执行 `scp docker/docker-compose.yml`，会**抹掉**服务器端的端口定制，导致下一次
`docker compose up` 卡在 8000 端口被 portainer 占用的冲突上。

**Why:** 2026-05-18 已经踩过一次：覆盖后老容器被销毁、新容器 Created 状态启不起来，
回滚不了（旧 compose 没备份）。最后从 `/opt/tools/imflow/config.yaml` 反查出 8200。

**How to apply:** 部署 VTA 到 n305 前，先 `ssh n305 cp /opt/media/VideoTranscriptAPI/docker/docker-compose.yml{,.bak.$(date +%s)}`；
或者跳过 compose 同步只发 `pull_and_deploy.sh`；或者把 repo 的 compose port 同步成 `8200:8000`。

### GHCR 凭据（2026-07-09 续期，双 PAT 最小权限）
- 旧 classic PAT（本机+n305）2026-07-09 发现过期，已由 user 换新并分权：
  **本机 = `write:packages`（推送用），n305 = `read:packages`（只读拉取）**。
  凭据在 `~/.docker/config.json` 明文存储，故服务器只给只读 token。
- 已验证 `:latest` digest 与 61d6698 一致，pull_and_deploy 流水线正常。
- gh CLI 的 token 无 `write:packages`，不能当 GHCR 推送凭据（也不该落进 docker config）。
- 凭据失效时的应急部署（当天用过）：本地 build 后
  `docker save <img>:latest <img>:<sha> | gzip | ssh n305 'gunzip | docker load'`，
  再服务器 `docker compose up -d`。内网传 1GB 约 4 分钟。事后务必补推 GHCR，
  否则 `:latest` 落后，下次 pull_and_deploy 会静默回滚。
- 项目开源，若日后把 GHCR 包设为 public，n305 可完全去掉凭据。

### 与 Mac Studio 的关系
- Mac Studio 用 pm2 直接运行 Python（裸机部署）
- n305 用 Docker compose 运行（容器化部署）
- 两个环境并存，代码库相同

相关：[[reference_mac_studio_server]]

## 运维舰队接入范围

这条事实归档 frontmatter 无 `modified` 字段；条目自身记载的拍板日期是 2026-07-03。

2026-07-03 决定:VideoTranscriptAPI 接入 ops-dispatcher 舰队,**只接 D5 可观测 + 监控两层,不接 D3**。部署仍走现有手动 GHCR 流(`docker/push_to_ghcr.sh` → n305 `pull_and_deploy.sh`),因为舰队 D3 走阿里 ACR,而本项目公开 GHCR 镜像(README badge)镜像策略暂不决定。

**Why:** 三层解耦可独立接;D3 迁 ACR 改动大且冲突公开镜像玩法,先拿低风险高价值的崩溃可见性+存活监控。

**How to apply:** 已完成的代码/台账改动(未 push):
- `/livez` 纯存活探针(`api/routes/health.py`)——`/health` 是深度检查不能当探针
- `utils/observability.py` fail-open 接 zlx-ops-sdk,`create_app` 顶部调用;DSN 走 `docker/ops.env`(env_file 注入 os.environ,`.gitignore`,仓留 ops.env.example)。本项目配置读 config.jsonc 非 pydantic-settings,故 §6.10 DSN 启动崩坑不适用
- Dockerfile `ARG/ENV GIT_SHA` + push_to_ghcr.sh 传 `--build-arg GIT_SHA`
- ops-dispatcher `fleet/registry.yaml` 加条目 id=video-transcript-api,port 8200,glitchtip_project=video-transcript-api,sentry_dsn_secret=SENTRY_DSN_VIDEO_TRANSCRIPT_API,非D3已注释标注

**已上线 2026-07-03:** GlitchTip project=video-transcript-api(id=29,DSN `http://<SENTRY_DSN_VIDEO_TRANSCRIPT_API>@<ops-host>:9000/29`)、Kuma monitor `fleet-video-transcript-api` 探 `http://<deploy-host>:8200/livez` 均由用户建好。n305 部署目录 `/opt/media/VideoTranscriptAPI` **不是 git 仓**(只有 compose+pull_and_deploy.sh,代码在镜像里),故 compose 用外科手术插入 env_file(非 git pull),已 `.bak-obs` 备份;`docker/ops.env`(600 权限,含 SENTRY_DSN)已落。镜像 `push_to_ghcr.sh` 本地构建推 GHCR(tag latest + b48fa13)→ `pull_and_deploy.sh` 部署。验证全绿:/livez=200、GIT_SHA=b48fa1309b0e、SENTRY_DSN 注入、SDK 日志「已接入」、GlitchTip 测试事件送达、Kuma 探针 TS IP 200。

**注意:** VTA/ops-dispatcher 两仓 commit(b48fa13 / fd152e0)仍在本地 main **未 push**(镜像从工作树构建,部署不依赖 push)。dsn-output.txt 是手维护清单、provisioner 不写它。provision_glitchtip.py 不支持 --only。

参考 [[reference_n305_docker_deploy]]、playbook `ops-dispatcher/docs/fleet-onboarding-playbook.md`。

## 说话人归属

这条事实记录于 2026-07-28（归档 frontmatter `modified`: 2026-07-28T15:19:15.142Z）。

说话人归属工程化纠错（session 260728-1050-q7m2，spec 在 docs/sessions/260728-1050-q7m2/SPEC.md）。

已确认的关键事实（2026-07-28 调研，勿重查）：
- funasr==1.2.7 的句子→说话人归属是纯时间重叠分配，句级 embedding/margin 从未被计算；embedding 在返回前被库删除（auto_model.py:641）。拿声学置信度必须 fork funasr 内部 → 已裁决不做。
- VTAPI raw 层完整透传（transcript_funasr.json 落盘整个响应）；新字段被吞的唯一关口是 speaker_aware_processor.py。
- 采样是「时间轴前 3 条」，对簇后半段污染不敏感（Inc1 改分层）。

用户裁决：引擎侧（preset_spk_num 透传、hotword 热词）只记 backlog 不动——担心 AI 提取人数不准反而干扰聚类；长期声学置信度路线是 funasr_spk_server 里的 Qwen3 自研管线（cluster_merge.py 有 cosine/centroid 基建）。

进度：**Inc1 已合并 main（2026-07-28 PR#32，c598915）并已部署 n305**（digest 固定 c5989154a2b9，healthy；本地重放验证过问题集会触发 low_confidence_cluster_dropped，Speaker4 占 17.9% 段落）。**Inc2（语义矛盾检测，只标记不改名）已合并 main（2026-07-28 PR#33，7e127b1）并已部署 n305**（tag 7e127b1a1437，healthy；部署曾被 n305→ghcr 网络中断阻塞约 2h，用户侧修复后完成）。spec 第 7 节 v2 契约：suspect override 不写 name、reason 枚举单一来源（schemas/contradiction_scan.py 的 CONTRADICTION_REASONS）、幻觉 id 过滤、>20% unreliable、分窗扫描（400/窗重叠10、任一窗失败整体 failed）、门控与 infer_speaker_names 解耦、任务级开关 contradiction_scan（processing_options 全链路含 cache_manager 白名单）。gate 曾红一轮（3 major 全接受）。下一步：部署后跑 10-20 集真实节目攒 suspect 精确率，再裁决 Inc3/Inc4。内容：segment_id（两遍去重保稳定）、segment_overrides 容器+渲染机制、speaker_risk_flags（R1/R2）、分层采样（人均预算截断保三层非空）、UI 免责声明/风险横幅/待确认徽标。渲染锚点保持 dlg-{index}，segment_id 走 data-segment-id（floating-toc.js 依赖，勿改回）。
后续：Inc2 语义矛盾检测（只标记，含 backlog：override 徽标 status 白名单扩展、tooltip 阈值与配置联动）、Inc3 有证据局部纠正、Inc4 人工锚点。生产 fixture 原料在 docs/sessions/260728-1050-q7m2/fixtures/raw/（已 gitignore）。相关：[[feedback-codex-implementer]]。
