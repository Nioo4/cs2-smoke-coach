from sqlalchemy.dialects.postgresql import JSONB

from app.db.models import (
    Base,
    ConversationState,
    ExternalSearchCache,
    Lineup,
    Origin,
    Target,
    TutorialVideo,
    UsageEvent,
)


def test_runtime_model_contains_no_throw_method_business_tables() -> None:
    assert set(Base.metadata.tables) == {
        "targets",
        "target_aliases",
        "origins",
        "origin_aliases",
        "lineups",
        "tutorial_videos",
        "conversation_states",
        "external_search_cache",
        "usage_events",
    }


def test_phase1_database_constraints_are_declared() -> None:
    target_checks = {constraint.name for constraint in Target.__table__.constraints}
    origin_checks = {constraint.name for constraint in Origin.__table__.constraints}
    lineup_checks = {constraint.name for constraint in Lineup.__table__.constraints}
    video_checks = {constraint.name for constraint in TutorialVideo.__table__.constraints}
    usage_checks = {constraint.name for constraint in UsageEvent.__table__.constraints}
    assert "uq_lineup_identity" not in lineup_checks
    assert "ck_lineups_default_status" in lineup_checks
    assert "ck_targets_coordinate_pair" in target_checks
    assert "ck_origins_coordinate_pair" in origin_checks
    assert Target.__table__.c.map_x.nullable is True
    assert Target.__table__.c.map_y.nullable is True
    assert Origin.__table__.c.map_x.nullable is True
    assert Origin.__table__.c.map_y.nullable is True
    assert "throw_method_id" not in Lineup.__table__.c
    assert "throw_method_slug" not in ConversationState.__table__.c
    assert "ck_video_state" in video_checks
    video_size = next(
        constraint
        for constraint in TutorialVideo.__table__.constraints
        if constraint.name == "ck_video_size"
    )
    assert "12582912" in str(video_size.sqltext)
    assert "ck_usage_provider_fields" in usage_checks
    assert "ck_usage_response_type" in usage_checks
    assert isinstance(ConversationState.__table__.c.pending_options.type, JSONB)
    assert ConversationState.__table__.c.pending_options.type.none_as_null is True
    assert "ck_search_cache_payload" in {
        constraint.name for constraint in ExternalSearchCache.__table__.constraints
    }
