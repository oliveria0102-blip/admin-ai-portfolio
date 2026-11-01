---
name: long-task-orchestrator
description: Break an explicitly scoped multi-step task into trackable work items, execute batches, and reconcile completion evidence.
---

# 长任务编排 · 公开示例

本 Skill 展示与机构业务无关的编排机制。业务内容由调用方的业务 Skill 提供，编排器不替代业务判断。

1. 确认任务范围、交付标准与业务 owner，形成 TaskContract。
2. 用 `scripts/init_task_package.py` 初始化独立任务包；用 `scripts/enumerate_work_items.py` 生成工作项。
3. 由一个进程调用 `scripts/claim_batch.py` 领取批次；按工作项执行授权范围内的操作。
4. 用 `scripts/update_item_state.py` 记录状态，用 `scripts/append_event.py` 保留事件。
5. 中断后通过 `scripts/resume_task.py` 检查未完成工作；用 `scripts/reconcile_task.py` 对账。

不因到达批次上限就宣布全任务完成。失败项保留状态和原因；不能把尝试过、已排队或已配置视为已交付。

本示例不包含业务账号、外部连接器或真实任务包。批次领取当前要求单进程执行，详见脚本说明。
