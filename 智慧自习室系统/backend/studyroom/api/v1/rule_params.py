from __future__ import annotations

"""API-19 规则参数 GET/PUT（§4.19）：版本化 + 乐观锁 + reason 四项启发式校验。"""
from datetime import datetime

from flask import g, request

from ...core import idempotency
from ...core.auth import login_required
from ...core.db_txn import immediate_transaction
from ...core.errors import ApiError
from ...core.responses import ok
from ...extensions import db
from ...models import RuleParameter, add_audit
from ...schemas.common import field_int, field_str, get_json
from ...services import rule_params
from ...services.timeutil import iso
from .. import api_bp


@api_bp.get("/admin/rule-parameters")
@login_required(roles=["ADM", "OPS"])
def get_rule_parameters():
    row = rule_params.current_row()
    if row is None:
        version, params = rule_params.current()
        return ok({"version": version, "effectiveFrom": None, "params": params})
    return ok({
        "version": row.version,
        "effectiveFrom": iso(row.effective_from),
        "params": row.params,
    })


@api_bp.put("/admin/rule-parameters")
@login_required(roles=["ADM"])
def put_rule_parameters():
    key, body_hash = idempotency.require_key()
    replay = idempotency.precheck(g.user.id, key, body_hash)
    if replay is not None:
        return replay

    body = get_json()
    version = field_int(body, "version", required=True)
    reason = field_str(body, "reason", max_len=500)
    params_in = body.get("params")
    if not isinstance(params_in, dict) or not params_in:
        raise ApiError("VALIDATION_FAILED", fields={"params": ["params 必须为非空对象"]})

    rule_params.validate_reason(reason)          # 400：四项内容启发式（Q-25 裁决 C）
    rule_params.validate_update(params_in)       # 422：逐字段范围（RULE_PARAM_INVALID）

    current_row = rule_params.current_row()
    current_version = current_row.version if current_row else 1
    if version != current_version:               # 409：乐观锁（U-7 语义）
        raise ApiError("RULE_PARAM_LOCKED", fields={"currentVersion": current_version})

    with immediate_transaction():
        idempotency.register(key, body_hash)
        # 合成新版本 = 当前生效参数 ∪ 本次提交
        merged = dict(rule_params.current()[1])
        merged.update(params_in)
        if current_row is not None:
            current_row.effective_to = datetime.now()
            db.session.add(current_row)
        new_row = RuleParameter(scope="GLOBAL", version=current_version + 1,
                                created_by=g.user.id, reason=reason)
        new_row.params = merged
        db.session.add(new_row)
        db.session.flush()
        add_audit("RULE_PARAM_CHANGE", target_type="rule_parameter", target_id=new_row.id,
                  reason=reason, rule_version=new_row.version)
        envelope = {"data": {
            "version": new_row.version,
            "params": new_row.params,
            "appliesTo": {"fromNow": True, "existingReservationsAffected": False},
        }, "meta": {"requestId": getattr(g, "request_id", None)}}
        idempotency.finish(200, envelope)

    rule_params.invalidate_cache()  # 事务提交后主动失效进程缓存（裁定#12）
    return ok(envelope["data"])
