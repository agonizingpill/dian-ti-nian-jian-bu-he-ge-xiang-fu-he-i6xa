"""HTTP surface for the daily supervisor spot-check quota."""

from datetime import date as date_cls

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import spotcheck_service as service
from .auth import current_user, require_roles
from .utils.audit import log_action

router = APIRouter(prefix="/api/spotchecks", tags=["主管抽查名额"])

# Every rejection carries the freshly recomputed roster so the losing
# supervisor immediately sees who took the seat.
_CONFLICT = {
    "lost": "该单名额已满，已落选，无法认领",
    "taken": "该席位已被其他主管认领，请查看更新后的名单",
    "lift_taken": "该电梯当天已有席位被认领",
}


class ClaimIn(BaseModel):
    job_id: int
    date: date_cls | None = None


@router.get("")
def allocation(date: date_cls | None = None, user=Depends(current_user)):
    return service.allocation(date)


@router.post("/claims", status_code=201)
def claim(payload: ClaimIn, user=Depends(require_roles("supervisor"))):
    outcome, seat, day = service.claim(payload.date, payload.job_id, user)
    if outcome == "unavailable":
        raise HTTPException(404, "该分流记录尚未出结论，或不属于抽取当天")
    if outcome == "retry":
        raise HTTPException(503, "席位正在被其他主管认领，请稍后重试")
    if outcome == "claimant_invalid":
        raise HTTPException(401, "登录信息已失效，请重新登录")
    if outcome in _CONFLICT:
        snapshot = service.allocation(day)
        detail = _CONFLICT[outcome]
        if seat and seat.get("claimed_by_name"):
            detail = detail.replace("其他主管", seat["claimed_by_name"]).replace(
                "被认领", f"由 {seat['claimed_by_name']} 认领")
        raise HTTPException(409, {"code": outcome.upper(), "message": detail,
                                  "job_id": payload.job_id, "roster": snapshot})
    if outcome == "created":
        log_action(int(user["sub"]), "claim_spotcheck", "analysis_job", payload.job_id,
                   {"date": day.isoformat(), "job_id": payload.job_id})
    snapshot = service.allocation(day)
    return {"created": outcome == "created", "outcome": outcome, "job_id": payload.job_id, **snapshot}
