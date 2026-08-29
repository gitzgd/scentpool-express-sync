# 功能索引

此文件提供稳定的业务能力地图。功能行为发生变化时，更新对应条目；复杂功能可在本目录新增独立文档并从这里链接。

| 领域 | 当前能力 | 主要代码 |
| --- | --- | --- |
| 身份与权限 | 总部/门店登录、会话、门店数据隔离 | `server.py`, `database.py` |
| 门店与商品 | 门店维护、商品维护、Excel 导入、条码标识 | `database.py`, `xlsx_importer.py`, `static/app.js` |
| [发货单](shipment-pagination.md) | 门店创建、服务端分页看板、筛选、编辑限制、总部处理 | `database.py`, `server.py`, `static/app.js` |
| [退货单](return-carrier-autodetect.md) | 门店创建、快递公司自动识别、总部看板、商品快照、退货物流 | `database.py`, `tracking.py`, `server.py`, `static/app.js` |
| 物流 | 快递100查询、自动/手动同步、签收更新 | `tracking.py`, `server.py` |
| 电子面单 | 菜鸟授权、网点余额、批量下单、取消、复打 | `shipping.py`, `database.py`, `server.py` |
| 打印 | 待打印选择、PDF 合并、打印状态、单张复打 | `label_pdf.py`, `server.py`, `static/app.js` |
| [任务可靠性与失败提示](task-reliability.md) | 面单短暂故障重试、中断恢复、分类异常角标、60 秒刷新、人工确认与操作指引 | `database.py`, `shipping.py`, `server.py`, `static/app.js` |
| [脱敏业务日报](daily-audit.md) | 独立 Bearer 鉴权、只读 SQLite 汇总、前向完整历史日末、连接双采样、脱敏打印时间相关性与 Render 延迟受控降级（最新扩展待部署） | `database.py`, `server.py`, `tools/scentpool_daily_audit_probe.py`, `daily_audit_test.py`, `daily_audit_probe_test.py` |
| 导出 | CSV/XLSX、门店和日期命名、商品分类换行 | `server.py` |
| 数据安全 | SQLite 在线备份、恢复保护、完整性检查 | `database.py`, `manage.py`, `server.py` |
| 运维 | 健康检查、进程与 SQLite 连接诊断、慢请求、资源限制 | `server.py`, `database.py`, `render.yaml`, `OPERATIONS.md` |

## 新增功能文档要求

当功能跨越多个模块、引入外部接口、改变状态机或需要生产迁移时，在本目录新增文档，至少包含：

- 用户和业务目标。
- 数据模型和状态流转。
- 权限边界。
- API 或页面变化。
- 失败与恢复路径。
- 验收与测试。
- 部署、迁移和回滚影响。
