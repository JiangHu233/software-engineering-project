TASK-S0-01 交付说明与结论
============================

一、交付物
    backend/tests/dataset.py               合成数据集（AV-01 口径：2 房间 × 30 座、3 学生、1 管理员）
    backend/tests/concurrency_harness.py   并发工装（真实文件库 + 线程/进程独立连接）
    backend/tests/test_concurrency.py      22 个用例（含任务卡要求的两组必执行用例）
    evidence/TASK-S0-01/                   本目录

二、必须执行的两组用例及结果

  1) 单进程组：2 线程 × 50 轮，每轮新座位
     用例：test_threads_two_racers_50_rounds
     结果：22/22 通过（其中该用例覆盖 50 轮全部成功数 == 1）
     用时：约 44 秒（整份 test_concurrency.py）

  2) 多进程组：2 进程 × 10 轮
     用例：test_processes_two_racers_10_rounds
     结果：通过 —— 每轮成功数 == 1，失败方返回 409 SLOT_TAKEN

三、重要差异：任务卡写的"当前实现必然失败"已不成立

  任务卡原文：
    "2) 在'当前实现'上运行，多进程组必须失败（这是工具有效性的唯一证明）
     ——2026-11-26 AI 探针实测：3/3 轮两进程均提交成功，即当前实现确有双占缺陷"

  本次实测：
    20 轮多进程压测（3 进程争抢）全部正确：每轮恰好 1 方成功
    直接验证 db：seat_occupancy 唯一约束 u1_occupancy 存在且生效
    写路径确实经过 core/db_txn.py 的 immediate_transaction（BEGIN IMMEDIATE）

  结论：
    任务卡是基于旧基线（src/app.py 单文件版，提交 9d49338）编写的。
    当前代码库已重构为 backend/studyroom/ 模块化结构，并且并发保护已实现：
      - 写事务使用 BEGIN IMMEDIATE 全局写锁（Q-28 裁决 C）
      - seat_occupancy 表有 (seat_id, slot_start) 唯一约束（U-1 数据库兜底）
    因此"多进程组必须失败"这一验证前提已失效，工具有效性改用另一方式证明：
    见下节"工具有效性的替代证明"。

  处理结果（已决策 A 案，2026-11-26，决策人：苏帅栋 246004468）：
    A. 接受现状：任务卡第 2 条已改为"工具有效性证明"（情形 a/b 二者择一），
       当前实现属情形 b，附四组负向对照；并发保护由 S1-03 的验收用例覆盖。
    B. （未采用）用旧提交 9d49338 单独录制失败证据与通过证据并存。
    变更记录见 .tasks/TASK-S0-01.md 的"口径变更记录"一节。

四、工具有效性的替代证明（不依赖"必失败"）

  1. 工装能区分"真并发"与"串行"：
     test_threads_different_seats_both_succeed —— 座位不同时两方都成功，
     证明工装不会因为有多线程就误判冲突。
  2. 工装能发现前置校验问题：
     test_seat_state_must_be_available —— 维护中座位并发抢全部被拒。
  3. 工装能发现脏数据：
     test_failed_racer_leaves_no_reservation —— 失败方若留下预约记录则报错。
  4. 工装能测出库层约束：
     test_threads_exactly_one_occupancy_row 与
     test_processes_db_has_single_occupancy —— 直接查 seat_occupancy 行数。

五、基线既有失败（不在本任务范围，仅登记）

  全量 pytest 结果：59 passed, 6 failed
  6 个失败均为既有用例，与本次新增文件无关，且在本任务开始前就存在：

    test_checkin_checkout.py::test_checkout_early_leave_strict_less   KeyError: 'data'
    test_checkin_checkout.py::test_checkout_after_end_rejected         期望 422 实际 409
    test_checkin_checkout.py::test_seat_freed_from_semantics           UNIQUE 冲突（造数问题）
    test_reservations.py::test_close_hour_24_next_day_midnight         期望 201 实际 422（跨零点）
    test_reservations.py::test_cancel_boundary_exactly_30min           now+30min 执行时已不足 30 分钟
    test_seats.py::test_room_404_and_room_closed                       ORM 会话缓存 room.active=True

  初步判责（详见任务卡之外的单独记录）：
    - 2 个属测试写法脆弱（时间精度、会话缓存）
    - 1 个属造数重复插同一时段
    - 3 个涉及业务语义分歧（跨零点、签退状态码），需人工确认后决定改实现还是改测试

  以上 6 条均未在本任务中修改，符合任务卡"禁止修改既有断言"的要求。

六、禁止修改清单核对

  [x] 未修改 src/app.py 及 backend/studyroom/ 下任何业务代码
  [x] 未修改 tests/test_app.py（当前仓库无此文件；既有 5 个测试文件均未改动）
  [x] 未修改 AV-01 合成数据规模（2×30、3 学生、1 管理员，已由 verify_dataset 断言）
  [x] 未连接 instance/studyroom.db（每个用例独立 tmp_path 临时库）
  [x] 未使用 unittest.mock 替换数据库会话
  [x] tests/conftest.py 未改动（本次未新增 fixture 到该文件）

七、复现方法

  cd 智慧自习室系统/backend
  ./.venv/Scripts/python.exe -m pytest tests/test_concurrency.py -v
  ./.venv/Scripts/python.exe -m pytest -q        # 全量回归

  环境：Python 3.14.0 / Flask 3.1.3 / SQLAlchemy 2.1.9 / pytest 8.4.2
  详见 env.txt
