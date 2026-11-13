"""Articles: list and creation from a Bambuddy print file (Spec 5.1, 5.2).

Bambuddy is only read here, through `BambuddyClient`. When creating, the file, the
plates, the filament need and the preview are read again from Bambuddy on submit
and nothing is taken from hidden fields.
"""

from fastapi import APIRouter

from reprintbuddy.web.routes.articles import archive, browse, create, detail
from reprintbuddy.web.routes.articles.form import active_parts, change_part_rows, parse_parts, part_rows

__all__ = ["router", "active_parts", "change_part_rows", "parse_parts", "part_rows"]

router = APIRouter()
router.include_router(browse.router)
router.include_router(create.router)
router.include_router(detail.router)
router.include_router(archive.router)
