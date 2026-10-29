from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.api import admin
from app.main import app


def test_admin_routes_use_json_body_guard_and_explicit_responses() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    assert paths["/api/v1/admin/session"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/AdminSessionOut")
    assert paths["/api/v1/admin/maps/mirage"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/MapAsset")
    assert (
        paths["/api/v1/admin/targets"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["type"]
        == "array"
    )
    assert paths["/api/v1/admin/targets"]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/PointOut")
    assert paths["/api/v1/admin/lineups"]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/AdminLineupOut")
    assert paths["/api/v1/admin/lineups/{lineup_id}/video"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/AdminLineupOut")


def test_authenticated_map_catalog_uses_the_static_source(monkeypatch) -> None:
    app.dependency_overrides[admin.require_admin] = lambda: "test-admin"
    monkeypatch.setattr(
        admin,
        "get_map_catalog",
        lambda: type(
            "Catalog",
            (),
            {
                "slug": "mirage",
                "name_zh": "荒漠迷城",
                "asset_url": "/assets/maps/mirage.svg",
                "asset_status": "approved",
            },
        )(),
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/admin/maps/mirage")
    finally:
        app.dependency_overrides.pop(admin.require_admin, None)

    assert response.status_code == 200
    assert response.json() == {
        "slug": "mirage",
        "name_zh": "荒漠迷城",
        "asset_url": "/assets/maps/mirage.svg",
        "asset_status": "approved",
    }
    assert response.headers["cache-control"] == "no-store"


def test_removed_throw_method_directory_and_write_field_stay_closed() -> None:
    app.dependency_overrides[admin.require_admin] = lambda: "test-admin"
    payload = {
        "target_id": str(uuid.uuid4()),
        "origin_id": str(uuid.uuid4()),
        "throw_method_id": str(uuid.uuid4()),
        "steps": {"setup": "站位", "aim": "瞄点", "throw": "普通文字", "result": "结果"},
    }
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            directory = client.get("/api/v1/admin/throw-methods")
            write = client.post("/api/v1/admin/lineups", json=payload)
    finally:
        app.dependency_overrides.pop(admin.require_admin, None)

    assert directory.status_code == 404
    assert directory.json()["error"]["code"] == "ENDPOINT_NOT_FOUND"
    assert write.status_code == 422
    assert write.json()["error"]["details"]["fields"] == [
        {"path": "throw_method_id", "reason": "extra_forbidden"}
    ]


def test_generic_point_writes_reject_coordinates_and_position_has_dedicated_contract() -> None:
    app.dependency_overrides[admin.require_admin] = lambda: "test-admin"
    base = {"slug": "contract-point", "name_zh": "契约点位"}
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            for route in ("/api/v1/admin/targets", "/api/v1/admin/origins"):
                response = client.post(route, json={**base, "map_x": 1, "map_y": 2})
                assert response.status_code == 422
                assert response.json()["error"]["code"] == "VALIDATION_ERROR"
                assert {
                    field["path"] for field in response.json()["error"]["details"]["fields"]
                } == {
                    "map_x",
                    "map_y",
                }
                assert response.headers["cache-control"] == "no-store"
                assert len(response.headers["x-request-id"]) == 32

            for route in ("/api/v1/admin/targets/test-id", "/api/v1/admin/origins/test-id"):
                response = client.patch(route, json={"map_x": 1, "map_y": 2})
                assert response.status_code == 422
                assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        app.dependency_overrides.pop(admin.require_admin, None)

    schemas = app.openapi()["components"]["schemas"]
    assert "map_x" not in schemas["PointCreate"]["properties"]
    assert "map_y" not in schemas["PointCreate"]["properties"]
    assert "map_x" not in schemas["PointPatch"]["properties"]
    assert "map_y" not in schemas["PointPatch"]["properties"]
    for schema_name in ("PointOut", "PointSummary"):
        properties = schemas[schema_name]["properties"]
        for coordinate in ("map_x", "map_y"):
            assert coordinate in schemas[schema_name]["required"]
            variants = properties[coordinate]["anyOf"]
            assert {variant.get("type") for variant in variants} == {"number", "null"}
    for coordinate in ("map_x", "map_y"):
        assert coordinate in schemas["MapTarget"]["required"]
        variants = schemas["MapTarget"]["properties"][coordinate]["anyOf"]
        assert {variant.get("type") for variant in variants} == {"number", "null"}
    position_schema = schemas["TargetPositionPatch"]
    assert position_schema["required"] == ["map_x", "map_y"]
    assert set(position_schema["properties"]) == {"map_x", "map_y"}
    assert "/api/v1/admin/targets/{target_id}/position" in app.openapi()["paths"]
    assert "/api/v1/admin/origins/{origin_id}/position" not in app.openapi()["paths"]


def test_position_route_rejects_single_keys_and_mixed_null_pairs_before_database_access() -> None:
    app.dependency_overrides[admin.require_admin] = lambda: "test-admin"
    point_id = str(uuid.uuid4())
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            for coordinates in (
                {"map_x": 1},
                {"map_y": 1},
                {"map_x": None, "map_y": 1},
                {"map_x": 1, "map_y": None},
                {"map_x": 1, "map_y": 2, "unknown": 3},
            ):
                response = client.patch(
                    f"/api/v1/admin/targets/{point_id}/position", json=coordinates
                )
                assert response.status_code == 422
                assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        app.dependency_overrides.pop(admin.require_admin, None)
