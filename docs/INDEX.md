# 项目知识索引

本目录把长期项目知识从单一对话中移到 Git 仓库。新任务应从这里和根目录 `AGENTS.md` 开始，而不是依赖旧任务记忆。

## 文档职责

- [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md)：业务目标、系统范围、角色和不可破坏的业务约束。
- [`ARCHITECTURE.md`](ARCHITECTURE.md)：代码模块、数据模型、关键数据流和生产拓扑。
- [`STATUS.md`](STATUS.md)：当前代码基线、已经验证的事实、待核实事项和近期建议。
- [`features/README.md`](features/README.md)：已实现能力和功能文档维护规则。
- [`decisions/`](decisions/)：需要长期保留的架构与运维决策。
- [`TASK_TEMPLATE.md`](TASK_TEMPLATE.md)：新增功能、修复、体验和运维任务模板。

仓库根目录已有两份规范文档：

- [`../README.md`](../README.md)：运行、部署、环境变量、集成和常用操作。
- [`../OPERATIONS.md`](../OPERATIONS.md)：生产安全、备份恢复、容量和事故处理。

## 长期任务结构

- `00｜快递同步系统｜总控与版本集成`：只做规划、拆分、合并验收和状态维护。
- `10｜生产运维｜Render・SQLite・备份`：默认只读，负责生产证据和风险报告。
- 功能、修复、体验、文档任务：每项一个 Worktree 和分支，合并后归档。

