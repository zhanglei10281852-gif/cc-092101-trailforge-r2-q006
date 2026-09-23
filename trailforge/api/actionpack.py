from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from trailforge.api.dependencies import get_session
from trailforge.schemas.actionpack import ImportReport
from trailforge.services.actionpack import ActionPackService

router = APIRouter(prefix="/action-packs", tags=["action-packs"])
SessionDep = Annotated[Session, Depends(get_session)]

EXPORT_MEDIA_TYPE = "application/json"
EXPORT_FILENAME = "action-pack.json"


def _secret(request: Request) -> str | None:
    secret = request.app.state.settings.action_pack_secret
    return secret or None


@router.post(
    "/expeditions/{expedition_id}/export",
    status_code=status.HTTP_200_OK,
    responses={200: {"content": {EXPORT_MEDIA_TYPE: {}}}},
)
def export_pack(
    expedition_id: int,
    request: Request,
    session: SessionDep,
    actor_id: int = Query(gt=0),
) -> Response:
    raw, summary = ActionPackService(session, secret=_secret(request)).export_pack(
        expedition_id, actor_id=actor_id
    )
    filename = f"action-pack-{expedition_id}-{summary.pack_id}.json"
    return Response(
        content=raw,
        media_type=EXPORT_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", response_model=ImportReport)
async def import_pack(
    request: Request,
    session: SessionDep,
    actor_id: int | None = Query(default=None, gt=0),
) -> ImportReport:
    raw = await request.body()
    return ActionPackService(session, secret=_secret(request)).import_pack(
        raw, actor_id=actor_id
    )
