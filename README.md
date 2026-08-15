# 万物香铺快递同步

万物香铺内部协同网站：门店提交快递发货需求，总部在后台统一处理、筛选、导出和备份。

## 项目知识与协作规范

- Codex 和开发协作规则见 `AGENTS.md`。
- 项目背景、架构、当前状态、功能索引和任务模板见 `docs/INDEX.md`。
- 新功能、修复和 UI 调整应使用独立 Git Worktree 和分支，不在长期总控或运维任务中直接开发。
- 生产事实可能随时间变化；未经实时核实的历史状态统一记录为“待核实”，以 `docs/STATUS.md` 为核对入口。

## 本地运行

```bash
cd /Users/zgd/scentpool-express-sync
python3 server.py
```

打开 `http://127.0.0.1:8765`。

局域网试用：

```bash
python3 server.py --host 0.0.0.0 --port 8765
```

本地开发库首次启动会创建开发账号；发布前必须用 `manage.py` 重置密码，生产环境不会自动创建门店默认账号。

## 环境变量

- `PORT`：云端端口，Render 会自动注入。
- `SCENTPOOL_ENV=production`：开启生产模式。
- `SCENTPOOL_DB_PATH=/var/data/scentpool.db`：生产数据库路径。
- `SCENTPOOL_PRODUCT_FILE=/var/data/products.xlsx`：最近一次上传的商品 Excel 保存路径。
- `SCENTPOOL_SESSION_SECURE=1`：Cookie 增加 `Secure`。
- `SCENTPOOL_ADMIN_PASSWORD`：生产首次启动且没有迁移数据库时，用它创建总部账号。
- `SCENTPOOL_ALLOW_DB_RESTORE=1`：临时开启数据库恢复接口，恢复完成后应改回 `0`。
- `SCENTPOOL_AUDIT_TOKEN`：脱敏业务日报接口的独立 Bearer 令牌。只在获授权的部署操作中配置，不写入仓库，也不能替代总部会话访问其他接口。
- `SCENTPOOL_TRACKING_PROVIDER=kuaidi100`：物流查询服务商。
- `SCENTPOOL_TRACKING_AUTO=1`：开启自动物流查询。
- `SCENTPOOL_TRACKING_INTERVAL_MINUTES=360`：发货单自动查询间隔，默认 6 小时。
- `SCENTPOOL_RETURN_TRACKING_INTERVAL_MINUTES=720`：退货单自动查询间隔，默认 12 小时。
- 总部“同步物流”会刷新全部超过 30 分钟未查询的已发货、未签收订单；30 分钟内已查询的订单会按快递100要求跳过。
- `SCENTPOOL_KUAIDI100_ENDPOINT=https://poll.kuaidi100.com/poll/query.do`：快递100实时查询接口地址。
- `SCENTPOOL_KUAIDI100_AUTODETECT_ENDPOINT=https://www.kuaidi100.com/autonumber/auto`：退货快递公司智能识别接口地址。
- `SCENTPOOL_KUAIDI100_CUSTOMER`：快递100 customer，只放 Render 环境变量。
- `SCENTPOOL_KUAIDI100_KEY`：快递100 key，只放 Render 环境变量。
- `SCENTPOOL_KUAIDI100_LABEL_ENABLED=0`：快递100电子面单开关，测试完成前保持 `0`。
- `SCENTPOOL_KUAIDI100_LABEL_ENDPOINT=https://api.kuaidi100.com/label/order`：电子面单下单、取消和复打接口。
- `SCENTPOOL_KUAIDI100_AUTH_ENDPOINT=https://poll.kuaidi100.com/printapi/authThird.do`：菜鸟账号授权接口。
- `SCENTPOOL_KUAIDI100_THIRD_INFO_ENDPOINT=https://poll.kuaidi100.com/eorderapi.do`：授权网点与面单余额接口。
- `SCENTPOOL_KUAIDI100_LABEL_SECRET`：企业电子面单 secret，只放 Render 环境变量。
- `SCENTPOOL_PUBLIC_BASE_URL`：网站公网根地址，用于生成菜鸟授权和打印状态回调地址。
- `SCENTPOOL_MAX_REQUEST_THREADS=8`：Web 请求固定线程池大小，防止并发请求无限创建线程与内存分配区。
- `SCENTPOOL_SHIPPING_TRANSIENT_RETRIES=2`：电子面单遇到限流、服务端错误、超时或临时网络错误时，在同一幂等请求内额外重试的次数；可设为 `0` 至 `3`。
- `SCENTPOOL_MAX_BATCH_PRINT_ORDERS=200`：单次批量合并面单上限。
- `SCENTPOOL_LABEL_PROCESS_MEMORY_MB=320`：Linux 上 PDF 合并子进程的硬内存上限。
- `MALLOC_ARENA_MAX=2`、`MALLOC_TRIM_THRESHOLD_=131072`：限制 glibc 分配区并更积极归还空闲内存。
- 菜鸟授权面单使用每家快递公司自己的 `thirdTemplateURL`。圆通当前配置为一联单模板 `https://cloudprint.cainiao.com/template/standard/850338` 和货物自定义区模板 `https://cloudprint.cainiao.com/template/customArea/77205369`；京东和顺丰暂不配置模板。系统通过 `thirdCustomTemplateUrl` 和 `customParam.itemSummary` 传入按分类换行的商品名称与数量。
- 电子面单的物品名称和备注会从发货单商品明细自动生成，格式为“【分类】商品名*数量”；同分类商品合并展示，不同分类自动换行，订单备注在全部货品信息后另起一行显示。物品栏超过 50 字时会清理重复品类前缀并逐级缩写每个商品的主关键词，但不会省略任何商品。自定义区内容最多 100 字。
- 发货后台可从当前筛选结果中选择全部或部分“待打印”订单，服务端合并快递100返回的 PDF 后一次打开打印窗口；合并成功的订单会统一标记为“打印成功”，后续仍可从单个订单查看原面单。

