"""DB 표 (설계_v2.md 5단계 "데이터 구조" 초안을 그대로 옮김)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from psd.db import Base


class Observatory(Base):
    """관측소: ASOS 관측소 2곳(부산 159, 울산 152). AWS 승인되면 역별 관측소로 교체."""

    __tablename__ = "observatories"

    id: Mapped[str] = mapped_column(String(10), primary_key=True)  # 지점 번호, 예: "159"
    kind: Mapped[str] = mapped_column(String(10))  # "ASOS" | "AWS"
    name: Mapped[str] = mapped_column(String(50))  # 이름은 추정(지점정보 API 미승인)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    stations: Mapped[list["Station"]] = relationship(back_populates="observatory")
    daily_weather: Mapped[list["DailyWeather"]] = relationship(back_populates="observatory")


class Station(Base):
    """역: 부전 ~ 북울산 24개 역."""

    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(primary_key=True)  # 역 순서(1~24)를 ID로 씀
    name: Mapped[str] = mapped_column(String(30), unique=True)
    open_date: Mapped[dt.date] = mapped_column(Date)  # 스크린도어 설치일로 가정(멘토 확인 대기)
    observatory_id: Mapped[str] = mapped_column(ForeignKey("observatories.id"))
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    observatory: Mapped[Observatory] = relationship(back_populates="stations")
    doors: Mapped[list["Door"]] = relationship(back_populates="station")
    parts: Mapped[list["Part"]] = relationship(back_populates="station")


class Door(Base):
    """문: 방향(상행/하행)별 16개(임시값, 역별 실제 문 수 확인 대기)."""

    __tablename__ = "doors"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    side: Mapped[str] = mapped_column(String(1))  # "U"(부전 방면) | "D"(북울산 방면)
    car: Mapped[int] = mapped_column()  # 호차 (1~4)
    position: Mapped[int] = mapped_column()  # 문 번호 (1~4)
    label: Mapped[str] = mapped_column(String(30))  # 예: "부전 방면 1-1"

    station: Mapped[Station] = relationship(back_populates="doors")
    parts: Mapped[list["Part"]] = relationship(back_populates="door")


class PartType(Base):
    """부품 종류: 코레일 표 7종."""

    __tablename__ = "part_types"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(30))
    unit: Mapped[str] = mapped_column(String(10))  # "door" | "station"
    weather_sensitive: Mapped[bool] = mapped_column()
    base_years: Mapped[float] = mapped_column(Float, default=10.0)

    parts: Mapped[list["Part"]] = relationship(back_populates="part_type")


class Part(Base):
    """부품: 역의 문마다(또는 역 단위로) 실제로 설치된 부품 1개."""

    __tablename__ = "parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_type_code: Mapped[str] = mapped_column(ForeignKey("part_types.code"))
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    door_id: Mapped[int | None] = mapped_column(ForeignKey("doors.id"), nullable=True)  # 역 단위 부품은 비움
    install_date: Mapped[dt.date] = mapped_column(Date)  # 마지막 교체일, 기록 없으면 역 개통일

    part_type: Mapped[PartType] = relationship(back_populates="parts")
    station: Mapped[Station] = relationship(back_populates="parts")
    door: Mapped[Door | None] = relationship(back_populates="parts")


class ReplacementRecord(Base):
    """교체 기록: 화면에서 입력. 한 기록에 부품(종류) 여러 개가 함께 묶일 수 있다."""

    __tablename__ = "replacement_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    replaced_on: Mapped[dt.date] = mapped_column(Date)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    door_id: Mapped[int | None] = mapped_column(ForeignKey("doors.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(10))  # "fail" | "prev" | "with"
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[str | None] = mapped_column(String(50), nullable=True)

    station: Mapped[Station] = relationship()
    door: Mapped[Door | None] = relationship()
    parts: Mapped[list["ReplacementPart"]] = relationship(back_populates="record")


class ReplacementPart(Base):
    """교체 부품: 교체 기록 1건에 들어간 부품 종류 (1:N)."""

    __tablename__ = "replacement_parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("replacement_records.id"))
    part_type_code: Mapped[str] = mapped_column(ForeignKey("part_types.code"))

    record: Mapped[ReplacementRecord] = relationship(back_populates="parts")
    part_type: Mapped[PartType] = relationship()


class DailyWeather(Base):
    """일별 날씨: 기상청 ASOS 일자료 전처리 결과(`data/processed/weather_daily.csv`)."""

    __tablename__ = "daily_weather"

    observatory_id: Mapped[str] = mapped_column(ForeignKey("observatories.id"), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    ta_avg: Mapped[float] = mapped_column(Float)
    ta_max: Mapped[float] = mapped_column(Float)
    ta_min: Mapped[float | None] = mapped_column(Float, nullable=True)  # 남은 결측 1일(울산)
    hm_avg: Mapped[float] = mapped_column(Float)
    dtr: Mapped[float | None] = mapped_column(Float, nullable=True)  # 일교차 = ta_max - ta_min
    frost: Mapped[bool | None] = mapped_column(nullable=True)  # 결빙일(최저기온 < 0°C)
    imputed: Mapped[bool] = mapped_column()  # 보간한 날인지

    observatory: Mapped[Observatory] = relationship(back_populates="daily_weather")


class CycleResult(Base):
    """계산 결과: 역 x 부품 종류의 권장 주기. 날씨가 갱신되면 다시 계산해 덮어쓴다."""

    __tablename__ = "cycle_results"

    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), primary_key=True)
    part_type_code: Mapped[str] = mapped_column(ForeignKey("part_types.code"), primary_key=True)
    base_years: Mapped[float] = mapped_column(Float)
    lo_years: Mapped[float] = mapped_column(Float)
    hi_years: Mapped[float] = mapped_column(Float)
    judgment: Mapped[str] = mapped_column(String(10))
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    weather_start: Mapped[dt.date] = mapped_column(Date)
    weather_end: Mapped[dt.date] = mapped_column(Date)

    station: Mapped[Station] = relationship()
    part_type: Mapped[PartType] = relationship()
