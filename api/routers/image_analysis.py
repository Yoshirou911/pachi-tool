"""Reviewed, local-first image analysis API (v3.45)."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from api.image_analysis import image_analyses

router = APIRouter()


class ImageMetadata(BaseModel):
    model_config = {"extra": "forbid"}
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime: Literal["image/jpeg", "image/png", "image/webp"]
    width: int = Field(ge=1, le=12000, strict=True)
    height: int = Field(ge=1, le=12000, strict=True)
    size: int = Field(ge=1, le=20_000_000, strict=True)


class ExtractionCandidate(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(min_length=1, max_length=1000)
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    kind: Literal["number", "text"]


class ReviewedValues(BaseModel):
    model_config = {"extra": "forbid"}
    game_count: int | None = Field(default=None, ge=0, le=999999, strict=True)
    seat_number: int | None = Field(default=None, ge=1, le=99999, strict=True)
    bb_count: int | None = Field(default=None, ge=0, le=9999, strict=True)
    rb_count: int | None = Field(default=None, ge=0, le=9999, strict=True)
    text: str = Field(default="", max_length=10000)
    seat_numbers: list[Annotated[int, Field(strict=True, ge=1, le=99999)]] = Field(default_factory=list, max_length=1000)

    @field_validator("seat_numbers")
    @classmethod
    def unique_seats(cls, value: list[int]) -> list[int]:
        if any(isinstance(item, bool) or item < 1 or item > 99999 for item in value):
            raise ValueError("台番号が不正です")
        if len(value) != len(set(value)):
            raise ValueError("台番号を重複して保存できません")
        return value


class ImageAnalysisInput(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["data_lamp", "store_material", "floor_map"]
    hall_name: str = Field(default="", max_length=120)
    observed_on: date | None = None
    image: ImageMetadata
    extraction_method: Literal["text-detector", "seven-segment", "manual-review"]
    extracted: list[ExtractionCandidate] = Field(default_factory=list, max_length=500)
    reviewed: ReviewedValues
    review_confirmed: Literal[True]
    save_original_on_device: bool = False

    @field_validator("review_confirmed", mode="before")
    @classmethod
    def explicit_review(cls, value):
        if value is not True:
            raise ValueError("元画像との照合が必要です")
        return value

    @model_validator(mode="after")
    def values_match_kind(self):
        lamp_values = (self.reviewed.game_count, self.reviewed.seat_number,
                       self.reviewed.bb_count, self.reviewed.rb_count)
        if self.kind != "data_lamp" and any(v is not None for v in lamp_values):
            raise ValueError("画像の種類と確認値が一致しません")
        if self.kind != "floor_map" and self.reviewed.seat_numbers:
            raise ValueError("台番号一覧はホールマップだけで保存できます")
        if self.kind != "store_material" and self.reviewed.text:
            raise ValueError("資料の文面は店舗資料だけで保存できます")
        if self.kind == "data_lamp" and all(
            value is None for value in (
                self.reviewed.game_count, self.reviewed.seat_number,
                self.reviewed.bb_count, self.reviewed.rb_count,
            )
        ):
            raise ValueError("データランプの確認値を1つ以上入力してください")
        if self.kind == "store_material" and not self.reviewed.text.strip():
            raise ValueError("店舗資料の確認済み文字を入力してください")
        if self.kind == "floor_map" and not self.reviewed.seat_numbers:
            raise ValueError("ホールマップの台番号を1台以上確認してください")
        return self


@router.get("/api/image-analysis/status", tags=["image-analysis"])
def image_analysis_status():
    return image_analyses.status()


@router.get("/api/image-analysis/records", tags=["image-analysis"])
def image_analysis_records(limit: int = Query(20, ge=1, le=100)):
    return image_analyses.recent(limit)


@router.post("/api/image-analysis/records", tags=["image-analysis"])
def save_image_analysis(body: ImageAnalysisInput):
    # model_dump never contains image bytes: the schema rejects all unknown fields.
    return image_analyses.save(body.model_dump(mode="json"))
