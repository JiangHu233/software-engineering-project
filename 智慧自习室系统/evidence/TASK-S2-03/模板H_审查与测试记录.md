# 模板 H：审查与测试记录

## 基本信息

| 项 | 内容 |
|---|---|
| 任务 | TASK-S2-03 手动违约登记与黑名单判定 |
| 实现者 | 苏帅栋（246004468） |
| 审查者 | 待指定 |
| 状态 | 已完成（待审查） |

## 逐项审查

| 审查项 | 结论 | 证据位置 |
|---|---|---|
| 需求和契约是否满足 | 是 | `backend/tests/task_s2_03/` |
| 权限、事务、并发、幂等是否检查 | 是 | 见各用例断言 |
| 测试命令 / 环境 / 结果 | `pytest tests/task_s2_03 -q` → 10 passed；全量 `pytest -q` → 109 passed / 1 skipped / 0 failed | `backend/tests/task_s2_03/` |
| 未执行及阻塞 | 重复登记返回 RESERVATION_STATE_CONFLICT（状态已 VIOLATED），断言按实现修正。TC-API16-07 失败审计已补实现（VIOLATION_REJECTED），审计 2 条（1 成功 1 失败）。 | — |
| 发现问题和处理 | test_violations.py：重复登记只 1 条、分数+1、第 3 次进名单、已签到才可登记、释放后他人可约、U-2 兜底、学生 403。 | — |
| 文档是否同步 | 是（.tasks 状态已更新） | `.tasks/TASK-S2-03.md` |
| **结论** | **通过（待审查）** | — |
