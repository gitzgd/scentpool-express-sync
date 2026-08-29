# 脱敏只读业务日报

- 状态：基础版生产已启用（2026-08-05）；历史日末与脱敏失败事件扩展已上线（2026-08-17）；连接/打印/延迟兼容及时间证据等级代码已完成但未部署（2026-08-29）
- 接口：`GET /api/admin/system/daily-audit?date=YYYY-MM-DD`
- 最小诊断接口：`GET /api/admin/system/audit-diagnostics`
- 时区：`Asia/Shanghai`

## 目标

为“每日盘点｜业务数据与网站运维”任务提供稳定、最小化、只读的业务汇总。调用方不需要总部 Cookie，也不能借日报凭据查看个人信息、订单详情、数据库诊断或执行写操作。

## 鉴权与权限边界

- 只接受一个 `Authorization: Bearer <token>` 请求头，期望值从进程环境变量 `SCENTPOOL_AUDIT_TOKEN` 读取。
- 服务端先对期望值和请求值做固定长度 SHA-256 摘要，再用 `secrets.compare_digest` 比较。
- 缺少请求头、请求头格式错误、令牌错误、重复 Authorization 头、令牌过长或环境变量未配置时，统一返回 `401` 与 `{"error":"审计接口认证失败。"}`，不说明配置状态。
- 总部登录会话不能替代该 Bearer 令牌。该 Bearer 令牌也不参与任何其他管理员路由鉴权。
- 精确日报路径上的非 `GET` 请求在 Bearer 鉴权通过后返回 `405`，不读取请求体、不执行写入。
- JSON 响应统一 `Cache-Control: no-store`。访问日志只记录方法与 URL，不记录 Authorization 请求头；专项测试同时检查响应体和日志中不存在令牌。

## 数据库只读边界

- 报表不复用 `Database.connect()`，也不调用 `initialize()`、迁移、建表或种子数据逻辑。
- 每次请求建立独立 SQLite URI `mode=ro` 连接，并执行、验证 `PRAGMA query_only=ON`。
- 自动化测试先证明 `query_only=1` 时写入被拒绝，再主动关闭 `query_only`，证明底层 `mode=ro` 仍拒绝删除，从而覆盖两层只读保护。
- 数据库打开或查询失败时只返回通用 `503`，不向调用方返回数据库路径或 SQLite 内部错误。

## 参数规则

- 必须且只能提供一次 `date`。
- 解码后必须恰为 10 个字符并匹配 `YYYY-MM-DD`，还必须是有效公历日期。
- 查询字符串最长 64 字符；重复参数、额外参数、空值、超长值、无效日期和 SQL 注入样式输入均返回 `400`。
- 日窗口固定为上海时区 `[date 00:00:00, 次日 00:00:00)`，避免遗漏带小数秒的边界事件。

## 响应契约

顶层只包含下列字段：

- `date`：已校验的请求日期。
- `timezone`：固定为 `Asia/Shanghai`。
- `metrics`：`new_shipments`、`shipped_shipments`、`signed_shipments`、`backlog_current_snapshot`。
- `by_store`：数组；每项只含 `store_name` 和上述四个计数字段，不含门店 ID。
- `exceptions.current_snapshot`：当前覆盖式状态中的面单下单、发货物流、退货物流和打印失败数量。
- `exceptions.current_snapshot_updated_on_date`：当前仍失败，且对应的最后状态时间落在指定日期内的数量。
- `historical_end_of_day`：目标日新建且日末未发出、日末已发货未签收、待处理超过 24 小时、面单排队/提交超过 30 分钟、打印处理中超过 30 分钟；每项都同时返回 `count`、`completeness` 与 `limitations`。
- `failures`：按 `label`、`tracking`、`printing` 返回目标日新发生、巡检时仍未恢复、目标日前历史残留和失败开始时间未知四组脱敏统计。
- `completeness`：事件 Schema 版本、首个具备完整全天证据的上海自然日、请求日期是否完整，以及固定限制代码。
- `long_waiting`：当前待处理超过 24 小时、面单排队/提交超过 30 分钟、打印处理中超过 30 分钟的快照数量。
- `recent_7_day_average`：固定 `calendar_days=7`，以及新建、发货、签收日均值。
- `data_quality`：时间缺失、时间非法、事件倒序和门店名缺失的汇总问题数；`time_evidence` 另按当前状态区分 `exact_records`、`estimated_records` 和 `still_missing_evidence_records`。
- `basis`：固定的机器可读口径标签，不包含动态业务明细。