## 脱敏业务日报（生产已启用）

`GET /api/admin/system/daily-audit?date=YYYY-MM-DD` 供每日盘点任务读取汇总数据。它不接受总部登录会话替代鉴权，只接受 `Authorization: Bearer <SCENTPOOL_AUDIT_TOKEN>`；该 Bearer 令牌也不能访问其他管理员接口。

接口使用独立的 SQLite URI `mode=ro` 连接并启用 `PRAGMA query_only=ON`，只返回日期、时区、门店名、汇总数量、固定失败分类和完整性说明，不返回个人信息、订单或物流标识、原始第三方报文、会话、密钥、环境变量值或数据库路径。新版本通过脱敏只追加事件复原完整覆盖日的日末待处理、未签收积压、长等待和失败恢复；覆盖开始前的日期会返回 `completeness` 与 `limitations`，不会猜测。完整字段、7 日窗口、测试和部署步骤见 `docs/features/daily-audit.md`。

该能力已在正确生产服务 `scentpool-express-sync-ec7c` 启用。`SCENTPOOL_AUDIT_TOKEN` 只保存在 Render 环境变量和受控本地钥匙串中，不得写入命令示例、日志、截图或仓库。

### 本机固定采集器

仓库内版本位于 `tools/scentpool_daily_audit_probe.py`，固定只读目标为 `srv-d913padckfvc73eom3f0` / `scentpool-express-sync-ec7c`。安全更新本机副本：

```bash
python3 scripts/install_daily_audit_probe.py
python3 scripts/install_daily_audit_probe.py --check
```

