<!-- delegate-outcome: succeeded -->
# PR235 quality Tests 失败诊断（dlg-20261012-130255-ad9507）

## 结论

`test_signal_after_candidate_start_rolls_back` 在 CI（run 38249678955 job 114812422440，head
7fd7013b）的 `:218 assert 0 != 0` 是**既有测试的时序竞态塌缩**，非本 PR 新增代码影响、非部署
脚本缺陷。脚本读到 healthy 后按规格进入提交临界区（`trap '' INT TERM`）吞掉 TERM 或已成功
退出 0，此时测试才发 TERM，断言前提（TERM 先于「健康确认/提交」被脚本消费）已不成立。同 head
草稿 run 38247412597（同池 self-hosted slot-51）Tests 通过、本次 slot-85 失败，即竞态两向各
命中一次。本地 30/30 轮全部通过，失败未复现（本地调度下测试侧 TERM 必先到达）。

## 确定事实

- diff base..head 不含 `docker/pull_and_deploy.sh` 与 `tests/deployment/test_pull_and_deploy.py`（独立核实，仅 cache_manager.py、unit 测试、两个文档）；主干基线 gh api 失败，继承红/新红判定：**非本 PR 引入，继承红（未能判定到更早引入点）**。
- CI 日志（/tmp/vta-pr235-quality-run38249678955.log，有界 grep）：唯一 FAILED 即该测试，Popen rc=0，runner 路径 `/_work/`；两 run 同 gatehub self-hosted 池，仅 slot 号不同。
- 取证（/tmp/vta-ci235-diag-scratch/，插桩副本+逐字保留真实 fake-docker fixture，未动主树）：
  - 143 路径（base1/inj1/expSysd3/expBare）：首次探测读到 `starting`→sleep→TERM 处理→回滚旧镜像、`.deploy-image` 保持旧值，事件序完整。
  - rc=0 路径（expC）：探测读 healthy→committed 候选→exit 0，TERM 送达时进程已死；expD：TERM(+0.5s) 落入 commit 窗口（受控延迟拉宽验证）被 `trap ''` 丢弃→committed→exit 0。docker argv/image、health 文件字节、`.deploy-image` 终态、事件时间线均已记录。
- 环境验证：systemd-run 用户单元与 env -i 裸 shell（Python 3.12.3/bash 5.2.21）各跑一次，行为与交互会话（Python 3.11.15）一致；身份变量仅记存在性/长度，无值泄露。

## 根因定性

测试用「docker.log 出现 up -d」推断「健康确认/提交前」，但 up -d 与提交之间仅隔两次 fake
docker 调用；慢 runner 上 fork 延迟把「读到 healthy→trap '' 生效」推进到测试发 TERM 之前，
窗口塌缩。脚本注释已自认 up 后存在信号竞态并以 trap '' 保护提交原子性，行为符合规格
（TERM 在健康确认/提交前到达→143+回滚，已实验钉死）。

## 最小下一步（建议，未落码）

仅改 `tests/deployment/test_pull_and_deploy.py::test_signal_after_candidate_start_rolls_back`
用到的 `_fake_docker`：给 `inspect --format={{.State.Health.Status}}` 分支加受控延迟开关（同
`COMPOSE_UP_DELAY` 模式，默认 0），测试先 terminate 再放行探测。锁死不变式：TERM 严格先于
健康确认与状态提交被脚本消费时，脚本必须 143+回滚。若要改变生产行为（如提交后窗口），超出
本卡范围，停报主脑。

## 取证工件

- 插桩脚本/probe/事件日志：/tmp/vta-ci235-diag-scratch/（pull_and_deploy.sh 副本与 HEAD 逐字节 diff 为空后插桩，probe.py 含事件时间线）
- CI 日志（私有）：/tmp/vta-pr235-quality-run38249678955.log
- GitHub API 用量：4 次（job 两则、jobs 列表、runner 白名单字段）
