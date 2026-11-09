### 里程碑 1：TDD 红
- 当前阶段：implementing / 回归测试先行
- 本段结论：新增真实落盘假 HTTP 响应测试，覆盖已有部分文件遇到 200 全量响应、无 Content-Length 空响应、无长度非空文件的媒体校验，以及基类 Content-Length 不符清理。Base commit 上四个测试全部失败，证明判据有效。
- 关键决策与已否决方案：用临时目录和真实文件字节断言，不仅检查返回值；不采用 stashing 或手工注入红验。
- 下一步唯一动作：实现基类与通用下载器的严格完整性行为。

### 里程碑 2：基线上限回归红
- 当前阶段：implementing / 回归测试先行
- 本段结论：补充 generic 流式体积上限的落盘清理判据；在独立 detached worktree 的 Base commit 上运行全部五个新测试，五项均失败，确认新增测试有效。
- 关键决策与已否决方案：基线红验在独立 worktree 执行，未使用 git stash；运行结束后销毁临时 worktree。
- 下一步唯一动作：提交新增上限判据，再提交生产实现。

### 里程碑 3：完整性实现转绿
- 当前阶段：implementing / 实现完成
- 本段结论：generic 移除续传并逐次从头覆盖，校验长度、非空、流式体积上限和媒体有效性；BaseDownloader 长度不符会记录 DOWNLOAD_LENGTH_MISMATCH、删除临时文件并进入现有失败重试。窄测全绿。
- 关键决策与已否决方案：不改 media_resolver；因 SSRF 传输测试使用无效微型假媒体，获批 seq=1 后仅在该测试 helper 中隔离媒体 probe。
- 下一步唯一动作：提交实现后完成接受检查与范围自检。

### 里程碑 4：测试隔离迁入 sibling fixture
- 当前阶段：implementing / 收敛文件闸
- 本段结论：提交闸拒绝增长 1037 行的 SSRF 测试模块，拒绝修改已撤销；seq=2 获批在 tests/unit/downloaders/conftest.py 增加仅作用于 test_generic_ssrf 的自动 fixture，遵守 request.module 名称限定。
- 关键决策与已否决方案：不压行或删除测试绕过文件体积 ratchet；不用无条件全目录 stub。
- 下一步唯一动作：提交实现与隔离 fixture，再运行最终接受检查。

### 里程碑 5：Content-Encoding TDD 红
- 当前阶段：implementing / 编码长度回归先行
- 本段结论：新增 BaseDownloader 与 GenericDownloader gzip 解码长度测试，并将既有基类长度不符测试改为混合大小写的 identity 编码；当前 HEAD 上两条 gzip 测试均因压缩长度与解码字节不同而失败。
- 关键决策与已否决方案：测试以 gzip 的真实压缩长度作 Content-Length、以 iter_content 返回解码后的字节写盘；不在测试里模拟编码逻辑或预先改生产代码。
- 下一步唯一动作：单独提交 TDD 红测试，再提取共享 Content-Encoding 判定并复用。

### 里程碑 6：编码长度判定修复
- 当前阶段：implementing / 实现完成
- 本段结论：在 base.py 增加共享 Content-Encoding 判定，base 与 generic 仅在编码缺失或 identity 时比较 Content-Length；其他编码仍按解码流执行非空、媒体与流式体积限制。
- 关键决策与已否决方案：不对压缩长度做体积上限预检，因为它不代表 iter_content 解码字节；保留实际解码流累计上限。
- 下一步唯一动作：提交修复后运行 gzip、identity 与原窄测验收。

### 里程碑 7：修正 gzip 样本压缩比判据
- 当前阶段：implementing / 回归样本精化
- 本段结论：复核发现原 gzip 样本实际压缩长度 68 大于解码字节 54，不符合反馈要求；改为 2048 解码字节、52 字节压缩长度，并在未修复的 detached HEAD `0ef7cc26` 上验证 base 与 generic 两项均红。
- 关键决策与已否决方案：不重写已 push 的 TDD/实现历史；保留原 TDD 红提交并以前向测试修正提交补齐方向性判据。
- 下一步唯一动作：提交样本修正，再运行最终窄测并同步报告。