响应不会返回或间接拼接收件人姓名、电话、地址、门店订单号、业务 ID、快递单号、退货物流标识、数据库路径、Cookie、会话、令牌、API 密钥、环境变量值、原始第三方报文或任何单条记录详情。

## 连接双采样

最小诊断接口复用日报 Bearer 鉴权，但不复用普通总部管理员接口。它不接受查询参数，也不打开 SQLite，只读取 `Database` 已有的进程内连接计数，响应固定为：

- `sampled_at`：服务端采样时间。
- `storage.connections.opened_total`：进程启动后累计打开数。
- `storage.connections.closed_total`：进程启动后累计关闭数。
- `storage.connections.active`：当前活动数。
- `storage.connections.peak_active`：进程内峰值活动数。

固定采集器两次调用间隔至少 30 秒。每次独立判断 `opened_total - closed_total == active`；两次都有效时再判断 `active` 是否回落、峰值变化、固定生产基线 9 是否被突破，以及累计计数是否因实例重启而重置。一次失败/超时只返回部分证据，两次失败返回不可用；不会用缺失样本补 0，也不会访问 `/api/admin/system/diagnostics`。

## 打印时间证据与相关性

批量打印入口为每个请求输出一条固定格式事件，PDF 合并开始后另输出合并事件。事件只含固定 `kind`、固定 `outcome`、整数毫秒耗时和慢请求布尔值。采集器必须全串匹配该格式；若日志带任何额外文字，就不能作为结构化打印事件。旧版本可在固定 Render request path 标签或原有纯数字合并成功日志上降级，但仍不输出原文。

采集结果按请求、合并、成功、失败、未知结果和慢请求计数，并保留仅含 ISO 时间、固定类别、结果、耗时和证据来源的事件。为覆盖上海自然日边界，相关性通道读取目标 UTC 窗口前后各 10 分钟，但普通当日日志计数仍来自目标日窗口。内存突升定义为相邻采样点同时满足至少增加 64 MiB、且至少增加前值 25%；异常重启只接受固定平台事件类别。输出列出每个风险信号 ±10 分钟内的打印事件数。

相关性是时间接近证据，不表示打印导致内存或重启。没有打印、没有风险信号、内存单位未知、日志时间缺失或边界通道失败时，结果保留 `no_data`、`schema_changed` 或 `evidence_complete=false`，不能表述为“打印无影响”。

## Render HTTP latency 兼容

2026-08-29 核对 Render 官方 API Reference 与 OpenAPI：`GET /v1/metrics/http-latency` 接受重复的浮点 `quantile`，响应正式契约为时间序列数组。官方 API 没有把 `0.90` 列为非法，因此既有 HTTP 400 不能归因于某个分位值；套餐能力、组合参数或服务端校验仍需由通道状态表达，不能猜测。

采集器现在先请求 `0.50`、`0.90`、`0.99` 多分位；仅在 HTTP 400 时逐个请求三个分位。成功的单分位不会被其他分位失败覆盖，而是返回 `status=ok`、`coverage=partial` 与失败分位列表。全部无数据返回 `no_data`，全部 HTTP 失败返回 `http_error`，无法识别的结构返回 `schema_changed`。解析仅接受官方数组以及受控的 `data`、`series` 或 `data.series` 包装；标签仍只转发 `quantile`，从不把缺失延迟写成 0。

## 指标口径与历史限制

### 精确事件指标

- 新建：`shipments.created_at` 落入日窗口。
- 发货：`shipments.shipped_at` 落入日窗口。
- 签收：`shipments.tracking_signed_at` 落入日窗口。
- 最近 7 日：从指定日期向前包含 6 日，共 7 个上海自然日；始终用事件总数除以 7，四舍五入保留两位。即使 7 日完全没有数据也返回 `0.0`，不改变分母。

### 历史日末与证据完整性

新增 `audit_event_meta` 与 `audit_state_events`。迁移时只保存一份不含业务编号和个人字段的旧库状态快照；此后 SQLite 触发器只追加以下规范化状态变化：

- 发货单：待处理、已发货、已签收、异常、取消、删除。
- 面单：未开始、排队、提交、失败、完成、取消。
- 发货/退货物流：未开始、等待揽收、运输中、失败、签收。
- 打印：未开始、待打印、处理中、成功、失败。

