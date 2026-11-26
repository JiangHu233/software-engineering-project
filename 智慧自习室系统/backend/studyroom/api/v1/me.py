from __future__ import annotations

"""API-10 我的预约列表 / API-12 我的违约与信用状态（§4.10/4.12）。

数据范围服务端强制注入：STU 一律 userId=currentUser（§3.5）。
eligibility 与 summary 由服务端计算，前端只渲染（§6.3）。
"""
from flask import g, request

from ...core.auth import login_required
from ...core.pagination import (apply_sort, parse_enum_list, parse_from_to,
                                parse_pagination)
from ...core.responses import ok, page_meta
from ...extensions import db
from ...models import ACTIVE_STATUSES, Reservation, Violation
from ...services import credit_service, reservation_service, rule_params
from ...services.timeutil import iso, iso_date
from ...services.violation_service import MASKED_TEMPLATES
from .. import api_bp

RSV_STATUSES = {"BOOKED", "CHECKED_IN", "COMPLETED", "CANCELLED", "VIOLATED"}
VIOLATION_KINDS = {"NO_SHOW", "NO_CHECKOUT", "EARLY_LEAVE", "ON_SITE"}

_SORT_FIELDS = {
    "startsAt": Reservation.starts_at,
    "endsAt": Reservation.ends_at,
    "status": Reservation.status,
    "createdAt": Reservation.created_at,
}


def _item_payload(r: Reservation) -> dict:
    seat = r.seat
    return {
        "reservationId": r.id,
        "status": r.status,
        "seatId": seat.id, "seatLabel": seat.label,
        "roomId": seat.room_id, "roomName": seat.room.name,
        "startsAt": iso(r.starts_at), "endsAt": iso(r.ends_at),
        "checkedInAt": iso(r.checked_in_at), "checkedOutAt": iso(r.checked_out_at),
        "cancelledAt": iso(r.cancelled_at),
        "terminatedBy": r.terminated_by,
        # 三个 eligibility 服务端计算（§4.10 固定算式，与 API-07/08/09 共用）
        **reservation_service.eligibility(r),
    }


@api_bp.get("/me/reservations")
@login_required(roles=["STU"])
def my_reservations():
    page, page_size = parse_pagination()
    dt_from, dt_to = parse_from_to()
    statuses = parse_enum_list("status", RSV_STATUSES)
    room_id = request.args.get("roomId", type=int)

    query = Reservation.query.filter(Reservation.user_id == g.user.id)
    if statuses:
        query = query.filter(Reservation.status.in_(statuses))
    if dt_from and dt_to:
        query = query.filter(Reservation.starts_at >= dt_from, Reservation.starts_at < dt_to)
    if room_id:
        query = query.filter(Reservation.seat.has(room_id=room_id))
    query = apply_sort(query, _SORT_FIELDS,
                       [(Reservation.starts_at, True), (Reservation.id, True)],
                       request.args.get("sort"))
    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()

    active_count = Reservation.query.filter(
        Reservation.user_id == g.user.id, Reservation.status.in_(ACTIVE_STATUSES)).count()
    items = [_item_payload(r) for r in rows]
    return ok({"items": items}, meta=page_meta(page, page_size, total, activeCount=active_count))


@api_bp.get("/me/violations")
@login_required(roles=["STU"])
def my_violations():
    page, page_size = parse_pagination()
    dt_from, dt_to = parse_from_to()
    kinds = parse_enum_list("kind", VIOLATION_KINDS)
    include_original = request.args.get("includeOriginalReason", "").lower() in ("1", "true", "yes")

    _, params = rule_params.current()
    term_start = credit_service.term_start(params)
    # summary 与 items 同一读快照（§4.12）
    score = credit_service.term_score(g.user.id)
    active_bl = credit_service.active_blacklist(g.user.id)
    entries = credit_service.entries_in_term(g.user.id)

    query = Violation.query.filter(Violation.user_id == g.user.id)
    if kinds:
        query = query.filter(Violation.kind.in_(kinds))
    if dt_from and dt_to:
        query = query.filter(Violation.created_at >= dt_from, Violation.created_at < dt_to)
    query = query.order_by(Violation.created_at.desc(), Violation.id.desc())
    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()

    items = []
    for v in rows:
        r = v.reservation
        item = {
            "violationId": v.id,
            "kind": v.kind,
            "reason": MASKED_TEMPLATES.get(v.kind, v.reason),
            "createdAt": iso(v.created_at),
            "reservationId": v.reservation_id,
            "reservationSummary": {
                "seatLabel": r.seat.label, "roomName": r.seat.room.name,
                "startsAt": iso(r.starts_at), "endsAt": iso(r.ends_at),
            },
        }
        if include_original:
            # 原文需二次确认并写审计（Q-20 裁决 C）
            from ...models import add_audit
            from ...core.db_txn import immediate_transaction
            with immediate_transaction():
                add_audit("VIEW_ORIGINAL_REASON", target_type="violation", target_id=v.id)
            item["reasonOriginal"] = v.reason
        items.append(item)

    summary = {
        "termStart": iso_date(term_start.date()),
        "violationScore": score,
        "blacklistActive": active_bl is not None,
        "blacklistUntil": iso(active_bl.blacklist_until) if active_bl else None,
        "entriesInTerm": entries,
        "nextThreshold": int(params["violationScoreThreshold"]),  # 读当前生效版本，不硬编码
    }
    return ok({"summary": summary, "items": items},
              meta=page_meta(page, page_size, total))
