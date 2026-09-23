from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from trailforge.api.dependencies import get_session
from trailforge.errors import (
    ActionPackConflictError,
    PackageIntegrityError,
    PackValidationError,
)
from trailforge.schemas.offline import ImportResult
from trailforge.services.offline import OfflinePackService

router = APIRouter(prefix="/offline", tags=["offline-action-packs"])
SessionDep = Annotated[Session, Depends(get_session)]


class ActionPackResponse(Response):
    """以 UTF-8 直出行动包（不转义中文），与离线文件格式保持一致。"""

    media_type = "application/json; charset=utf-8"

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8")


@router.post(
    "/expeditions/{expedition_id}/action-pack/export",
    status_code=status.HTTP_200_OK,
    responses={200: {"content": {"application/json": {}}}},
)
def export_action_pack(
    expedition_id: int,
    session: SessionDep,
    actor_id: int = Query(gt=0),
) -> ActionPackResponse:
    """导出单个活动的离线行动包（稳定 UTF-8 JSON，含校验摘要）。"""

    pack, _summary = OfflinePackService(session).export_pack(expedition_id, actor_id=actor_id)
    filename = f"action-pack-{expedition_id}-{pack['manifest']['pack_id']}.json"
    return ActionPackResponse(
        pack,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/action-packs/import", response_model=ImportResult)
def import_action_pack(
    pack: dict[str, Any],
    session: SessionDep,
    actor_id: int = Query(gt=0),
    on_conflict: str = Query(default="abort", pattern="^(abort|skip_conflicts)$"),
) -> ImportResult:
    """导回离线行动包。

    默认 ``abort``：任何条目冲突都会整包拒绝（同一事务，零写入）并返回
    结构化冲突清单；``skip_conflicts`` 时跳过冲突条目，其余原子提交。
    """

    try:
        return OfflinePackService(session).import_pack(
            pack, actor_id=actor_id, on_conflict=on_conflict
        )
    except (PackageIntegrityError, PackValidationError, ActionPackConflictError) as exc:
        # 请求会话随异常回滚关闭后，由异常处理器在独立事务中写拒绝审计。
        exc.pack = pack  # type: ignore[attr-defined]
        exc.actor_id = actor_id  # type: ignore[attr-defined]
        raise