事件表只含领域、实体类型、内部整数记录号、事件类型、规范化状态、固定原因分类、记录创建时间、发生时间和证据来源。数据库触发器拒绝更新或删除事件；日报连接仍是 `mode=ro` + `query_only`，不能写这两张表。

迁移时刻通常位于一个自然日中间，因此 `full_day_coverage_from` 从下一个上海自然日开始。请求日期不早于该日期、且该上海自然日已结束时，日末状态和目标日失败可从只追加事件精确复原，`completeness=complete_append_only_events`。更早日期或尚未结束的日期使用现有时间字段给出明确标记的部分观察，限制包括后续取消可能清空发货时间、覆盖式状态可能抹去已恢复失败、历史打印开始时间未单独保存；这些结果不会被描述为完整历史。

### 兼容的当前快照

现有 `shipments` 与 `return_orders` 保存覆盖式当前状态，没有完整的状态事件日志：

- `backlog_current_snapshot` 只统计“查询时仍为待处理，且 `created_at` 早于指定日期次日零点”的记录。它不能证明这些记录在指定日期结束时也处于积压状态。
- 兼容字段 `exceptions` 仍只统计查询时仍处于失败状态的记录。`current_snapshot_updated_on_date` 进一步按当前记录的最后相关时间筛选，但不能还原当日发生后已恢复、被覆盖或多次发生的失败事件。
- 打印表没有独立失败事件时间，日期筛选使用发货单当前 `updated_at`，因此同样只表示当前失败快照，不是打印失败历史。
- 不返回历史积压均值或历史失败均值，以免把不可重建的数据伪装成精确事实。

`basis.backlog` 与 `basis.exceptions` 保留原有固定声明，避免旧调用方把兼容字段误认成历史值；新增调用方应使用 `historical_end_of_day`、`failures` 与 `completeness`。

## 失败分类与提示

日报从内部失败文字映射到四种固定分类，不返回原文：

| 分类 | 含义 | `action_hint` |
| --- | --- | --- |
| `provider_or_api_rejected` | 供应商/API 明确拒绝、停发或超区 | 核对承运范围或供应商规则，确认后再重试 |
| `system_error_or_timeout` | 系统异常、网络或超时 | 等待自动恢复；持续失败时联系管理员，不连续重复提交 |
| `data_or_configuration` | 地址、联系方式、承运商、网点、余额或配置 | 修正数据/配置后再重试 |
| `insufficient_historical_evidence` | 空原因、迁移快照或无法安全判断 | 由管理员在受控后台核对，不凭猜测重复提交 |

`new_on_date` 在完整覆盖日统计进入失败状态的失败轮次，因此当日失败后恢复仍会保留；覆盖前只能返回“当前仍失败且最后时间落在目标日”的部分观察。`unresolved_at_collection` 始终是巡检时当前失败；`historical_residual_before_date` 是当前仍失败且已有失败时间早于目标日的记录；时间缺失或非法的当前失败进入 `unresolved_with_unknown_start`。

## 数据质量计数

`data_quality` 是查询时全部发货单的当前数据质量快照，不按请求日期过滤；`basis.data_quality` 固定声明这一范围。`data_quality.total_issues` 是下列问题计数之和；同一记录可能贡献多个问题，因此它不是问题记录去重数：

- `invalid_created_at`
- `invalid_shipped_at`
- `invalid_tracking_signed_at`
- `shipped_state_missing_shipped_at`
- `signed_state_missing_tracking_signed_at`
- `shipped_before_created`
- `signed_before_shipped`
- `missing_store_name`

这些字段只返回数量，不返回问题记录标识或内容。

`time_evidence` 不从 `total_issues` 中扣除任何记录，也不把估算值算作精确值。旧值没有可靠来源分类时为 `legacy_unclassified`，进入“仍缺证据”而不是伪装为精确；修复机制与证据优先级见 [`shipment-time-integrity.md`](shipment-time-integrity.md)。

## 验收与测试

专项测试 `python3 daily_audit_test.py` 使用临时合成数据库和本机临时 HTTP 服务，并由 `smoke_test.py` 调用，覆盖：

