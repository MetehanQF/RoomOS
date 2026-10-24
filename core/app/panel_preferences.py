from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PanelCardId = Literal["main_lights", "ambient", "scenes", "routines", "system"]
PanelSceneId = Literal["normal", "work_1", "work_2", "gaming_1", "gaming_2", "movie", "night", "all_off"]

CARD_IDS = ("main_lights", "ambient", "scenes", "routines", "system")
SCENE_IDS = ("normal", "work_1", "work_2", "gaming_1", "gaming_2", "movie", "night", "all_off")


class PanelHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clock: bool = True
    date: bool = True
    connection: bool = True
    health: bool = True


class PanelLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    cards: list[PanelCardId] = Field(default_factory=lambda: list(CARD_IDS), min_length=5, max_length=5)
    hidden: list[PanelCardId] = Field(default_factory=list, max_length=4)
    density: Literal["normal", "compact"] = "normal"
    scene_order: list[PanelSceneId] = Field(default_factory=lambda: list(SCENE_IDS), min_length=8, max_length=8)
    header: PanelHeader = Field(default_factory=PanelHeader)

    @model_validator(mode="after")
    def validate_catalogs(self):
        if len(set(self.cards)) != len(CARD_IDS) or set(self.cards) != set(CARD_IDS):
            raise ValueError("cards must contain the fixed panel catalog exactly once")
        if len(set(self.hidden)) != len(self.hidden):
            raise ValueError("hidden cards must be unique")
        if {"main_lights", "ambient"}.issubset(self.hidden):
            raise ValueError("at least one light card must remain visible")
        if len(set(self.scene_order)) != len(SCENE_IDS) or set(self.scene_order) != set(SCENE_IDS):
            raise ValueError("scene_order must contain the fixed scene catalog exactly once")
        return self


DEFAULT_PANEL_LAYOUT = PanelLayout()


class PanelLayoutResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layout: PanelLayout
    source: Literal["default", "saved"]
    updated_at: datetime | None = None

    @classmethod
    def default(cls):
        return cls(layout=DEFAULT_PANEL_LAYOUT.model_copy(deep=True), source="default")

    @classmethod
    def saved(cls, layout: PanelLayout, updated_at: int):
        return cls(
            layout=layout,
            source="saved",
            updated_at=datetime.fromtimestamp(updated_at, UTC),
        )