安装器原子更新 `~/.codex/scentpool_daily_audit_probe.py` 并设为 `0700`，不读取密钥。采集器运行时只从 Mac 登录钥匙串读取 `scentpool-audit-token` 与 `scentpool-render-api`，只执行固定 GET 白名单；它聚合部署、重启、OOM、5xx、异常栈、SQLite 锁、超时、慢请求、内存、磁盘和 HTTP 请求/延迟。接口无数据、权限不足、HTTP 错误、响应结构变化、网络受限或进程异常都会输出有效脱敏 JSON，不会以“成功退出但没有结果”表示失败。

当前生产仍需部署本分支后才会返回新增的历史日末和失败事件字段；部署前的生产日报保持既有快照口径。

## 发布到 Render

1. 先在本地重置所有默认账号密码：

```bash
python3 manage.py set-password admin
python3 manage.py set-password store01
python3 manage.py summary
```

2. 导出可迁移的生产数据库：

```bash
python3 manage.py export-production --output data/scentpool-production.db
```

3. 将项目推送到私有 GitHub 仓库，然后在 Render 使用 `render.yaml` 创建 Blueprint。

4. Render 首次部署时确认：

- Web Service 绑定 `$PORT`。
- 持久磁盘挂载到 `/var/data`。
- 健康检查路径是 `/api/health`。
- `SCENTPOOL_ENV`、`SCENTPOOL_DB_PATH`、`SCENTPOOL_PRODUCT_FILE`、`SCENTPOOL_SESSION_SECURE` 已按 `render.yaml` 设置。
- 如果不迁移数据库，必须在 Render 环境变量里设置 `SCENTPOOL_ADMIN_PASSWORD`。

5. 迁移本地数据库到云端时，临时将 Render 环境变量 `SCENTPOOL_ALLOW_DB_RESTORE` 设为 `1`，用总部账号登录后通过接口上传 `data/scentpool-production.db`：

```bash
read -s SCENTPOOL_PASSWORD
curl -b cookie.txt -c cookie.txt \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$SCENTPOOL_PASSWORD\"}" \
  https://你的服务.onrender.com/api/login

curl -b cookie.txt -c cookie.txt \
  -F "backup_file=@data/scentpool-production.db" \
  https://你的服务.onrender.com/api/admin/restore-db
```

恢复完成后把 `SCENTPOOL_ALLOW_DB_RESTORE` 改回 `0` 并重启服务。

## 商品导入

总部进入“商品”页面，上传 `.xlsx` 商品资料即可刷新点菜单。生产环境会把最近一次上传文件保存到 `/var/data/products.xlsx`，不依赖本机 Downloads 路径。

## 物流查询

总部在发货后台为待处理订单填写快递单号后，系统会自动改为“已发货”并立即查询一次物流。门店新增退货时只填写快递单号，系统先由快递100智能识别快递公司，再使用识别结果中的官方公司编码立即查询物流；申通等电子面单列表以外、但快递100支持的公司也可以查询。历史查询失败的退货重试时会重新识别并纠正旧公司。识别结果在退货看板标为“仅供参考”，识别或查询失败会保留退货记录并显示明确原因。开启快递100环境变量后，后台使用快递100实时查询接口（`https://poll.kuaidi100.com/poll/query.do`）每 6 小时查询一次未签收发货单；退货单默认每 12 小时查询一次。总部也可以在发货后台点击“同步物流”，或在退货看板点击“同步退货物流”手动刷新。

实时查询返回“查询无结果”或没有轨迹时显示“等待揽收”，不作为异常。若快递100接口本身返回 HTTP `401/403/408/429/5xx`、连接失败或无效响应，系统只探测一个单号后停止本轮批量查询，不覆盖订单已有物流状态；异常中心合并为一条“物流服务异常”并显示影响数量，下一轮先探测服务是否恢复。

快递100返回签收后，系统会自动更新：

```text
订单状态：已签收
物流状态：已签收
签收时间：快递100最新签收轨迹时间
```

问题件只会显示为“问题件”，不会自动改成“异常”，避免误判影响订单。

## 电子面单下单与打印

