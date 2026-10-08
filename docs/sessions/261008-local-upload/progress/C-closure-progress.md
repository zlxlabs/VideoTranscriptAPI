# C closure progress

已将上传源预算从固定两倍改为源文件预留，并把 CapsWriter 文本/兼容 JSON 的 UTF-8 原子写临时峰值接入同一 Runtime 预留；真实集成测试已证明两个产物均在送入大模型前清理。FunASR 说话人分支已跑到大模型队列边界，实际任务目录只观测到源文件；历史 `source=url` 已在 SQL 的 COUNT/LIMIT/OFFSET 前排除 `local_upload`，通知 False 与异常边界均有真实 HTTP 断言。下一步只做最终窄验证、类型/语法核对、差分预算和远端干净状态收口。
