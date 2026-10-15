"""API 입력값 검사 (MVP_계획.md 4-7)."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, field_validator

from psd.catalog import PART_TYPES

VALID_PART_CODES = {p["code"] for p in PART_TYPES}
VALID_REASONS = {"fail": "고장", "prev": "예방 교체", "with": "함께 교체"}


class ReplacementRecordIn(BaseModel):
    station_id: int
    door_id: int | None = None  # 역 단위 부품(종합제어반 릴레이)만 교체하면 비움
    replaced_on: date
    reason: str
    part_type_codes: list[str]
    created_by: str | None = None

    @field_validator("reason")
    @classmethod
    def reason_known(cls, v: str) -> str:
        if v not in VALID_REASONS:
            raise ValueError(f"사유는 {sorted(VALID_REASONS)} 중 하나여야 함")
        return v

    @field_validator("part_type_codes")
    @classmethod
    def parts_known_and_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("부품 종류를 하나 이상 골라야 함")
        unknown = set(v) - VALID_PART_CODES
        if unknown:
            raise ValueError(f"알 수 없는 부품 종류: {sorted(unknown)}")
        return v

    @field_validator("replaced_on")
    @classmethod
    def not_in_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("교체일은 미래일 수 없음")
        return v
