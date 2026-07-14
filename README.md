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
- 总部“同步物流”会刷新全部超过 30 分钟未查询的已发货、未签收订单；30 分钟内已查询的订单会按快递100要求跳过。
- `SCENTPOOL_KUAIDI100_ENDPOINT=https://poll.kuaidi100.com/poll/query.do`：快递100实时查询接口地址。
- `SCENTPOOL_KUAIDI100_CUSTOMER`：快递100 customer，只放 Render 环境变量。
- `SCENTPOOL_KUAIDI100_KEY`：快递100 key，只放 Render 环境变量。
- `SCENTPOOL_KUAIDI100_LABEL_ENABLED=0`：快递100电子面单开关，测试完成前保持 `0`。
- `SCENTPOOL_KUAIDI100_LABEL_ENDPOINT=https://api.kuaidi100.com/label/order`：电子面单下单、取消和复打接口。
- `SCENTPOOL_KUAIDI100_AUTH_ENDPOINT=https://poll.kuaidi100.com/printapi/authThird.do`：菜鸟账号授权接口。
- `SCENTPOOL_KUAIDI100_THIRD_INFO_ENDPOINT=https://poll.kuaidi100.com/eorderapi.do`：授权网点与面单余额接口。
- `SCENTPOOL_KUAIDI100_LABEL_SECRET`：企业电子面单 secret，只放 Render 环境变量。
- `SCENTPOOL_PUBLIC_BASE_URL`：网站公网根地址，用于生成菜鸟授权和打印状态回调地址。

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

## 电子面单下单与打印

系统使用快递100电子面单接口，不再调用“上门取件线下支付”接口。总部先进入“电子面单设置”，填写固定寄件信息，然后点击“授权菜鸟账号”。授权成功后刷新网点与面单余额，为圆通、京东、顺丰分别选择授权网点和产品类型。

发货后台的“批量下单”读取当前筛选条件下的全部可下单订单，不受每页 50 条限制。系统按持久化队列逐单取号并生成面单；成功后保存快递单号、任务 ID、面单地址和打印状态，订单改为“已发货”，随后立即查询一次物流。

打印方式：

- `PDF`：菜鸟授权模式返回 PDF 面单，在发货后台点击“查看面单”后使用本地打印机打印。
- `CLOUD`：快递100云打印，需要网点电子面单账号和设备码 `siid`；打印状态通过回调更新，支持两天内复打。菜鸟第三方授权通道会固定返回 PDF，不执行云打印。

电子面单取消只有在快递公司接口确认成功后才会清空单号并解锁订单。部分快递公司或授权通道不支持接口取消时，需要在菜鸟后台或合作网点人工回收面单。

正式启用前需要在快递100企业后台开通“电子面单”并取得 `LABEL_SECRET`。配置完成、菜鸟授权成功并完成一张测试单后，将 Render 中的 `SCENTPOOL_KUAIDI100_LABEL_ENABLED` 改为 `1`。

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
