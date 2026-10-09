# 本地上传运维检查与恢复边界

本说明只覆盖软件侧工具与安全操作顺序，不构成生产容量、恢复或启用证明。当前 M6 软件工具可以在本地沙箱演练；生产入口、磁盘/代理容量、ASR 最大工作集、独立恢复域与部署授权仍未知，必须保持上传开关关闭。

## 真实消费路径

- API 通过 `load_and_validate_config` 读取 `/app/config/config.jsonc`；Docker consumer 将宿主配置目录挂到该路径。四项 `storage.upload_limits` 必须为正有限值（并发为正整数）；示例配置保留 `null`，不会把实验数字写入生产额度。
- `docker/docker-compose.yml` 的服务环境来自可选 `docker/ops.env`，数据与配置分别挂在 `../data`、`../config`。部署 compose 的环境源是部署域 `.env`，配置与数据挂载为 `./config`、`./data`；仓内 `docker/deploy_targets.json` 指向 `/opt/media/VideoTranscriptAPI`。
- `VTA_UPLOADS_ENABLED` 是进程环境变量，不是 JSON 配置，也不在数据卷里。上传接收实际要求值精确等于小写 `true`；未设置、空值及其他值均关闭。恢复 `data/` 不得覆盖部署域 `.env`。Resolver 与接收入口都要检查该 gate。
- 当前工具不读取 `.env`，只从 tracked compose 描述中报告 `env_file` 文件名。检查 `.env` 时仅可用单键 `grep`，不得 `source` 整个文件；不得打印完整环境或配置。

## 本地诊断

只对已知配置路径运行；不要把沙箱样例数写成生产额度：

```bash
uv run --frozen python scripts/ops/local_upload_check.py \
  --config <consumer-config-path> \
  --data-dir <consumer-data-mount> \
  --compose <tracked-deploy-compose-path> \
  --repo <source-checkout> \
  --source-sha <artifact-attested-full-source-sha> \
  --sample <known-local-media-file>
```

工具使用真实配置解析器、只读取配置中的有限额度字段、目标文件系统剩余空间及可选样本的 `ffprobe` 时长/大小；不会打印配置秘密、读 dotenv、启用/部署/恢复数据。兼容判据是 Git 对象中已存在的 commit ancestry，最低 A guard-only SHA 为 `6af391d20edab8dfd8b320ce6b91d4d19e3260e2`；更早的 pre-A `38b299468daaf309608f46ffe1c59886b0922c04` 应拒绝。 ancestry 只核验源码祖先，不证明 OCI digest 与源码 SHA 的绑定，也不证明真实旧 image 行为。

无论本地检查多绿，CLI 都以 `UPLOAD_ENABLE_BLOCKED` 结束：当前尚未提供生产四限额实测、独立恢复域证据及生产 ingress/image 证明。不可将 gate 设置为 true，也不能用该诊断输出代替部署授权。

### 有界本地接收实验

```bash
uv run --frozen python scripts/perf/local_upload_capacity.py --duration-seconds 20
```

命令会报告 source SHA 与工作树 clean/dirty 状态；只有 clean checkout 的观测才可归因到精确提交。它会自行新建并回收一个 OS `mkdtemp`，从 tracked 示例生成仅用于实验的配置，服务只绑定 loopback，数据、用户和日志都写该临时目录。脚本用原始 HTTP 向真实 FastAPI upload receiver 发送两份 20 秒、PCM 16-bit mono/48 kHz 合成 WAV，运行真实 `ffprobe` 与当前 worker；同时用受控 loopback HTTP fixture 做 URL 下载。ASR 对端被替换成 loopback WebSocket 拒绝器，worker 应显式失败，不调用外部 ASR/LLM/通知服务。脚本比对服务端 SQLite 中每份实际字节数和 SHA-256，并测量 10ms 采样间隔下临时目录观察峰值。

这是一个有明确大小/时长/并发上限的单机诊断，不测真实生产硬盘配额、反代缓冲、ASR/LLM 峰值与最大时长；观察峰值不是预留量，也不得回填四项限制。任何实验中的未知或异常都应中止并查因，不能扩限重跑直到变绿。

## 安全恢复顺序（仅获授权后）

1. 在部署项目根 `/opt/media/VideoTranscriptAPI` 只查询开关一键，不读/打印整个 `.env`：`grep -n '^VTA_UPLOADS_ENABLED=' .env`；缺键、空值或非精确 `true` 都是关闭态。检查返回非零时应区分“键不存在”与“值不同”。如果值为 true，须先由获准操作者在部署域将该单键改成 false，并重新 grep 核实；不得 `source .env`。
2. 服务保持停止：`docker compose -f docker-compose.yml --project-directory /opt/media/VideoTranscriptAPI --env-file /opt/media/VideoTranscriptAPI/.env stop video-transcript-api`。只用授权恢复工具把 cache DB 与对应正文文件恢复到已知空目标；严禁覆盖部署目录 `.env`、服务 unit 或其他卷。不得使用本卡工具自动恢复任何数据库。
3. 核对恢复来源确实包含撤销后的权威状态，并检查 DB 与正文/必要映射完整。仅有撤销前 snapshot 时，恢复会带回旧 `revoked_at=NULL`；本地行无法识别完整卷回退。没有独立、可信且可核验的撤销来源时，停止在关闭态，不可声称撤销未复活。
4. 仍保持环境 gate 关闭，启动隔离消费方做只读核对：真实 Resolver 拒绝被撤销记录，cache/task cleaner 保留有效 `never` 与有效 30d 正文及必要 root 映射，原媒体仍按既有临时清理规则处理。恢复后的服务只有在独立部署授权后才可启动；在证据齐全前不要把 gate 改回 true。本文测试只在 pytest `tmp_path` 沙箱验证，未启动 systemd 或生产服务。
5. 只有 M4/M5 完成、部署 image 具有可验证 source SHA、A guard-only 或更新版本通过真实消费回归、独立恢复域/撤销状态可证明、四限额在实际磁盘/ASR并发/URL负载下测得、真实代理/ingress 条件已核对且获得单独部署授权后，才可由授权操作者评估设置开关。任一项 unknown 就继续 off。

## 当前证据状态

- 已实现并测试：独立 SQLite+正文生产者生成 snapshot；撤销后将 sandbox 恢复到撤销前副本；fresh Resolver 通过独立 `VTA_UPLOADS_ENABLED=false` 子进程环境拒读；current cache/task cleaner 对有效长期成果及 root 保持保护。
- 已用真实 A guard-only SHA 的源树作为独立 subprocess consumer，消费当前 producer 生成的 SQLite+文件；启用 sandbox gate 时读取合法 active share，legacy upload-token/blank token 别名拒绝，两个 cleaner 均保留旧龄有效长期结果。pre-A 版本由 commit ancestry 判据拒绝。
- 本机单次 synthetic 探针数据只代表当次硬件、依赖与临时文件系统；ASR 被 loopback rejector 拒绝，ASR 容量、代理缓冲与真实生产并发未知。脚本报告为 blocked，不将该样本作为任何生产额度。
- 未执行：真实生产 ingress、配置/数据卷、systemd 身份与 mount、生产备份恢复、真实磁盘配额、真实 ASR/LLM 工作集或用户部署授权。不得把未执行改写成 passed。