系统使用快递100电子面单接口，不再调用“上门取件线下支付”接口。总部先进入“电子面单设置”，填写固定寄件信息，然后点击“授权菜鸟账号”。授权成功后刷新网点与面单余额，为圆通、京东、顺丰分别选择授权网点和产品类型。

发货后台的“批量下单”读取当前筛选条件下的全部可下单订单，不受每页 50 条限制。系统按持久化队列逐单取号并生成面单；成功后保存快递单号、任务 ID、面单地址和打印状态，订单改为“已发货”，随后立即查询一次物流。快递100返回 `30011` 且包含完整运单号和任务号时按“重新取得面单成功”处理，不再误报失败。限流、服务端错误、超时和临时网络错误会使用原请求编号自动重试，避免重复取号；中断超过 10 分钟的任务会自动恢复，连续 3 次仍未完成则停止自动尝试并明确标为失败。

总部发货后台顶部的“异常提醒”角标汇总面单下单、物流查询、退货物流查询和打印失败，以及等待超过 30 分钟的面单任务。点击角标后按电子面单、物流查询和打印分类查看；每个分类使用独立列表，不会被其他分类的前 50 项挤空。订单级异常包含业务编号、失败原因和操作建议，失败批次只会重新提交失败订单；平台级异常合并显示影响数量且不提供逐单处理按钮。提醒列表每 60 秒自动刷新，底层状态恢复后会自动消失；线下已经处理的订单级异常可标记“人工已处理”，只隐藏当前这一次，后续再次失败仍会重新提醒。页面无法读取异常状态时会显示红色提示，恢复前不要重复提交同一批任务。

打印方式：

- `PDF`：菜鸟授权模式返回 PDF 面单，在发货后台点击“查看面单”后使用本地打印机打印。
- `CLOUD`：快递100云打印，需要网点电子面单账号和设备码 `siid`；打印状态通过回调更新，支持两天内复打。菜鸟第三方授权通道会固定返回 PDF，不执行云打印。

电子面单取消会使用原下单任务保存的授权参数调用快递100 `method=cancel`，只有返回 `code=200` 后才清空旧单号并解锁订单。取消后订单恢复为待处理并保留原快递公司；再次下单会生成新的 `orderId`，避免快递100在 48 小时内返回已经取消的旧面单。部分快递公司或授权通道不支持接口取消时，需要在菜鸟后台或合作网点人工回收面单。

快递100当前官方取消列表包含京东、顺丰和圆通承诺达，但不包含普通圆通 `yuantong`。普通圆通返回 `30005` 时系统会保留原面单并阻止重复下单；不能仅在本地清空单号，否则旧面单仍可能有效。

正式启用前需要在快递100企业后台开通“电子面单”并取得 `LABEL_SECRET`。配置完成、菜鸟授权成功并完成一张测试单后，将 Render 中的 `SCENTPOOL_KUAIDI100_LABEL_ENABLED` 改为 `1`。

## 备份与回滚

- 总部在“发货后台”点击“备份数据库”，会下载完整 SQLite 文件。
- 所有在线备份都使用 SQLite Backup API，并在交付前执行 `PRAGMA integrity_check`，兼容 WAL 模式。
- 命令行可运行 `python3 manage.py --db /var/data/scentpool.db backup` 生成并校验备份，运行 `diagnostics` 查看文件与各表占用。
- 恢复数据库前，服务会自动在数据库同目录的 `backups/` 下保存时间戳备份。
- 本地数据库默认在 `/Users/zgd/scentpool-express-sync/data/scentpool.db`。
- Render 数据库存放在持久磁盘 `/var/data/scentpool.db`，服务重启后数据应保留。
- 生产事故、容量预估、备份恢复和数据库迁移门槛见 `OPERATIONS.md`。

## 本地验证

```bash
python3 -m py_compile server.py database.py manage.py smoke_test.py tracking.py shipping.py label_pdf.py
node --check static/app.js
python3 smoke_test.py
```
