# 동해선 PSD 교체주기 예측

동해선 스크린도어(PSD) 부품의 권장 교체주기를 날씨(기상청 AWS 2022~2025년치, 역마다 가장
가까운 지점 7곳)로 보정해 계산하고, 역 → 문 → 부품 단위로 보여 주는 도구다. 실제 고장
기록은 엑셀로 업로드해 반영하고(없으면 더미데이터로 대신), 현재 상태는 엑셀 보고서로
내려받을 수 있다.

- 설계 결정: [설계_v2.md](설계_v2.md)
- 계산 공식 근거: [4단계_계산공식_근거조사.md](4단계_계산공식_근거조사.md)
- 진행 상황: [MVP_계획.md](MVP_계획.md)
- 화면 방향성(가정 날씨로 만든 참고용 목업, 실제 계산과는 다름): [목업/동해선_PSD_교체주기_목업.html](목업/동해선_PSD_교체주기_목업.html)

## 구성

```
기상자료개방포털 웹(수동) ──→ data/raw/aws/hourly ──→ [시간→일 집계] ──┐
기상청 API(자동, ASOS만)  ──→ data/raw/asos_daily_raw.csv ──→ [전처리]─┤(ASOS는 DB 적재용일 뿐,
                                                                        실제 계산엔 안 씀)
                                                                        ↓
                                                          [주기 계산] psd/cycle.py
                                                                        ↓
엑셀 업로드(고장 기록) ──┐                                      MySQL
더미데이터 생성 ─────────┼──→ POST /api/replacement-records/upload ──┘
                          │
                   [API] psd/api.py (FastAPI) ──→ [화면] psd/web/ ──→ 엑셀 보고서 내려받기
```

| 구성 | 파일 |
| --- | --- |
| AWS 날씨(수동 다운로드 → 집계) | `data/raw/aws/hourly/`(gitignore) → `scripts/preprocess_aws_weather.py` → `data/processed/aws_weather_daily.csv` |
| ASOS 날씨(자동, DB 적재용) | `scripts/collect_weather.py`(최초 수집), `scripts/daily_weather_update.py`(매일), `scripts/preprocess_weather.py` |
| 주기 계산 | `psd/cycle.py` (AWS 기준, 역별 `aws_stn`으로 지점 묶음) |
| DB 모델 | `psd/models.py`, `psd/db.py`, `psd/catalog.py` |
| DB 초기 데이터 | `scripts/init_db.py`, `scripts/load_weather.py`, `scripts/compute_cycles.py` |
| 경고 상태 | `psd/status.py` |
| 고장 기록 엑셀 입출력 | `psd/excel_io.py`, 더미데이터 생성 `scripts/generate_dummy_records.py` |
| API | `psd/api.py` |
| 화면 | `psd/web/index.html` (API가 `/`에서 그대로 제공) |

## 설치

1. Python 3.10 이상, MySQL 8.0이 설치돼 있어야 한다.
2. 패키지 설치:

   ```bash
   pip install -r requirements.txt
   ```

3. `.env` 파일을 만든다(저장소에 커밋되지 않음, `.gitignore` 등록):

   ```
   KMA_AUTH_KEY=<기상청 API허브 인증키>

   DB_HOST=localhost
   DB_PORT=3306
   DB_NAME=psd
   DB_USER=psd_app
   DB_PASSWORD=<비밀번호>
   ```

   `DB_USER`/`DB_PASSWORD`는 MySQL에 미리 만들어 둔 전용 계정이다(루트 계정을 그대로 쓰지 않는다):

   ```sql
   CREATE DATABASE IF NOT EXISTS psd CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   CREATE USER IF NOT EXISTS 'psd_app'@'localhost' IDENTIFIED BY '<비밀번호>';
   GRANT ALL PRIVILEGES ON psd.* TO 'psd_app'@'localhost';
   FLUSH PRIVILEGES;
   ```

## 날씨 데이터 (이미 받아 둔 결과가 `data/processed/`에 있어 다시 하지 않아도 됨)

**AWS(실제 계산에 쓰는 것, 수동):** API허브 AWS 엔드포인트가 아직 미승인(403)이라, 기상자료
개방포털(data.kma.go.kr) 웹 화면에 무료 회원가입으로 로그인해 "방재기상관측(AWS) → 파일셋"
에서 시간 자료를 받아야 한다(자료형태=시간 자료, 지역=부산·울산, 기간은 필요한 만큼).
`data/raw/aws/hourly/`에 CSV를 모은 뒤:

