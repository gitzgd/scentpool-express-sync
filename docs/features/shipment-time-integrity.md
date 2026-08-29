# 发货与签收时间完整性

- 状态：时间完整性约束已于 2026-08-29 部署，生产历史数据未修复
- 关联决策：[`ADR-0006`](../decisions/ADR-0006-shipment-time-evidence-and-controlled-repair.md)

## 不变量

- `status` 为“已发货”或“已签收”时，`shipped_at` 必须可解析且不早于 `created_at`。
- `status` 为“已签收”时，`tracking_signed_at` 必须可解析且不早于 `shipped_at`。
- 每个非空业务时间同时保存 `quality` 和固定 `source`。`exact` 表示供应商事件或人工明确输入；`estimated` 表示查询、面单或状态观察时间；旧值在没有可靠来源分类时标为 `legacy_unclassified`。
- SQLite 触发器拒绝新写入违反上述条件，但不在部署迁移时自动猜测或改写已有坏行。

## 写入路径

- 物流运输中：待处理订单推进为已发货；缺少发货时间时使用本次查询观察时间并标为 `estimated/tracking_check_observed_at`。
- 物流签收：供应商轨迹时间标为 `exact/provider_event`；供应商没有事件时间时使用查询时间并标为 `estimated`。缺少发货时间时只把签收时间作为发货上界估算，不伪装为精确发货时间。
- 人工状态：带时区的明确发货时间标为 `exact/manual_input`；仅改变状态时使用操作观察时间并标为 `estimated/manual_status_observed_at`。
- 电子面单成功：下单成功时间是系统精确观察到的面单事件，但只是实际发货时间的估算，保存为 `estimated/label_success_observed_at`。
- 取消成功：恢复待处理并同时清空发货、签收时间及证据字段。重新下单生成新请求标识，成功后重新建立时间证据。
- 旧库“待处理但已有单号”兼容迁移：沿用既有状态纠正，用合法 `updated_at` 或迁移观察时间补发货时间，并明确标为 `estimated/legacy_status_updated_at`；其他历史异常不自动修复。

## 历史 dry-run 与 apply

命令默认只读，只输出问题类别、精确可修复、估算可修复、证据冲突/不可安全修复数量和预览指纹；不输出内部记录号、业务编号、订单号、快递单号、姓名、电话、地址或原始报文。

```bash
python3 manage.py --db /绝对路径/scentpool.db repair-shipment-times
```

证据优先级：供应商签收轨迹；既有物流查询时间；电子面单成功/请求时间；状态更新时间。`legacy_snapshot` 只证明 2026-08-17 迁移时的状态，绝不作为历史发货或签收时间候选。

写入必须再次明确数据库绝对路径、预览指纹、最大记录数、全新备份路径和固定确认短语。估算记录默认不写入，只有增加 `--include-estimated` 才授权。

```bash
python3 manage.py --db /绝对路径/scentpool.db repair-shipment-times \
  --apply \
  --preview-fingerprint <dry-run输出> \
  --max-rows <批准上限> \
  --backup-output /受控目录/scentpool-before-time-repair.db \
  --confirm-db-path /绝对路径/scentpool.db \
  --confirm APPLY_SHIPMENT_TIME_REPAIR
```

执行顺序固定为：Schema/路径/指纹预检、在线备份、备份完整性检查、`BEGIN IMMEDIATE`、事务内重算并再次比对预览指纹、检查行数上限、写业务字段与只追加修复审计、统一提交。任何一步失败都回滚业务事务；若提交后需要撤销，进入维护窗口并恢复命令已验证的修复前备份。生产执行属于独立授权动作。

## Schema 与隐私

`shipments` 增量增加四个无个人信息字段：`shipped_at_quality/source` 与 `tracking_signed_at_quality/source`。`shipment_time_repair_events` 只保存内部整数记录号、字段名、变更前值摘要、替换时间、证据等级/来源、预览指纹、批次和执行时间；触发器禁止更新或删除。旧字段内容不进入审计，因此即使异常文本被误写进时间列也不会扩散；表中不保存业务编号、订单号、快递单号、个人字段或第三方原始报文。

## 测试

`shipment_time_integrity_test.py` 使用临时合成数据库覆盖物流推进、直接签收、供应商有/无签收时间、人工状态、面单成功、取消、旧库兼容、非法格式、时区、倒序、证据冲突、dry-run 零写入、估算授权、幂等、行数上限、指纹变化、备份失败和完整性检查失败。`smoke_test.py` 会调用该专项测试。
