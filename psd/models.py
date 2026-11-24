"""DB 표 (설계_v2.md 5단계 "데이터 구조" 초안을 그대로 옮김)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from psd.db import Base


class Station(Base):
    """역: 부전 ~ 북울산 24개 역.

    날씨는 DB에 두지 않는다 — 역마다 가장 가까운 AWS 지점(psd/cycle.py STATIONS의 aws_stn)의
    data/processed/aws_weather_daily.csv를 읽는다. 예전 관측소 표(observatories)와 일별 날씨 표(daily_weather)는
    쓰는 곳이 없어 2026-11-23에 지웠다(scripts/init_db.py _DROP_TABLES).
    """

    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(primary_key=True)  # 역 순서(1~24)를 ID로 씀
    name: Mapped[str] = mapped_column(String(30), unique=True)
    open_date: Mapped[dt.date] = mapped_column(Date)  # 스크린도어 설치일로 가정(멘토 확인 대기)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    near_water: Mapped[bool] = mapped_column(default=False)  # 바다·하천 인접 추정(psd/cycle.py STATIONS 주석, 확인 필요)
    in_service: Mapped[bool] = mapped_column(default=True)  # PSD 실제 운영 중인지(psd/cycle.py Station.in_service 참고)
    # 역 환경(2026-11-23): 나중에 AI가 역 특성별로 가중치를 다르게 주도록 넣을 자리. 정확한 정보가 없어 지금은 전부
    # 비워 둔다(NULL = 모름). 아직 계산·화면 어디에서도 읽지 않는다. 날씨(습도·기온·일교차)는 AWS 지점으로 이미
    # 공식에 들어가 있으니, 여기엔 날씨로 설명 안 되는 것만 넣는다(near_water 가중을 걷어낸 이유, psd/cycle.py 맨 위 주석)
    structure: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 역 구조: "지상" | "지하" | "고가"
    coast_km: Mapped[float | None] = mapped_column(Float, nullable=True)  # 해안선까지 거리(km)
    daily_riders: Mapped[int | None] = mapped_column(nullable=True)  # 하루 평균 이용객 수(명)
    env_note: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 그 밖의 환경 메모(염분·먼지·바람 등)
    # 2026-11-24 v3: 장애 이력 양식의 역 정보 칸. 업로드한 기록에서 가장 최근 값으로 채운다(psd/api.py _update_station_info).
    # structure(위)는 양식의 "PSD설치"(지상/지하)로 채운다. 화면 표시와 AI 특징 후보용 — 아직 계산에는 안 쓴다
    hq: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 본부
    line_name: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 노선
    office: Mapped[str | None] = mapped_column(String(30), nullable=True)  # 관할사업소
    install_year: Mapped[int | None] = mapped_column(nullable=True)  # 설치년도(사용개시일 추정엔 아직 안 씀 — 멘토 확인)
    manufacturer: Mapped[str | None] = mapped_column(String(30), nullable=True)  # 제조업체
    control_system: Mapped[str | None] = mapped_column(String(30), nullable=True)  # 제어시스템
    dcu_vendor: Mapped[str | None] = mapped_column(String(30), nullable=True)  # DCU(MCU)업체
    install_owner: Mapped[str | None] = mapped_column(String(30), nullable=True)  # 설치주체

    doors: Mapped[list["Door"]] = relationship(back_populates="station")
    parts: Mapped[list["Part"]] = relationship(back_populates="station")


class Door(Base):
    """문: 선로별 16개(임시값, 역별 실제 문 수 확인 대기). 처음엔 상·하 본선만 있고, 기록에 부본선이 나오면 자동으로 생긴다."""

    __tablename__ = "doors"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    side: Mapped[str] = mapped_column(String(2))  # 선로: "U" 상선 | "D" 하선 | "UB" 상부본선 | "DB" 하부본선 (catalog.SIDES)
    car: Mapped[int] = mapped_column()  # 호차 (1~4)
    position: Mapped[int] = mapped_column()  # 문 번호 (1~4)
    label: Mapped[str] = mapped_column(String(30))  # 예: "상선 1-1"

    station: Mapped[Station] = relationship(back_populates="doors")
    parts: Mapped[list["Part"]] = relationship(back_populates="door")


class PartType(Base):
    """예측 단위: 장애 이력 양식의 장애부위 소분류(2026-11-24 v3, 분류표 파일 psd/taxonomy.py). 이름은 예전 그대로 "부품 종류"."""

    __tablename__ = "part_types"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(30))
    unit: Mapped[str] = mapped_column(String(10))  # "door" | "station"
    weather_sensitive: Mapped[bool] = mapped_column()
    group_name: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 대분류("구동부 장애" ...)
    formula: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 날씨 공식("motor"|"dcm"|"fixed"|NULL=공식 없음)
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
    """정비 기록: 장애 이력 양식의 한 줄(2026-11-24 v3). 한 줄에 소분류 하나(ReplacementPart 1개)."""

    __tablename__ = "replacement_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    replaced_on: Mapped[dt.date] = mapped_column(Date)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    door_id: Mapped[int | None] = mapped_column(ForeignKey("doors.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(10))  # 장애인지: "fail" 사후조치 | "prev" 사전조치 (catalog.REASONS)
    # 조치사항(psd/catalog.py ACTIONS): "full" 교체 | "repair" 수리 | "adjust" 조정 | "partial" 단순교체 | "reset" 리셋 | "other" 기타.
    # 설치일(새 수명 시작)은 교체 때만 바뀐다. 장애형태(장애/보수/예방보수)는 조치로 정해져 따로 저장하지 않는다(catalog.FAULT_TYPE)
    action: Mapped[str] = mapped_column(String(10), default="full")
    symptom: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 장애내용①(분류표의 소분류별 목록)
    symptom_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)  # 장애내용②(상세), 자유 글
    cause_text: Mapped[str | None] = mapped_column(String(500), nullable=True)  # 고장원인, 자유 글
    action_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)  # 조치사항 상세, 자유 글
    track_raw: Mapped[str | None] = mapped_column(String(30), nullable=True)  # 승강장 선로 원문(예: "상본선(#3)")
    home: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 승강장 홈 번호
    receiver: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 접수자
    received: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 접수일(양식이 "O월 O일 OO:OO" 같은 글이라 글로 둔다)
    completed: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 완료일(글)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 조치자
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("upload_batches.id"), nullable=True)  # 이 기록을 넣은 업로드

    station: Mapped[Station] = relationship()
    door: Mapped[Door | None] = relationship()
    parts: Mapped[list["ReplacementPart"]] = relationship(back_populates="record")


class ReplacementPart(Base):
    """기록의 소분류. v3부터 기록 1건에 1개다(표는 v2의 1:N 구조를 그대로 둔다)."""

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

    station: Mapped[Station] = relationship()
    part_type: Mapped[PartType] = relationship()


class RiskModel(Base):
    """AI 고장 위험도 모델 버전(psd/risk.py, 2026-11-16 재학습 흐름). 학습할 때마다 1줄.

    새 모델은 지금 쓰는 모델(status="active")이 학습 때 못 본 최근 기간으로 둘을 함께 맞혀 보고,
    새 모델이 지지 않을 때만 바꿔 쓴다. 진 모델은 "rejected"로 남기고, 밀려난 모델은 "retired".
    """

    __tablename__ = "risk_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    trained_at: Mapped[dt.datetime] = mapped_column(DateTime)
    trigger: Mapped[str] = mapped_column(String(20))  # "upload" | "undo" | "manual" | "script" | "pretrained"(데스크탑에서 가져옴)
    data_key: Mapped[str] = mapped_column(String(200))  # 학습한 기록 상태(같으면 다시 학습하지 않음)
    data_end: Mapped[dt.date] = mapped_column(Date)  # 학습에 쓴 기록의 마지막 날
    last_train_snapshot: Mapped[dt.date] = mapped_column(Date)  # 최종 모델이 본 마지막 스냅샷 달
    train_rows: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(10))  # "active" | "rejected" | "retired"
    reason: Mapped[str] = mapped_column(String(300))  # 바꿨거나 안 바꾼 이유
    metrics: Mapped[dict] = mapped_column(JSON)  # {"holdout": [from, to], "challenger", "champion", "baseline", ...}
    importance: Mapped[list] = mapped_column(JSON)
    file_name: Mapped[str] = mapped_column(String(100))
