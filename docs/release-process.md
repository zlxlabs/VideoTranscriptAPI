# 发版与部署流程

CHANGELOG、annotated tag 与 GitHub Release 按下述人工流程维护；应用改动推送到 `main` 后由部署工作流自动构建并部署（[DEPLOY.md:5](../docker/DEPLOY.md)）。版本号以 1.0.0 为基线，散布在 `pyproject.toml`、`package.json`、`e2e/package.json`、`src/video_transcript_api/__init__.py` 与 `src/video_transcript_api/api/app.py`，发版时人工同步。

## 发版时机

绑定部署：需要把主干部署到 n305 时发一版。不为发版而发版——没有部署需求就不打 tag、不发 CHANGELOG。

## 发版步骤

以下假设上一版是 `vX.Y.Z`，本次发 `vA.B.C`。

### 1. 取材

在主干 checkout 上跑取材命令，得到自上一版以来每个合并提交的正文（GitHub 会把 PR 标题写进合并提交正文，这就是变更素材）：

```sh
git log --first-parent --merges vX.Y.Z..HEAD --format='%b'
```

把素材人工整理成用户可见变更，写入 `CHANGELOG.md` 的新条目（Keep a Changelog 风格）。CHANGELOG 只回答「版本 → 用户可见变更」，不搬运 WORKLOG 或验收流水。

### 2. 自动构建与部署

应用改动推送到 `main` 后，由 `.github/workflows/deploy.yml` 自动构建并部署；该工作流对 `**.md`、`docs/**`、`.github/**` 配置了忽略路径，因此仅修改文档或 CI 文件不会触发生产部署（[deploy.yml:14-23](../.github/workflows/deploy.yml)、[DEPLOY.md:5](../docker/DEPLOY.md)）。部署入口、运行细节和灾备步骤见[部署流程与入口链路](../docker/DEPLOY.md)。

判定部署镜像状态时区分以下两项，不要互相替代：

- **实际运行版本**：在部署主机上查看运行容器的 `.Image`，例如 `docker inspect --format='{{.Image}}' video-transcript-api`。工作流也以运行容器 `.Image` 与候选镜像 ID 对账（[deploy.yml:164-201](../.github/workflows/deploy.yml)）。
- **最近验证成功镜像 / 回滚锚点**：部署目录 `.deploy-state/last_verified_good`。工作流在验证通过后写入运行容器镜像 ID（[deploy.yml:242-252](../.github/workflows/deploy.yml)），回滚时读取该记录（[deploy.yml:307-318](../.github/workflows/deploy.yml)）；它表示最近验证成功的镜像，不代表当前容器必然仍在运行该版本。

`.deploy-image` 已废弃，当前部署与回滚链路不再使用它；判断运行版本只看运行容器 `.Image`，判断回滚锚点只看 `last_verified_good`（现行判定逻辑见 [deploy.yml:164-201、242-252、307-318](../.github/workflows/deploy.yml)）。

### 3. 打 annotated tag

服务确认正常、部署成功后再打 annotated tag 并推送；tag 落在部署所对应的主干提交上：

```sh
git tag -a vA.B.C -m "vA.B.C"
git push origin vA.B.C
```

tag 命名固定 `v` 前缀 + 三段版本号，供下一次取材命令引用。顺序不能反：tag 和 GitHub Release 都对外可见，先打就把一个尚未上线的版本公开了。

### 4. 建 GitHub Release

仓库是 public，GitHub Releases 页对外可见。部署成功并打完 tag 后由发版人手动建 Release：tag 名与 git tag 一致（`vA.B.C`），body 从 `CHANGELOG.md` 对应版本条目摘取（单一生成源，禁止另写一份）。把 `## [A.B.C]` 条目正文写入临时文件后执行：

```sh
gh release create vA.B.C --title "vA.B.C" --notes-file notes.md
```

临时文件用完即删，不要提交。
