from __future__ import annotations

from dataclasses import dataclass

MODEL_VERSION = "projection-2026.1"
FEATURES = (
    "historical_fppg",
    "opportunity_share",
    "depth_role",
    "injury_availability",
    "opponent_factor",
    "schedule_factor",
    "weather_factor",
)


@dataclass(frozen=True)
class TrainingRow:
    position: str
    features: dict[str, float]
    target_points: float


@dataclass(frozen=True)
class Projection:
    weekly_points: float
    ros_points: float
    confidence: float
    model_version: str = MODEL_VERSION


class PositionProjectionModel:
    """Small deterministic linear model with position-specific fitted coefficients."""

    def __init__(self, position: str, coefficients: dict[str, float] | None = None):
        self.position = position
        self.coefficients = coefficients or {feature: 0.0 for feature in FEATURES}
        self.intercept = 0.0

    def fit(
        self, rows: list[TrainingRow], *, learning_rate: float = 0.002, iterations: int = 800
    ) -> PositionProjectionModel:
        selected = [row for row in rows if row.position == self.position]
        if len(selected) < 3:
            raise ValueError(f"At least three {self.position} rows are required")
        coefficients = dict(self.coefficients)
        intercept = self.intercept
        for _ in range(iterations):
            intercept_gradient = 0.0
            gradients = {feature: 0.0 for feature in FEATURES}
            for row in selected:
                prediction = intercept + sum(
                    coefficients[feature] * row.features.get(feature, 0.0) for feature in FEATURES
                )
                error = prediction - row.target_points
                intercept_gradient += error
                for feature in FEATURES:
                    gradients[feature] += error * row.features.get(feature, 0.0)
            scale = 2 / len(selected)
            intercept -= learning_rate * scale * intercept_gradient
            for feature in FEATURES:
                coefficients[feature] -= learning_rate * scale * gradients[feature]
        self.intercept = intercept
        self.coefficients = coefficients
        return self

    def predict(
        self,
        features: dict[str, float],
        *,
        games_remaining: int,
        source_fresh: bool,
        disputed_inputs: bool = False,
    ) -> Projection:
        weekly = max(
            self.intercept
            + sum(self.coefficients[feature] * features.get(feature, 0.0) for feature in FEATURES),
            0.0,
        )
        completeness = sum(feature in features for feature in FEATURES) / len(FEATURES)
        confidence = 0.85 * completeness
        if not source_fresh:
            confidence *= 0.45
        if disputed_inputs:
            confidence *= 0.5
        return Projection(
            weekly_points=round(weekly, 3),
            ros_points=round(weekly * max(games_remaining, 0), 3),
            confidence=round(confidence, 3),
        )


def blend_open_projection(
    internal: Projection, open_weekly_points: float | None, *, open_source_fresh: bool
) -> Projection:
    if open_weekly_points is None:
        return Projection(
            internal.weekly_points,
            internal.ros_points,
            round(internal.confidence * 0.7, 3),
        )
    open_weight = 0.45 if open_source_fresh else 0.15
    weekly = internal.weekly_points * (1 - open_weight) + open_weekly_points * open_weight
    games = internal.ros_points / internal.weekly_points if internal.weekly_points else 0
    confidence = internal.confidence * (1.0 if open_source_fresh else 0.7)
    return Projection(round(weekly, 3), round(weekly * games, 3), round(confidence, 3))
