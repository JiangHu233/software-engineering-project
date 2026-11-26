任务编号/名称：TASK-S1-01 / 数据模型与唯一约束
负责人/审查人：苏帅栋（246004468） / 待指定
关联阶段、需求、ADR、接口和测试：S1；BR-04、FR-13、FR-20、NFR-06、NFR-12、NFR-13；
                                  ADR-002、ADR-5、ADR-10（去重索引预留）；Q-28；
                                  《接口说明书》§5.3 U-1～U-8、§5.10 预留列；TC-DB-01、TC-DB-02
目标：即使绕过应用层直接写库，数据库也会拒绝"同座位同时段双占"与"同预约双违约"。
输入基线：TASK-S0-01 交付物；《接口说明书》v1.0-draft §5.3
允许修改：数据模型定义、建表/迁移脚本、模型层文件、tests/test_models.py、evidence/TASK-S1-01/
禁止修改：既有业务接口的对外语义；验收预期；不得为让约束通过而放宽约束或修改需求
前置依赖：TASK-S0-01；Q-28（数据库选型决定"部分唯一索引"写法：MySQL 生成列 / SQLite-PG 部分索引）
事务、权限、幂等约束：
  - U-1 UNIQUE(seat_id, slot_start)，释放时删除行（§5.3 方案 A）
  - U-2 UNIQUE(reservation_id) 违约表
  - U-3 UNIQUE(room_id, label)、U-4 UNIQUE(name)
  - U-6 UNIQUE(actor_id, method, path, key) 幂等表
  - U-7 UNIQUE(scope, version) 参数版本表
  - 审计表只增不改：应用层不得提供 update/delete 路径
  - §5.10 预留列一次建齐：reservation.operator_id/operator_reason/created_via、
    violation.waived_at/waived_reason、blacklist.source、audit_log、notification outbox
必执行测试：
  1) 约束层 TC-DB-01：直接两条 INSERT 同座位同时段 → 一条必须 IntegrityError
  2) 约束层 TC-DB-02：两条同 reservation_id 的违约 INSERT → 一条必须失败
  3) U-3/U-4 冲突测试（同房重号、同名自习室）
  4) 迁移/建表脚本可重复执行且幂等
完成标准：约束存在并被测试实际触发；约束异常能被映射为 409 的异常类型；
          证据含"绕过应用层直接写库"的失败日志；结构经人工审查
停止条件：Q-28 未定而无法确定部分唯一索引写法时先反馈，不得凭猜测选定并在文档外固化
状态：已完成（待审查）
