# B 卡进度

## 当前阶段

implementing；完成第一份独立可运行单元：bounded metadata decoder 与Unicode/字段约束测试通过。尚未实现 HTTP 接收、队列/SQLite 原子交接、worker readiness、实际integration验证。

## 本段结论（≤3句）

- metadata 使用有界 UTF-8 base64url JSON；显示文件名规范化为 basename，不作为路径；处理选项沿用既有 normalize_processing_options。
- `tests/unit/test_upload_routes.py` 通过，初始失败为值差异 AssertionError，修复后全绿。
- 尚无验证失败归因或生产环境结论。

## 关键决策与否决方案

- 不消费客户端文件hash；元数据只声明正字节数，正文由服务端流式计数/hash。
- source_url 只接受无userinfo的 http/https 文本，不抓取。

## 下一步唯一动作

扩展 CacheManager 持久 admission/root/media mapping 与 runtime 限额/预留，再接上真正 HTTP stream 和 dispatcher 测试。
