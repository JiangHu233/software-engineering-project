from __future__ import annotations

# §3.4 错误码字典（唯一事实来源）：code -> (HTTP, 默认中文文案, rule)
ERRORS: dict[str, tuple[int, str, str | None]] = {
    "VALIDATION_FAILED": (400, "请求字段缺失或格式非法。", "NFR-09"),
    "UNAUTHENTICATED": (401, "未认证或认证已失效。", "NFR-07"),
    "ACCOUNT_DISABLED": (401, "账号已停用。", "ADR-1"),
    "FORBIDDEN_ROLE": (403, "角色无权执行该操作。", "F-3"),
    "FORBIDDEN_OBJECT": (403, "无权访问该对象。", "P-3"),
    "NOT_FOUND": (404, "目标资源不存在。", None),
    "BLACKLISTED": (422, "你当前处于预约限制期，暂不能创建预约。", "F-8"),
    "RESERVATION_BLOCKED": (422, "预约被业务规则拒绝。", "决策表7.1"),
    "SLOT_TAKEN": (409, "该时段已被占用，请选择其他座位或时段。", "BR-04"),
    "CANCEL_TOO_LATE": (422, "距预约开始不足取消时限，无法取消。", "BR-05"),
    "RESERVATION_STATE_CONFLICT": (409, "预约当前状态不允许该操作。", "BR-08"),
    "CHECKIN_WINDOW_CLOSED": (422, "当前不在签到时间窗内。", "BR-08"),
    "CHECKOUT_NOT_ALLOWED": (422, "已超过预约结束时间，无法签退。", "P-5"),
    "DUPLICATE_REQUEST": (409, "相同请求标识的请求正在处理中，请稍后重试。", "§3.7"),
    "IDEMPOTENCY_KEY_CONFLICT": (422, "请求内容与首次提交不一致，请勿复用请求标识。", "§3.7"),
    "ROOM_INACTIVE": (422, "自习室已停用。", "FR-01"),
    "SEAT_UNAVAILABLE": (422, "座位当前不可预约。", "F-4"),
    "SEAT_LABEL_DUPLICATED": (409, "同一自习室内座位编号重复。", "DD02"),
    "ROOM_NAME_DUPLICATED": (409, "自习室名称已存在。", "FR-01"),
    "SCHEDULE_INVALID": (422, "自习室开放时间非法。", "BR-02"),
    "RULE_PARAM_INVALID": (422, "规则参数取值越界。", "NFR-12"),
    "RULE_PARAM_LOCKED": (409, "参数已被他人修改，请刷新后重试。", "NFR-12"),
    "LAST_ADMIN_FORBIDDEN": (422, "该操作会使系统失去全部可用管理员。", "Q-23"),
    "DUPLICATE_VIOLATION": (409, "该预约已存在违约记录。", "BR-11"),
    "REASON_REQUIRED": (400, "必填原因为空。", "BR-12"),
    "RATE_LIMITED": (429, "请求过于频繁，请稍后再试。", "NFR-07"),
    "INTERNAL_ERROR": (500, "服务内部错误，请稍后重试。", "NFR-01"),
}


class ApiError(Exception):
    """抛出后由 handlers.py 统一序列化为 {error:{code,message,fields,rule,requestId}}。"""

    def __init__(self, code: str, *, message: str | None = None,
                 fields: dict | None = None, rule: str | None = None):
        http, default_message, default_rule = ERRORS[code]
        self.code = code
        self.http_status = http
        self.message = message or default_message
        self.fields = fields or {}
        self.rule = rule or default_rule
        super().__init__(f"{code}: {self.message}")


def validation_failed(fields: dict) -> ApiError:
    return ApiError("VALIDATION_FAILED", fields=fields)
