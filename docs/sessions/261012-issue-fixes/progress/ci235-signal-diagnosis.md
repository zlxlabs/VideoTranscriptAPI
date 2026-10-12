<!-- delegate-outcome: succeeded -->
# PR235 quality Tests 失败诊断（dlg-20261012-130255-ad9507 / dlg-20261012-133014-566fcd）

## 结论

CI 失败样本（run 38249678955 job 114812422440，head 7fd7013b，`:218 assert 0 != 0`）中只观测到
部署脚本以 0 退出；TERM 相对「健康确认/状态提交」的具体抵达时刻**未观测**，不能断言该样本命中
哪条时序。已确证：

- 本地原样复跑 30/30 通过，失败未复现；本地与 CI 在调度与 runner 上不等价，不据此断言 CI 假红。
- 受控实验（脚本副本插桩+真实 fake-docker fixture，主树零变异）确认两种时序均可行：① 探测读到
  starting→TERM 被消费→143+回滚旧镜像；② 探测读到 healthy→`trap '' INT TERM` 提交保护吞掉
  TERM 或进程已退出→exit 0。现测试以「docker.log 出现 up -d」推断「提交前」，缺少阶段同步，
  两种时序都可能出现。
- 同 head 草稿轮 Tests 通过、ready 轮失败，仅证明结果不稳定；同宿主不同 slot、labels 相同不
  证明两次执行环境等价。

## 确定事实

- diff base..head 不含 `docker/pull_and_deploy.sh` 与 `tests/deployment/test_pull_and_deploy.py`（独立核实，仅 cache_manager.py、unit 测试、两个文档）；主干基线 gh api 失败，继承红/新红判定：**非本 PR 引入，继承红（未能判定到更早引入点）**。
- CI 日志（/tmp/vta-pr235-quality-run38249678955.log，有界 grep）：唯一 FAILED 即该测试，Popen rc=0，runner 路径 `/_work/`；两 run 同 gatehub self-hosted 池，仅 slot 号不同。
- 取证（/tmp/vta-ci235-diag-scratch/，插桩副本+逐字保留真实 fake-docker fixture，未动主树）：
  - 143 路径（base1/inj1/expSysd3/expBare）：首次探测读到 `starting`→sleep→TERM 处理→回滚旧镜像、`.deploy-image` 保持旧值，事件序完整。
  - rc=0 路径（expC）：探测读 healthy→committed 候选→exit 0，TERM 送达时进程已死；expD：TERM(+0.5s) 落入 commit 窗口（受控延迟拉宽验证）被 `trap ''` 丢弃→committed→exit 0。docker argv/image、health 文件字节、`.deploy-image` 终态、事件时间线均已记录。
- 环境披露：受控 systemd-run 用户单元与 env -i 裸 shell 复试用 Python 3.12.3，与 CI/目标测试的
  3.11 系列不同版本，结论仅称「行为方向一致」，不称完全等价；身份变量仅记存在性/长度，无值泄露。

## 候选根因（未证）

测试与脚本对「健康确认→状态提交」窗口无阶段同步（「docker.log 出现 up -d」与提交之间仅隔
两次 fake docker 调用）；受控实验以受控延迟复现了窗口塌缩方向。但 CI 失败样本未观测信号
时刻，「塌缩即该样本根因」未证。TERM 在健康确认/提交前到达→143+回滚，这一脚本行为已实验
钉死。

## 最小下一步候选（未验证，不落码）

候选（非定案）：改 `tests/deployment/test_pull_and_deploy.py` 的 `_fake_docker`，给
`inspect --format={{.State.Health.Status}}` 分支加受控延迟开关（默认 0），该测试先 terminate
再放行探测。未验证点：增加延时不自动保证 TERM 先于健康确认（所需窗口量级未测）；修复验证须含
变异校验——删除/变异脚本对 TERM 的处理时，目标断言必须变红且红因是信号失败（非零退出+回滚），
不能由健康失败路径替代。后续卡契约由主脑/Fable 决定，本卡不改代码。

## 取证工件

- 插桩脚本/probe/事件日志：/tmp/vta-ci235-diag-scratch/（pull_and_deploy.sh 副本与 HEAD 逐字节 diff 为空后插桩，probe.py 含事件时间线）
- CI 日志（私有）：/tmp/vta-pr235-quality-run38249678955.log
- GitHub API 用量：取证轮 4 次（job 两则、jobs 列表、runner 白名单字段）；表述修正轮 0 次
