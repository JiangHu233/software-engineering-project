# 模板 H：审查与测试记录

## 基本信息

| 项 | 内容 |
|---|---|
| 任务 | TASK-S0-01 合成数据与并发 harness |
| 分支 | `task/TASK-S0-01` |
| fork 基线 | `b178fb9`（dev-ssd，含 .gitignore 提交） |
| 实现者 | 苏帅栋（246004468） |
| 审查者 | 待指定 |
| 提交标识 | 见下方"提交记录" |

## 逐项审查

| 审查项 | 结论 | 证据位置 |
|---|---|---|
| 需求和契约是否满足 | 是。合成数据严格符合 AV-01 口径（2 房间 × 30 座、3 学生、1 管理员），由 `verify_dataset()` 断言，对应用例 `test_dataset_scale_matches_av01` | `backend/tests/dataset.py` `DATASET_SPEC`；`test_concurrency.py::test_dataset_scale_matches_av01` |
| 权限、事务、并发、幂等是否检查 | 是。并发：单进程 2 线程 × 50 轮、多进程 2 进程 × 10 轮、3 方争抢；幂等：`test_repeat_idempotency_key_returns_same_result`；事务：写路径经 `immediate_transaction`（BEGIN IMMEDIATE），库层 U-1 唯一约束兜底 | `test_concurrency.py` 全部并发用例；`backend/studyroom/core/db_txn.py` |
| 测试命令 / 环境 / 结果位置 | `pytest tests/test_concurrency.py -v` → 22 passed；`pytest -q` → 59 passed / 6 failed（6 条为既有失败） | `evidence/TASK-S0-01/run_concurrency_verbose.txt`、`run_full_regression.txt`、`env.txt` |
| 未执行及阻塞 | 无阻塞。唯一未做的是"在旧提交上录制失败证据"，见下方"发现问题和处理"第 1 条 | — |
| 发现问题和处理 | 见下表 | — |
| 文档是否同步 | 是。已回填《分阶段实现与测试计划》§4.2 的实际命令 | `.docs/架构设计/分阶段实现与测试计划.md` |
| **结论** | **通过**（口径变更已由决策人确认，见问题 1） | — |

## 发现的问题与处理

### 1. 任务卡的"必失败"前提已失效（已按 A 案处理）

**现象**：任务卡要求"在'当前实现'上运行，多进程组必须失败"，理由是当时存在双占缺陷。本次实测多进程组**通过**：20 轮压测每轮恰好 1 方成功，失败方返回 `409 SLOT_TAKEN`；直接查库确认 `seat_occupancy` 只有 1 行占用。

**原因**：任务卡基于旧基线（单文件 `src/app.py`，提交 `9d49338`）编写。当前代码已重构为 `backend/studyroom/`，并发保护已经实现：
- `core/db_txn.py` 的 `immediate_transaction()` 使用 `BEGIN IMMEDIATE` 全局写锁
- `seat_occupancy` 表有 `u1_occupancy`（`seat_id, slot_start`）唯一约束

**处理（已决策，A 案）**：
- 本任务**未**为了让测试"按预期失败"而削弱实现或放宽断言；
- 工具有效性改用 4 个不依赖"必失败"的用例证明（见 `evidence/TASK-S0-01/README.txt` 第四节）；
- **决策人：苏帅栋（246004468），2026-11-26，选择 A 案**：修改任务卡第 2 条为"工具有效性证明"，
  接受当前实现已修复；已同步修改 `.tasks/TASK-S0-01.md`（含口径变更记录）与计划文档 §4.2.1。

### 2. 基线既有 6 个失败用例

**现象**：全量 pytest 为 59 passed / 6 failed。6 条均为本任务开始前就存在的既有失败。

**处理**：按要求"先判责、不擅改"，本任务**未修改**这 6 条。初步判责：
- 测试写法脆弱 2 条：`test_cancel_boundary_exactly_30min`（`now+30min` 后请求执行已不足 30 分钟）、`test_room_404_and_room_closed`（ORM 会话缓存 `room.active`）
- 造数重复 1 条：`test_seat_freed_from_semantics`（重复插同一 `slot_start`）
- 业务语义待定 3 条：`test_close_hour_24_next_day_midnight`（跨零点）、`test_checkout_after_end_rejected`（期望 422 实际 409）、`test_checkout_early_leave_strict_less`（响应结构）

**待办**：这 6 条需要单独建卡处理，不能靠删断言让全绿。

### 3. 仓库 `.venv` 被误提交（已在本分支处理）

**现象**：`development` 分支 `f4121b9` 提交了完整 `.venv`（3824 个文件，含 `.pyc`），且指向组员机器路径 `C:\Users\admin\...`，在本机无法使用。

**处理**：已在本分支提交 `b178fb9`：新增 `.gitignore`，将 `.venv`、`__pycache__` 移出版本控制，本地重建 venv 用于运行测试。

**遗留**：远程 `development` 分支仍带着这个 `.venv`，需要由提交者或组长决定清理方式（新增清理提交，不推荐改写历史）。

## 测试用例清单（22 条）

| # | 用例 | 验证点 |
|---|---|---|
| 1 | `test_dataset_scale_matches_av01` | AV-01 规模：2×30、3 学生、1 管理员 |
| 2 | `test_dataset_build_is_idempotent` | 重复构建不产生重复数据 |
| 3 | `test_dataset_seat_ids_are_unique_and_ordered` | 座位 id 唯一有序，越界抛错 |
| 4 | `test_dataset_rooms_are_distinct` | 两房间独立，各 30 座 |
| 5 | `test_dataset_spec_is_immutable` | 规格不可被篡改（防绕过 AV-01） |
| 6 | `test_threads_two_racers_50_rounds` | **必执行 1**：2 线程 × 50 轮，每轮成功数 == 1 |
| 7 | `test_threads_three_racers_single_winner` | 3 线程争抢，1 成功 2 冲突 |
| 8 | `test_threads_exactly_one_occupancy_row` | 库层只留 1 条占用 |
| 9 | `test_threads_different_seats_both_succeed` | 座位不同不误判冲突 |
| 10 | `test_threads_same_student_same_slot_blocked` | 同一学生并发，最多成功 1 次 |
| 11 | `test_threads_cross_room_seats_independent` | 跨房间互不影响 |
| 12 | `test_processes_two_racers_10_rounds` | **必执行 2**：2 进程 × 10 轮，成功数 == 1 且失败方冲突语义 |
| 13 | `test_processes_db_has_single_occupancy` | 多进程后库中仅 1 条占用 |
| 14 | `test_processes_three_racers_single_winner` | 3 进程争抢，1 成功 2 冲突 |
| 15 | `test_conflict_only_for_overlapping_slot` | 相邻不重叠时段都可成功 |
| 16 | `test_repeat_idempotency_key_returns_same_result` | 同一幂等键不产生第二条预约 |
| 17 | `test_failed_racer_leaves_no_reservation` | 失败方不留脏数据 |
| 18 | `test_racer_cannot_hold_two_seats_concurrently` | P-1 上限（2 个）不被突破 |
| 19 | `test_seat_state_must_be_available` | 维护中座位并发抢全被拒 |
| 20 | `test_racers_see_consistent_winner_across_views` | 胜者视角一致 |
| 21 | `test_harness_reports_worker_identity` | 工装可定位到具体 worker |
| 22 | `test_no_connection_to_default_instance_db` | 护栏：不连演示库 |

## 提交记录

```
cb6639f test(TASK-S0-01): 合成数据与并发验证工装
b178fb9 chore: 添加 .gitignore 并将 .venv/__pycache__ 移出版本控制
```
