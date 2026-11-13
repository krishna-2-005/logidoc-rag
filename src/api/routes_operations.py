"""
HTTP API: operations for administrators (permission admin.maintenance).

    GET  /api/admin/backups    the schedule, the last attempts and the backups
    POST /api/admin/backups    a backup now (202; 409 while one runs). It holds
                               the ingestion lock while it copies, so uploads
                               wait for it
    GET  /api/admin/metrics    the process's metrics as JSON, or with
                               ?format=prometheus the Prometheus text format
    GET  /api/admin/alerts     active alerts and the recent alert events

Restores are not offered over HTTP: they run from the command line on the
server (scripts/manage_backups.py restore), into a separate folder.
Nothing here returns a path, a URL or a secret.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

import authz
import backup
import backup_automation
import metrics
from api import schemas
from api.core import ApiState, get_state, require

router = APIRouter(prefix="/api/admin", tags=["operations"])

Maintain = Depends(require(authz.ADMIN_MAINTENANCE))

STATUS_FIELDS = ("last_attempt", "last_success", "last_failure", "consecutive_failures")


class BackupBody(BaseModel):

    label: str | None = Field(default=None, max_length=80)


class VerifyBody(BaseModel):

    restore_drill: bool = False


@router.get("/backups", response_model=schemas.BackupOverview, responses=schemas.ERRORS)
def backups(principal=Maintain, state: ApiState = Depends(get_state)):

    operations = state.operations
    settings = operations.backup_settings
    status = operations.runner.status()
    latest = backup_automation.latest_verifications(settings.root)

    def last_scheduled(kind):
        return backup_automation.last_scheduled_check(settings.root, kind)

    return {
        "schedule": settings.schedule.describe(),
        "running": operations.runner.running,
        "retention": {"daily": settings.retention.daily, "weekly": settings.retention.weekly,
                      "monthly": settings.retention.monthly},
        "status": {name: status.get(name) for name in STATUS_FIELDS if status.get(name) is not None},
        "backups": [{**item, "last_check": latest.get(item["backup_id"])} for item in backup.list_backups(settings.root)],
        "verification": {"verify_hours": settings.verify_hours, "drill_hours": settings.drill_hours,
                         "last_verification": last_scheduled(backup_automation.VERIFY),
                         "last_drill": last_scheduled(backup_automation.DRILL)},
    }


@router.get("/backups/checks", response_model=schemas.BackupChecks, responses=schemas.ERRORS)
def backup_checks(principal=Maintain, state: ApiState = Depends(get_state), limit: int = Query(100, ge=1, le=1000)):
    """Recorded verifications and restore drills, newest first (append-only history beside the backups)."""

    return {"checks": backup_automation.read_verifications(state.operations.backup_settings.root, limit=limit)}


@router.post("/backups/{backup_id}/verify", status_code=202, response_model=schemas.BackupStarted,
             responses={**schemas.ERRORS, 409: {"model": schemas.ErrorResponse, "description": "A backup or check is running"}})
def verify_backup(backup_id: str, body: VerifyBody, principal=Maintain, state: ApiState = Depends(get_state)):
    """
    Checks one backup again now (manifest, checksums, integrity, recorded
    contents); with restore_drill, also restores it into an isolated folder and
    checks it like a real restore. The result is added to the history.
    """

    try:
        state.operations.start_verification(backup_id, drill=body.restore_drill)
    except backup.BackupBusy as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except backup.RestoreRefused as error:
        status = 404 if str(error).startswith(("No backup", "That is not")) else 422
        raise HTTPException(status_code=status, detail=str(error)) from None

    return {"started": True}


@router.post("/backups", status_code=202, response_model=schemas.BackupStarted,
             responses={**schemas.ERRORS, 409: {"model": schemas.ErrorResponse, "description": "A backup is running"}})
def start_backup(body: BackupBody, principal=Maintain, state: ApiState = Depends(get_state)):

    try:
        state.operations.start_backup(label=body.label)
    except backup.BackupBusy as error:
        raise HTTPException(status_code=409, detail=str(error)) from None

    return {"started": True}


@router.get("/metrics", responses={**schemas.ERRORS, 200: {"description": "JSON, or the Prometheus text format",
                                                         "content": {"application/json": {}, "text/plain": {}}}})
def metrics_view(format: Literal["json", "prometheus"] = Query("json"), principal=Maintain):

    if format == "prometheus":
        return PlainTextResponse(metrics.REGISTRY.prometheus(), media_type="text/plain; version=0.0.4")

    return metrics.REGISTRY.snapshot()


@router.get("/alerts", response_model=schemas.AlertsView, responses=schemas.ERRORS)
def alerts_view(principal=Maintain, state: ApiState = Depends(get_state)):

    return state.operations.alerts.view()
