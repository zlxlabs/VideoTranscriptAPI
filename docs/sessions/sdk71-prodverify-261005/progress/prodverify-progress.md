# 生产验证进度

## 2026-10-05 现场接手与设计落盘
- 当前阶段：implementing，部署前只读核查。
- 本段结论：在指定 worktree `card/sdk71-prodverify-261005`、HEAD `a21d9729b15dac3df2cc0e595dea5426a2d1718f` 接手，树初始干净且派发锁为本任务。n305 当前 `/opt/media/VideoTranscriptAPI/.deploy-image` 与容器配置镜像均为 `ghcr.io/zj1123581321/video-transcript-api@sha256:98681b3c05a82727fe5559e526c220db68047a14d9f55151105406e8ab3fb21a`，容器 `running healthy`，启动时间 `2026-10-05T10:09:37.254997782Z`；本段尚未部署。
- 关键决策与已否决方案：按卡面将设计落盘；部署唯一入口仍为 `pull_and_deploy.sh`。禁止使用 4 小时 51 分录制做 C3，禁止手工 compose 部署或修改生产配置。
- 下一步唯一动作：读取项目部署记忆并核实合格的 ≤15 分钟直播录制素材及其 API 可用性。

## 2026-10-05 部署前核查完成
- 当前阶段：implementing，准备构建候选镜像。
- 本段结论：重新核实旧镜像 digest 为 `sha256:98681b3c05a82727fe5559e526c220db68047a14d9f55151105406e8ab3fb21a`、容器 healthy、SDK 旧文件哈希为 `eccec1a69b81c4a2360d33e15f0ddb8adffb85725dc93d41f8e3150a0863f6c7`。部署前文件哈希为 config.jsonc=`a349b81837e0cb06e2ce3dd8ff670df7e4c107b00e911a8b60670a8a334c25cc`、users.json=`237cefb5a2d3bda3ea4e6a227750c348f34475cba1dd0fa804c82458cf28f7ad`、.env=`5885382b22606aafaa4aeb55d529d56739c6555347286183dcae4ddf577b3b1e`、docker-compose.yml=`7b0a89b50de84f71c480c8ef7398d64d479bbaf429805b9401d748294e498188`。真实视频号录制 `250f1810fba9422dbdee6f1a3c236763` 时长 196.224 秒，生产端真实 API/SDK 任务此前没有该源标识日志命中；公网 HTTPS `/livez` 当前基线返回 HTTP 000（TLS 错误），n305 容器内 `/livez` 返回 200。
- 关键决策与已否决方案：不覆盖服务器 compose；其与部署模板仅有既有 `stop_grace_period: 30s` 差异，候选脚本以 digest 检查现有 compose 并按原样保留。C2 将从上述真实录制切 90 秒；C3 使用对应 `recorder://` URL 和录制器文件直链，转录整段 196.224 秒素材。
- 下一步唯一动作：在生产容器中验证该内网下载地址通过现有白名单，再按指定 detached 源树构建并推送 `a21d9729b15d`。

## 2026-10-05 18:43:59 CST 部署前检查点
- 当前阶段：implementing，即将切换生产镜像。
- 本段结论：在干净 detached worktree `sdk71-build-a21d9729-261005`、HEAD `a21d9729b15dac3df2cc0e595dea5426a2d1718f` 执行 `BUILDX_NO_DEFAULT_ATTESTATIONS=1 ./docker/push_to_ghcr.sh`，退出码 0。GHCR 远端 tag `a21d9729b15d` 实测 digest 为 `sha256:e1d6339d9dc3e8c391d1b5f61560a86f43dd02cacaaa90251694cb3ba283486d`；镜像 `GIT_SHA=a21d9729b15d`。n305 部署前 `.deploy-image` 和容器镜像仍为回滚点 `ghcr.io/zj1123581321/video-transcript-api@sha256:98681b3c05a82727fe5559e526c220db68047a14d9f55151105406e8ab3fb21a`，容器 `running healthy`，SDK 哈希仍为旧值 `eccec1a69b81c4a2360d33e15f0ddb8adffb85725dc93d41f8e3150a0863f6c7`；四个配置哈希与部署前基线相同。
- 关键决策与已否决方案：已知健康检查为 `ssh n305 'docker ps --filter name=video-transcript-api'` 加 `/livez`；已知回滚命令为 `ssh n305 '/opt/media/VideoTranscriptAPI/docker/pull_and_deploy.sh ghcr.io/zj1123581321/video-transcript-api@sha256:98681b3c05a82727fe5559e526c220db68047a14d9f55151105406e8ab3fb21a'`。不手工运行 compose，不改生产配置。
- 下一步唯一动作：运行卡面指定的 `ssh n305 '/opt/media/VideoTranscriptAPI/docker/pull_and_deploy.sh ghcr.io/zj1123581321/video-transcript-api:a21d9729b15d'`。

## 2026-10-05 18:45 CST 部署完成检查点
- 当前阶段：implementing，生产已切到候选镜像，开始判据验证。
- 本段结论：`ssh n305 '/opt/media/VideoTranscriptAPI/docker/pull_and_deploy.sh ghcr.io/zj1123581321/video-transcript-api:a21d9729b15d'` 退出码 0（远端状态时间 2026-10-05T10:45:02Z）。`.deploy-image`、容器 Config.Image 与 GHCR tag 均指向 digest `sha256:e1d6339d9dc3e8c391d1b5f61560a86f43dd02cacaaa90251694cb3ba283486d`；容器 `running healthy`，StartedAt=`2026-10-05T10:44:40.019437247Z`。容器 SDK `client.py` 哈希已变为目标 `0490b5f877917e55f995e5ea9a81da226b47cda52e406bf361207c70550eb01b`，镜像 Config.Image 对应 image ID 与候选 tag 的 image ID 相同；公网 `https://sum.zlxlabs.com/livez` 返回 200。部署后 config.jsonc、users.json、.env、docker-compose.yml 四个哈希与部署前逐字节一致。
- 关键决策与已否决方案：健康、哈希和配置不变均满足，不触发回滚；后续 C1/C2 仍直接在此生产容器运行。
- 下一步唯一动作：准备真实录制的 90 秒音频切片并执行 C1 连接拒绝判据。

## 2026-10-05 18:47 CST C1 完成
- 当前阶段：implementing，生产 SDK 探针进行中。
- 本段结论：生产容器内新 SDK 对拒绝连接按默认预算路径返回 `AsrError(code=connection_lost)`，实测 0.009 秒，命令退出码 0。C2 的 91.557 秒 WAV 来自真实视频号直播录制，producer 文件与容器内副本 SHA256 相同（`36161dd2…`）；容器仍 healthy、新 SDK 哈希正确。
- 关键决策与已否决方案：C1 仅说明升级没有破坏连接拒绝路径；仓内既有证据说明旧 pin 也会秒级绿，不将其解释为升级修复。
- 下一步唯一动作：使用生产容器里的同一 SDK 对真实 91.557 秒音频调用默认 `transcribe_file_sync`，全程不传 `deadline_total` 并等待终态返回。
