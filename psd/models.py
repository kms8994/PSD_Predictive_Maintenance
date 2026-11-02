"""DB 표 (설계_v2.md 5단계 "데이터 구조" 초안을 그대로 옮김)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from psd.db import Base


class Observatory(Base):
    """관측소: ASOS 관측소 2곳(부산 159, 울산 152). 역이 이 표를 참조하는 연결로만 남아 있다.

    2026-10-25부터 날씨 계산은 역마다 가장 가까운 AWS 지점(psd/cycle.py STATIONS의 aws_stn,
    AWS_STATION_NAMES)을 쓰고, AWS 날씨는 DB가 아니라 data/processed/aws_weather_daily.csv에서 읽는다.
    """

    __tablename__ = "observatories"

    id: Mapped[str] = mapped_column(String(10), primary_key=True)  # 지점 번호, 예: "159"
    kind: Mapped[str] = mapped_column(String(10))  # "ASOS" | "AWS"
    name: Mapped[str] = mapped_column(String(50))  # 이름은 추정(지점정보 API 미승인)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    stations: Mapped[list["Station"]] = relationship(back_populates="observatory")


class Station(Base):
    """역: 부전 ~ 북울산 24개 역."""

    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(primary_key=True)  # 역 순서(1~24)를 ID로 씀
    name: Mapped[str] = mapped_column(String(30), unique=True)
    open_date: Mapped[dt.date] = mapped_column(Date)  # 스크린도어 설치일로 가정(멘토 확인 대기)
    observatory_id: Mapped[str] = mapped_column(ForeignKey("observatories.id"))
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    near_water: Mapped[bool] = mapped_column(default=False)  # 바다·하천 인접 추정(psd/cycle.py STATIONS 주석, 확인 필요)
    in_service: Mapped[bool] = mapped_column(default=True)  # PSD 실제 운영 중인지(psd/cycle.py Station.in_service 참고)

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
    has_record: Mapped[bool] = mapped_column(default=False)  # 교체·점검 기록이 한 번이라도 있는지(확인 필요 판정용)

    part_type: Mapped[PartType] = relationship(back_populates="parts")
    station: Mapped[Station] = relationship(back_populates="parts")
    door: Mapped[Door | None] = relationship(back_populates="parts")


class UploadBatch(Base):
    """업로드 이력: 정비기록 엑셀을 한 번 올릴 때마다 1줄(psd/sync.py — 엑셀이 원본, DB는 그에 맞춘다).

    되돌리기는 가장 최근 것부터 차례로만 된다. 이 업로드로 넣은 기록은 ReplacementRecord.batch_id로,
    지운 기록은 removed(JSON)에 그대로 적어 두었다가 되돌릴 때 다시 넣는다.
    """

    __tablename__ = "upload_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(String(255))
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    scope_from: Mapped[dt.date] = mapped_column(Date)  # 비교 범위: 파일의 가장 이른 날짜
    scope_to: Mapped[dt.date] = mapped_column(Date)  # ~ 가장 늦은 날짜
    station_count: Mapped[int] = mapped_column()  # 파일에 나온 역 수
    inserted: Mapped[int] = mapped_column()  # 새로 넣은 기록
    updated: Mapped[int] = mapped_column()  # 고친 기록(사유·부품이 달라 바꿔 넣은 것)
    deleted: Mapped[int] = mapped_column()  # 지운 기록(엑셀에서 빠진 것 + DB 중복)
    removed: Mapped[list] = mapped_column(JSON)  # 지우거나 고치기 전 기록들(되돌리기용)
    undone_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 올린 사람(화면에서 적는 이름, 로그인 전이라 확인은 안 함)


class ReplacementRecord(Base):
    """교체 기록: 정비기록 엑셀을 올려 넣는다. 한 기록에 부품(종류) 여러 개가 함께 묶일 수 있다."""

    __tablename__ = "replacement_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    replaced_on: Mapped[dt.date] = mapped_column(Date)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    door_id: Mapped[int | None] = mapped_column(ForeignKey("doors.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(10))  # "fail" | "prev" | "with" | "check"(점검 이상 없음 — 교체 아님)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("upload_batches.id"), nullable=True)  # 이 기록을 넣은 업로드

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
