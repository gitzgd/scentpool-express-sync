# 万物香铺快递同步

万物香铺内部协同网站：门店提交快递发货需求，总部在后台统一处理、筛选、导出和备份。

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
- `SCENTPOOL_TRACKING_PROVIDER=kuaidi100`：物流查询服务商。
- `SCENTPOOL_TRACKING_AUTO=1`：开启自动物流查询。
- `SCENTPOOL_TRACKING_INTERVAL_MINUTES=360`：发货单自动查询间隔，默认 6 小时。
- `SCENTPOOL_RETURN_TRACKING_INTERVAL_MINUTES=720`：退货单自动查询间隔，默认 12 小时。
- `SCENTPOOL_KUAIDI100_ENDPOINT=https://poll.kuaidi100.com/poll/query.do`：快递100实时查询接口地址。
- `SCENTPOOL_KUAIDI100_CUSTOMER`：快递100 customer，只放 Render 环境变量。
- `SCENTPOOL_KUAIDI100_KEY`：快递100 key，只放 Render 环境变量。
- `SCENTPOOL_KUAIDI100_ORDER_ENABLED=0`：快递100寄件下单开关，测试完成前保持 `0`。
- `SCENTPOOL_KUAIDI100_ORDER_ENDPOINT=https://order.kuaidi100.com/order/corderapi.do`：上门取件线下支付下单接口。
- `SCENTPOOL_KUAIDI100_ORDER_SECRET`：寄件下单 secret，只放 Render 环境变量。
- `SCENTPOOL_PUBLIC_BASE_URL`：网站公网根地址，用于生成快递100订单回调地址。

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

总部在发货后台为待处理订单填写快递单号后，系统会自动改为“已发货”并立即查询一次物流。开启快递100环境变量后，后台使用快递100实时查询接口（`https://poll.kuaidi100.com/poll/query.do`）每 6 小时查询一次未签收发货单；退货单默认每 12 小时查询一次。总部也可以在发货后台点击“同步物流”，或在退货看板点击“同步退货物流”手动刷新。

快递100返回签收后，系统会自动更新：

```text
订单状态：已签收
物流状态：已签收
签收时间：快递100最新签收轨迹时间
```

问题件只会显示为“问题件”，不会自动改成“异常”，避免误判影响订单。

## 快递一键下单

总部先进入“发货设置”，填写固定寄件人、联系电话和完整寄件地址。发货后台的“批量下单”会读取当前筛选条件下的全部可下单订单，不受每页 50 条限制；确认圆通、京东或顺丰及取件时间后，系统在后台逐单提交快递100。

圆通可能在首次响应中不返回单号，此时显示“等待快递单号”。快递100回调取得单号后，系统自动写入单号并改为“已发货”。失败订单可以按批次单独重试；已经接单的订单如需修改，应先取消快递下单。

正式启用前需要在快递100企业后台开通“上门取件线下支付”并取得 `ORDER_SECRET`。配置完成并完成一张测试单后，将 Render 中的 `SCENTPOOL_KUAIDI100_ORDER_ENABLED` 改为 `1`。

“菜鸟打印数据”目前导出标准收寄件和商品字段。拿到菜鸟商家后台的实际空白导入模板后，应再按模板列名和工作表结构做精确对齐。

## 备份与回滚

- 总部在“发货后台”点击“备份数据库”，会下载完整 SQLite 文件。
- 恢复数据库前，服务会自动在数据库同目录的 `backups/` 下保存时间戳备份。
- 本地数据库默认在 `/Users/zgd/scentpool-express-sync/data/scentpool.db`。
- Render 数据库存放在持久磁盘 `/var/data/scentpool.db`，服务重启后数据应保留。

## 本地验证

```bash
python3 -m py_compile server.py database.py manage.py smoke_test.py tracking.py shipping.py
node --check static/app.js
python3 smoke_test.py
```
