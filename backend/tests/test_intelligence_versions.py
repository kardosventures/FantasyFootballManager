from app.championship import SIMULATION_VERSION
from app.models import PlayerWeekFeature, ProjectionSnapshot, SimulationRun
from app.projection_ensemble import MODEL_VERSION
from app.usage import FEATURE_VERSION


def test_intelligence_versions_fit_database_columns() -> None:
    assert len(FEATURE_VERSION) <= PlayerWeekFeature.__table__.c.feature_version.type.length
    assert len(MODEL_VERSION) <= ProjectionSnapshot.__table__.c.model_version.type.length
    assert len(SIMULATION_VERSION) <= SimulationRun.__table__.c.model_version.type.length
