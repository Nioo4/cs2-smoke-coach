"""The single static source of truth for the Mirage map asset."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MapAssetStatus = Literal["placeholder", "approved"]


@dataclass(frozen=True, slots=True)
class MapCatalog:
    slug: Literal["mirage"]
    name_zh: str
    asset_url: str
    asset_status: MapAssetStatus


_MIRAGE = MapCatalog(
    slug="mirage",
    name_zh="荒漠迷城",
    asset_url="/assets/maps/mirage.svg",
    asset_status="approved",
)


def get_map_catalog() -> MapCatalog:
    """Return the immutable map metadata used by every map-state guard."""

    return _MIRAGE
