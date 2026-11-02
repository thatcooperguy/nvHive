"""``GET /v1/outputs/images/{scope}/{filename}`` — generated images behind ``require_auth``.

The Wizard's ``generate_image`` tool and the profile-portrait generator save
what they render under ``$NVH_HOME/outputs/images/<scope>/`` (see
:mod:`nvh.integrations.imagegen`); the card fetches the file from here with
the API's auth headers and renders it as an object URL. Unlike the avatar
route this one is not open: a generated image is user content.

Every bad path answers 404, never 500: ``scope`` and ``filename`` must match
the renderer's regexes (one folder level, one short suffix the renderer
writes), and the resolved file must sit directly inside that scope folder
under the process home's ``outputs/images`` — ``..``, a foreign scope, an
unknown suffix, a symlink that escapes and a missing file are all "no such
image". The response carries ``Cache-Control: private, max-age=300`` so a
browser keeps the inline render for a while without a shared cache ever
holding it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from nvh.api.deps import require_auth
from nvh.integrations.imagegen import media_type_for, resolve_output_image

router = APIRouter()

CACHE_CONTROL = "private, max-age=300"
NOT_FOUND_DETAIL = "No such image"


@router.get(
    "/v1/outputs/images/{scope}/{filename}",
    summary="Serve one generated image from $NVH_HOME/outputs/images/<scope>/",
    response_class=FileResponse,
)
async def get_output_image(
    scope: str,
    filename: str,
    _auth: Any = Depends(require_auth),
) -> FileResponse:
    path = resolve_output_image(scope, filename)
    media_type = media_type_for(filename)
    if path is None or media_type is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    return FileResponse(
        str(path),
        media_type=media_type,
        headers={"Cache-Control": CACHE_CONTROL},
    )