- Bearer 缺失、错误、正确和环境变量未配置。
- 总部会话不能替代日报令牌，日报令牌不能访问诊断或写路由。
- 非 `GET`、非法/重复/额外/超长日期和 SQL 注入样式输入。
- `mode=ro` 与 `query_only` 双层拒绝写入。
- 上海时区日边界、跨日发货/签收、7 日窗口边界、两门店汇总、异常、三类长等待和数据质量。
- 完整覆盖日的日末复原、迁移前部分口径、当日失败后恢复、当前未恢复、历史残留和失败时间缺失。
- 四类脱敏原因及非技术处理提示、事件表字段隐私和事件更新/删除拒绝。
- 7 日空数据固定返回 `0.0`。
- 递归扫描全部响应键和值，排除个人信息、订单/物流标识、`raw` 字段、密钥、令牌、会话和数据库路径。
- 时间证据分组不含记录标识，估算修复后仍保留原始质量口径和证据等级。
- 响应体与 HTTP 日志不出现审计令牌。
- 最小连接端点的令牌/总部会话双向隔离、非 GET、额外参数、字段白名单和递归隐私扫描。

采集器专项测试 `python3 daily_audit_probe_test.py` 另覆盖：连接守恒与不守恒、30 秒间隔、一次失败/超时、活动不回落、峰值异常、计数重置/诊断不可用；打印成功/失败/慢请求、无打印、时间不足、内存突升与异常重启 ±10 分钟、跨上海午夜、恶意日志和长字段；latency 多分位、单分位降级、HTTP 400、部分成功、套餐无数据、多个受控 Schema 和未知 Schema；分页游标缺失/不前进及单通道失败。

仍需同时运行项目规定的 Python 编译、前端语法检查和完整 `smoke_test.py`。

## 部署、回滚与令牌管理

基础版在 2026-08-05 不修改 Schema。扩展已于 2026-08-17 增量创建 `audit_event_meta`、`audit_state_events`、索引和触发器，并为现有记录写入一次脱敏状态快照；没有修改业务表状态或个人字段。

扩展部署前应执行 SQLite 在线备份和 `PRAGMA integrity_check`。上线后从 `completeness.full_day_coverage_from` 起才能获得完整自然日证据；不能把迁移前日期改写成完整。代码回滚时新增表和触发器可以安全保留，旧代码会忽略它们；不要为回滚删除审计事件。

2026-08-29 的连接/打印/延迟兼容扩展没有 Schema 迁移，也不需要新增环境变量。部署后需要用固定采集器完成一次真实 30 秒双采样，且只能通过 `scripts/install_daily_audit_probe.py` 原子安装本机副本。回滚代码即可移除最小诊断路由和固定打印事件；本机采集器应同样用安装器回退到已审查的仓库版本。未部署前，生产缺少新端点属于预期的 `http_error`，不能据此宣称生产连接异常。

2026-08-05 基础版已完成以下生产操作：

1. 审核并将提交 `74066c1` 快进合并到 `main`。
2. 在受控环境生成 256 位随机令牌，并分别保存到正确 Render 生产服务环境变量和 Mac 登录钥匙串；没有输出或提交令牌值。
3. 核对服务名、Service ID、仓库、分支、公开域名和区域后，仅部署正确生产服务 `scentpool-express-sync-ec7c`。
4. 验证健康检查、正确/错误/缺失令牌、非 `GET`、管理员接口隔离和响应隐私白名单；全部通过。

2026-08-17 历史扩展完成以下生产操作：

1. 部署前通过总部会话下载 SQLite 在线备份到本机受控目录，权限收紧为 `0600`，`PRAGMA integrity_check` 返回 `ok`；备份没有进入仓库。
2. 将提交 `a25501b` 推送到 `main`，并只监控正确生产服务；部署 `dep-da17chh42hec73ag7d70` 标记为 `live`。
3. 公开健康检查返回 `ok=true`、`database=true`；只读日报确认新增日末、失败分类、完整性和长等待字段可用，且不返回失败原文或个人字段。
4. 本次迁移发生在上海自然日中间，生产返回 `full_day_coverage_from=2026-08-18`；此前日期继续明确标为部分证据。Render 日志通道本次返回 HTTP 503、延迟指标返回 HTTP 400，因此固定采集器仍正确给出总状态 `error`，不会把运维证据缺失当作正常。

基础版回滚只需部署上一版本。扩展版回滚同样不需要数据恢复，但新增表、索引和触发器会保留。环境变量的轮换或删除仍属于生产变更，必须另行授权。轮换时必须同时更新 Render 与受控调用端的钥匙串值，旧令牌随后失效。
