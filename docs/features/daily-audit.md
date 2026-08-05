# 脱敏只读业务日报

- 状态：已开发、未部署/待验收
- 接口：`GET /api/admin/system/daily-audit?date=YYYY-MM-DD`
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
- `long_waiting.label_tasks_over_30_minutes_current_snapshot`：当前仍处于排队/提交中且等待超过 30 分钟的面单任务数量。
- `recent_7_day_average`：固定 `calendar_days=7`，以及新建、发货、签收日均值。
- `data_quality`：时间缺失、时间非法、事件倒序和门店名缺失的汇总问题数。
- `basis`：固定的机器可读口径标签，不包含动态业务明细。

响应不会返回或间接拼接收件人姓名、电话、地址、门店订单号、业务 ID、快递单号、退货物流标识、数据库路径、Cookie、会话、令牌、API 密钥、环境变量值、原始第三方报文或任何单条记录详情。

## 指标口径与历史限制

### 精确事件指标

- 新建：`shipments.created_at` 落入日窗口。
- 发货：`shipments.shipped_at` 落入日窗口。
- 签收：`shipments.tracking_signed_at` 落入日窗口。
- 最近 7 日：从指定日期向前包含 6 日，共 7 个上海自然日；始终用事件总数除以 7，四舍五入保留两位。即使 7 日完全没有数据也返回 `0.0`，不改变分母。

### 当前快照，不是精确历史

现有 `shipments` 与 `return_orders` 保存覆盖式当前状态，没有完整的状态事件日志：

- `backlog_current_snapshot` 只统计“查询时仍为待处理，且 `created_at` 早于指定日期次日零点”的记录。它不能证明这些记录在指定日期结束时也处于积压状态。
- 失败汇总只统计查询时仍处于失败状态的记录。`current_snapshot_updated_on_date` 进一步按当前记录的最后相关时间筛选，但不能还原当日发生后已恢复、被覆盖或多次发生的失败事件。
- 打印表没有独立失败事件时间，日期筛选使用发货单当前 `updated_at`，因此同样只表示当前失败快照，不是打印失败历史。
- 不返回历史积压均值或历史失败均值，以免把不可重建的数据伪装成精确事实。

`basis.backlog` 与 `basis.exceptions` 在每次响应中固定声明上述限制。若未来业务需要精确历史，应单独设计只追加事件表和迁移验证，不能修改本接口名称后静默改变口径。

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

## 验收与测试

专项测试 `python3 daily_audit_test.py` 使用临时合成数据库和本机临时 HTTP 服务，覆盖：

- Bearer 缺失、错误、正确和环境变量未配置。
- 总部会话不能替代日报令牌，日报令牌不能访问诊断或写路由。
- 非 `GET`、非法/重复/额外/超长日期和 SQL 注入样式输入。
- `mode=ro` 与 `query_only` 双层拒绝写入。
- 上海时区日边界、7 日窗口边界、两门店汇总、异常、长等待和数据质量。
- 7 日空数据固定返回 `0.0`。
- 递归扫描全部响应键和值，排除个人信息、订单/物流标识、`raw` 字段、密钥、令牌、会话和数据库路径。
- 响应体与 HTTP 日志不出现审计令牌。

仍需同时运行项目规定的 Python 编译、前端语法检查和完整 `smoke_test.py`。

## 部署、回滚与后续授权

本功能不修改 Schema、不迁移数据、不修改业务记录。部署前仍需独立授权完成以下操作：

1. 统一工作台审核并合并功能分支。
2. 由获授权的生产操作员在受控环境生成高熵令牌；不能在代码、任务输出或 Git 中生成或保存。
3. 核对准确 Render 服务后配置 `SCENTPOOL_AUDIT_TOKEN` 并部署。
4. 用无个人信息的响应结构检查、错误令牌检查和只读生产观察完成上线验收。

本开发任务没有执行以上生产操作。回滚只需部署上一版本；没有数据库回滚或数据恢复步骤。环境变量的新增、轮换或删除均属于生产变更，必须另行授权。