```bash
python scripts/preprocess_aws_weather.py   # 시간→일 평균 집계 → data/processed/aws_weather_daily.csv
```

**ASOS(DB 적재용, 자동 가능):**

```bash
python scripts/collect_weather.py --start 2016-01-01 --end 2025-12-31 --stations 152,159  # 최초 1회
python scripts/daily_weather_update.py --recompute   # 이후 매일(작업 스케줄러 등록 권장)
```

`collect_weather.py`는 중간에 끊겨도 다시 실행하면 이어서 받는다. `daily_weather_update.py`는
어제치 하루만 받아 이어 붙인다(상세 설명은 스크립트 docstring 참고 — "매일 자동화"가 실제로
무엇을 바꾸고 무엇을 안 바꾸는지 적어 뒀다). 원본은 `data/raw/`(저장소에 올리지 않음), 전처리
결과는 `data/processed/*.csv`(저장소에 올림)에 쌓인다.

## DB 만들기

```bash
python scripts/init_db.py          # 표 생성 + 역·관측소·문·부품 종류·부품 초기 데이터 (기존 표에 없는 컬럼은 자동으로 ALTER)
python scripts/load_weather.py     # ASOS 전처리 결과 적재(DB 보관용, 실제 계산엔 안 씀)
python scripts/compute_cycles.py   # psd/cycle.py(AWS 기준)로 역 x 부품 7종 권장 주기 계산 후 저장
```

역/부품 구성 자체를 바꿨다면(표 구조가 아니라 데이터 초기화가 필요하면) `python scripts/init_db.py --reset`으로
표를 지우고 다시 만든다. 세 스크립트는 이 순서로 다시 실행해도 안전하다(있으면 지우고 새로 넣음).

## 고장 기록 입력 (엑셀 업로드 / 더미데이터)

실제 고장 데이터가 없어서, 신뢰성 공식이 낸 권장 주기를 평균으로 한 와이블 분포로 "이미
고장났을 법한" 부품을 뽑아 더미데이터를 만든다(자세한 설명은 스크립트 docstring 참고):

```bash
python scripts/generate_dummy_records.py --seed 1 --out data/dummy/replacement_records_dummy.xlsx
```

받은(또는 만든) 엑셀은 화면의 "고장(교체) 기록 엑셀 일괄 업로드"로 올리거나, API로 바로 올려도 된다:

```bash
curl -F "file=@data/dummy/replacement_records_dummy.xlsx" http://127.0.0.1:8000/api/replacement-records/upload
```

열 형식은 `psd/excel_io.py` 상단 설명 참고(역, 방향, 문번호, 교체일, 사유, 부품, 입력자).
한 행이라도 틀리면 전체를 저장하지 않는다(all-or-nothing).

## 보고서 출력

`GET /api/report`(전체 역) 또는 `GET /api/report?station_id=1`(역 하나)로 지금 상태를 엑셀로
받는다. 화면에도 "보고서 내려받기" 링크로 연결돼 있다.

## 실행

```bash
uvicorn psd.api:app --reload
```

브라우저에서 `http://127.0.0.1:8000/` 을 열면 화면이 나온다. API 문서는 `http://127.0.0.1:8000/docs`.

## 테스트

```bash
python -m pytest tests/ -v
```

`tests/test_cycle.py`는 설계 문서 손계산 예시와 계산의 기본 성질(AWS 지점별 묶음 포함)을,
`tests/test_status.py`는 경고 상태(빨강·노랑·초록·확인 필요) 판정 규칙을, `tests/test_excel_io.py`는
엑셀 업로드 파싱을 검증한다. `tests/test_api.py`만 예외로 **실제 MySQL DB가 필요하다**
(`init_db.py`·`compute_cycles.py`를 먼저 실행해 둔 DB).

## 코레일에서 받은 자료에 대하여

`PSD_부품_리스트.png`, `증빙/`, `서식5_프롬프트기록부_학생배포본.xlsx` 등은 수업·코레일 제공 비공개 자료라
이 저장소(공개)에 올리지 않는다(`.gitignore` 참고).
