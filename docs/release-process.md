# 发版流程（全手动）

本项目采用全手动发版：一条取材命令 + 人工整理 CHANGELOG，不引入 Conventional Commits、release-please 或任何发布自动化。版本号以 1.0.0 为基线，散布在 `pyproject.toml`、`package.json`、`e2e/package.json`、`src/video_transcript_api/__init__.py` 与 `src/video_transcript_api/api/app.py`，发版时人工同步。

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

### 2. 打 annotated tag

CHANGELOG 条目经日常流程进主干后，在主干 HEAD（即本次发版提交）上打 annotated tag 并推送：

```sh
git tag -a vA.B.C -m "vA.B.C"
git push origin vA.B.C
```

tag 命名固定 `v` 前缀 + 三段版本号，供下一次取材命令引用。

### 3. 建 GitHub Release

仓库是 public，GitHub Releases 页对外可见。打完 tag 后由发版人手动建 Release：tag 名与 git tag 一致（`vA.B.C`），body 从 `CHANGELOG.md` 对应版本条目摘取（单一生成源，禁止另写一份）。把 `## [A.B.C]` 条目正文写入临时文件后执行：

```sh
gh release create vA.B.C --title "vA.B.C" --notes-file notes.md
```

临时文件用完即删，不要提交。

### 4. 构建与部署

按现有镜像流程：`docker/push_to_ghcr.sh` 在干净工作树上构建并推送 `ghcr.io/zj1123581321/video-transcript-api:<sha12>`，随后 `docker/pull_and_deploy.sh <镜像引用>` 以不可变 digest 部署到目标机。

## 两条红线

1. **禁止给镜像打浮动 tag**（含 `:latest`、`:vA.B.C` 等任何会被后续推送覆盖的引用），镜像 tag 只允许 sha12 唯一。机理：`docker/Dockerfile` 构建时通过 `COPY --from` 从仓库里一个冻结的旧镜像获取 BBDown 二进制；一旦出现浮动 tag，下次构建会把它静默解析到新镜像，BBDown 的来源就被悄悄换掉。`tests/deployment/test_pull_and_deploy.py` 已锁定该行为。
2. **部署 digest 链不动**：`docker/pull_and_deploy.sh` 与 `docker/docker-compose.deploy.yml` 的不可变 digest 约束（`${VIDEO_TRANSCRIPT_IMAGE:?...}` 强制 digest 引用）是部署可追溯性的根基，禁止改成浮动引用或绕过校验。

出现第三方镜像消费者时，浮动 tag 政策需重新裁决后才能放开。
