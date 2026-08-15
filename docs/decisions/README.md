# 决策记录

本目录保存需要跨任务延续的架构、数据和运维决策。文件使用 `ADR-NNNN-短标题.md` 命名。

每份 ADR 包含：

- 状态：提议、接受、替代或废弃。
- 日期和背景。
- 决策内容。
- 影响和约束。
- 何时重新评估。

当前记录：

- [`ADR-0001-sqlite-single-instance.md`](ADR-0001-sqlite-single-instance.md)：单实例阶段继续使用 SQLite。
- [`ADR-0002-provider-confirmed-label-cancellation.md`](ADR-0002-provider-confirmed-label-cancellation.md)：只有供应商确认后才释放电子面单。
- [`ADR-0003-default-delivery-and-local-acceptance.md`](ADR-0003-default-delivery-and-local-acceptance.md)：普通改动默认提交、发布，并提供合成数据本地验收界面。
- [`ADR-0004-daily-audit-readonly-boundary.md`](ADR-0004-daily-audit-readonly-boundary.md)：日报使用独立令牌、独立只读连接和汇总白名单。
- [`ADR-0005-forward-only-audit-state-events.md`](ADR-0005-forward-only-audit-state-events.md)：用脱敏只追加状态事件提供前向完整的历史日报，迁移前数据保持部分口径。
