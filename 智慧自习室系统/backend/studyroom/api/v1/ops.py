from __future__ import annotations

"""API-20 运维指标（§4.20）：仅 OPS，全部标量计数，无任何个人字段。"""
from flask import request

from ...core.auth import login_required
from ...core.errors import ApiError
from ...core.responses import ok
from ...services import ops_metrics
from .. import api_bp

_WINDOWS = {"1m", "5m", "1h"}


@api_bp.get("/ops/metrics")
@login_required(roles=["OPS"])
def metrics():
    window = request.args.get("window", "5m")
    if window not in _WINDOWS:
        raise ApiError("VALIDATION_FAILED", fields={"window": ["window 只允许 1m/5m/1h"]})
    return ok(ops_metrics.metrics(window))
