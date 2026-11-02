# 동해선 PSD 교체주기 예측

동해선 스크린도어(PSD) 부품의 권장 교체주기를 날씨(기상청 AWS 2022~2025년치, 역마다 가장
가까운 지점 7곳)로 보정해 계산하고, 역 → 문 → 부품 단위로 보여 주는 도구다. 현장의 정비기록
엑셀을 원본으로 두고, 올리면 DB를 그 엑셀에 맞춰 교체주기에 반영한다(없으면 더미데이터로 대신).
현재 상태는 엑셀·PDF 보고서로 내려받을 수 있다.

- 설계 결정: [설계_v2.md](설계_v2.md)
- 계산 공식 근거: [4단계_계산공식_근거조사.md](4단계_계산공식_근거조사.md)
- 진행 상황: [MVP_계획.md](MVP_계획.md)
- UI 개발 환경(Windows): [docs/ui-development.md](docs/ui-development.md)
- 화면 방향성(가정 날씨로 만든 참고용 목업, 실제 계산과는 다름): [목업/동해선_PSD_교체주기_목업.html](목업/동해선_PSD_교체주기_목업.html)

## 다른 컴퓨터에서 처음 설치하기

날씨 집계 결과(`data/processed/aws_weather_daily.csv`)는 저장소에 들어 있어서, 날씨를 다시 받지
않아도 아래 순서만 따라 하면 화면이 뜬다. Windows·macOS·Linux 어디서든 같다.

### 1. 준비물

- Python 3.10 이상
- MySQL 8.0 (서버가 켜져 있어야 함)
- Git

### 2. 내려받기와 패키지 설치

```bash
git clone https://github.com/kms8994/PSD_Predictive_Maintenance.git
cd PSD_Predictive_Maintenance
python -m venv .venv
```

가상환경 켜기 — Windows: `.venv\Scripts\activate`, macOS/Linux: `source .venv/bin/activate`

```bash
pip install -r requirements.txt
```

### 3. MySQL에 DB와 전용 계정 만들기

MySQL에 root로 접속해서(`mysql -u root -p`) 한 번만 실행한다. `<비밀번호>`는 직접 정한다:

```sql
CREATE DATABASE IF NOT EXISTS psd CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'psd_app'@'localhost' IDENTIFIED BY '<비밀번호>';
GRANT ALL PRIVILEGES ON psd.* TO 'psd_app'@'localhost';
FLUSH PRIVILEGES;
```

### 4. `.env` 만들기

`.env.example`을 복사해 `.env`로 저장하고, 위 3단계의 계정을 사용한다면 `DB_NAME`을
`psd`, `DB_USER`를 `psd_app`으로 바꾼 뒤 `DB_PASSWORD`에 3단계의 비밀번호를 넣는다.
`.env.example`의 기본값은 별도 UI 개발용 DB를 위한 설정이다.
`.env`는 비밀번호가 들어 있어 저장소에 올라가지 않는다(`.gitignore`).

```
DB_HOST=localhost
DB_PORT=3306
DB_NAME=psd
DB_USER=psd_app
DB_PASSWORD=<비밀번호>
```

### 5. 표 만들기와 교체주기 계산

```bash
python scripts/init_db.py          # 표 생성 + 역 24개·문·부품 4,439개 등 기본 데이터
python scripts/compute_cycles.py   # 날씨로 역 x 부품 7종 권장 교체주기를 계산해 저장
```

### 6. (선택) 초기 데이터 넣기 — 더미 교체 기록 1,230건

```bash
python scripts/load_initial_records.py
```

> **⚠️ 이 단계는 선택이다. 넣을지 말지에 따라 화면이 크게 달라진다.**
>
> 실제 고장 데이터가 아직 없어서, 신뢰성 공식이 낸 주기를 평균으로 한 와이블 분포로 "이미 고장났을
> 법한" 기록 1,230건을 만들어 두었다(`data/dummy/초기데이터_교체기록_1230건.xlsx`, 개발 PC의 DB와 같은 내용).
>
> | | 넣으면 | 넣지 않으면 |
> | --- | --- | --- |
> | 화면 상태 | 개발 PC와 똑같다 — 위험 38 · 주의 104 · 안전 4,297 | 부품 4,439개가 **전부 "안전"** |
> | 교체주기 | 고장 기록으로 보정된 값(예: IFM 릴레이 평균 약 6.3년) | 고장 기록이 0건이라 날씨 공식보다 길게 나옴(IFM 릴레이 평균 약 10.5년) |
> | 최근 교체 기록·교체 물량·보고서 | 기록이 채워져 있음 | 거의 비어 있음 |
> | 정비기록 시연 탭 | 샘플이 이 기록을 기준으로 만들어져 결과가 의도대로 나옴 | 동작은 하지만 바뀌는 모습이 다르게 나옴 |
>
> - **시연·발표처럼 지금 화면 그대로 보여 주려면 넣는다.**
> - **실제 고장 데이터를 받아서 처음부터 쓰려면 넣지 않는다.** 이 기록은 가짜라서, 실제 데이터와 섞이면
>   교체주기 보정이 틀어진다. 실제 기록은 화면의 "교체 기록" 탭에서 정비기록 엑셀로 올리면 된다.
>   (실제 운영 때의 초기 데이터는 실데이터에 맞춰 따로 다시 만든다.)
> - 이미 교체 기록이 있는 DB에서는 겹치지 않게 아무것도 하지 않는다. 넣은 뒤 빼고 싶으면 화면의
>   "교체 기록 → 업로드 이력"에서 이 업로드를 되돌리거나, `python scripts/init_db.py --reset`으로 표를
>   지우고 5단계부터 다시 한다(그 DB의 모든 교체 기록이 지워진다).

### 7. 실행

```bash
uvicorn psd.api:app --reload
```

브라우저에서 `http://127.0.0.1:8000/` 을 연다. API 문서는 `http://127.0.0.1:8000/docs`.

PDF 보고서의 한글 글꼴은 Windows의 맑은 고딕을 쓰고, 없으면(macOS·Linux) PDF 뷰어의 기본 한글 글꼴로
대신 나온다.

## 화면 구성

| 탭 | 내용 |
| --- | --- |
| 현황 | 노선 전체 상태, 교체 예정 목록(전체 보기에서 기간·역·부품으로 거르기), 분기별 교체 물량(부품별 표), 최근 교체 기록, 보고서 내려받기 |
| 역 | 역 → 문 → 부품 상태, 그 역 보고서(엑셀·PDF). 부품 화면의 계산 근거는 "계산 근거 자세히"에 접혀 있다 |
| 교체 기록 | 정비기록 엑셀 올리기(먼저 비교 → 반영), 업로드 이력·되돌리기, 기록 찾기(역·부품·쪽 넘기기), 전체 기록 엑셀로 받기 |
| 분석·시연 | 날씨 영향(날씨가 바뀌면 교체주기가 얼마나 달라지는지, 날씨 공식 기준) · 정비기록 시연(샘플 162건을 올리면 결과가 어떻게 바뀌는지 미리 계산, 저장 안 함) |

## 구성

```
기상자료개방포털 웹(수동) ──→ data/raw/aws/hourly ──→ [시간→일 집계] ──┐
                                                                        ↓
                                                          [주기 계산] psd/cycle.py
                                                                        ↓
정비기록 엑셀(원본) ─────┐                                                          MySQL
초기 데이터(선택) ───────┼──→ 비교·맞추기(psd/sync.py) → 주기 보정(psd/calibrate.py) ──┘
                          │
                   [API] psd/api.py (FastAPI) ──→ [화면] psd/web/ ──→ 엑셀·PDF 보고서
```

| 구성 | 파일 |
| --- | --- |
| AWS 날씨(수동 다운로드 → 집계) | `data/raw/aws/hourly/`(gitignore) → `scripts/preprocess_aws_weather.py` → `data/processed/aws_weather_daily.csv` |
| 주기 계산 | `psd/cycle.py` (AWS 기준, 역별 `aws_stn`으로 지점 묶음) |
| 고장 기록으로 주기 보정 | `psd/calibrate.py` (날씨 공식 값을 사전값으로 와이블 + 베이지안) |
| DB 모델 | `psd/models.py`, `psd/db.py`, `psd/catalog.py` |
| DB 만들기 | `scripts/init_db.py`, `scripts/compute_cycles.py`, (선택) `scripts/load_initial_records.py` |
| 경고 상태 | `psd/status.py` |
| 고장 기록 엑셀 입출력 | `psd/excel_io.py` |
| 엑셀(원본)과 DB 비교 | `psd/sync.py` |
| 더미·샘플 기록 만들기 | `scripts/generate_dummy_records.py`, `scripts/make_sample_records.py` |
| API | `psd/api.py` |
| 화면 | `psd/web/index.html` (API가 `/`에서 그대로 제공) |
| PDF 보고서 | `psd/report_pdf.py` |

## 날씨 데이터 다시 받기 (보통은 필요 없음)

**AWS(실제 계산에 쓰는 것, 수동):** API허브 AWS 엔드포인트가 아직 미승인(403)이라, 기상자료
개방포털(data.kma.go.kr) 웹 화면에 무료 회원가입으로 로그인해 "방재기상관측(AWS) → 파일셋"
에서 시간 자료를 받아야 한다(자료형태=시간 자료, 지역=부산·울산, 기간은 필요한 만큼).
`data/raw/aws/hourly/`에 CSV를 모은 뒤:

```bash
python scripts/preprocess_aws_weather.py   # 시간→일 평균 집계 → data/processed/aws_weather_daily.csv
python scripts/compute_cycles.py           # 새 날씨로 주기 다시 계산
```

원본은 `data/raw/`(저장소에 올리지 않음), 집계 결과는 `data/processed/aws_weather_daily.csv`(저장소에 올림)에 있다.
예전에 쓰던 ASOS(부산·울산 2곳) 자동 수집 스크립트와 자료는 계산에 쓰지 않아 2026-11-01에 지웠다.

## DB 다시 만들기

역/부품 구성 자체를 바꿨다면 `python scripts/init_db.py --reset`으로 표를 지우고 다시 만든다(교체 기록도
모두 지워진다). 그다음 `compute_cycles.py`와, 필요하면 `load_initial_records.py`를 다시 실행한다.
`init_db.py`·`compute_cycles.py`는 여러 번 실행해도 안전하다(있으면 지우고 새로 넣음, 없는 컬럼은 자동으로 추가).

**이미 설치한 PC에서 코드를 새로 받았다면** `python scripts/init_db.py`를 한 번 실행한다. 2026-11-01에
추가된 업로드 이력 표(`upload_batches`)와 교체 기록의 `batch_id` 컬럼이 만들어진다(기존 기록은 그대로).

## 정비기록 올리기 (엑셀이 원본)

현장은 정비기록을 엑셀로 관리하므로 **엑셀을 원본으로 두고, 올리면 DB를 그 엑셀에 맞춘다**(`psd/sync.py`).
화면에서 한 건씩 입력하는 기능은 없다 — 원본이 둘이 되면 다음 업로드 때 헷갈리기 때문이다. 잘못 적은
기록은 엑셀에서 고치거나 지운 뒤 다시 올린다.

1. "교체 기록" 탭에서 엑셀을 고르고 **비교하기**를 누른다. 아직 저장하지 않는다.
2. 지금 기록과 비교한 결과가 나온다: **새 기록 · 고친 기록**(같은 역·문·날짜인데 사유나 부품이 다름)
   **· 엑셀에 없는 기록 · 그대로**. 반영하면 교체주기·예정일이 어떻게 바뀌는지도 함께 나온다.
3. **반영하기**를 누르면 저장하고 업로드 이력에 한 줄 남긴다.

- **같은 파일을 다시 올려도 기록이 늘지 않는다**("바뀌는 기록이 없습니다"). 매달 누적 파일을 올려도 된다.
- **비교 범위**는 파일에 나온 역들의, 파일의 가장 이른 날짜 ~ 가장 늦은 날짜 사이다. 누적 파일이면 전체,
  한 달·한 역 파일이면 그 달·그 역하고만 비교하고 범위 밖 기록은 건드리지 않는다.
- **엑셀에 없는 기록은 기본으로 지우지 않는다.** 엑셀에서 지운 기록이 맞을 때만 "이 N건을 지웁니다"를 체크한다.
  20건이 넘으면 다른 파일을 고른 건 아닌지 경고한다.
- 엑셀 안에 똑같은 줄이 두 번 있으면 한 번만 넣고, DB에 같은 기록이 두 번 있으면 하나만 남긴다.
- **업로드 이력**에서 되돌릴 수 있다. 나중 업로드가 앞 업로드의 기록을 또 고쳤을 수 있어서, 가장 최근
  업로드부터 차례로만 되돌린다. 새로고침하거나 다음 날이 되어도 된다.
- "전체 기록 엑셀로 받기"는 DB의 기록을 업로드 양식 그대로 내려받는다. 원본 엑셀을 잃어버렸을 때 이 파일을
  이어 쓰면 되고, 그대로 다시 올리면 "변경 없음"이 나온다.

열 형식은 `psd/excel_io.py` 상단 설명 참고(역, 방향, 문번호, 교체일, 사유, 부품, 입력자). **첫 시트만 읽는다.**
"빈 양식 내려받기"의 첫 시트 "기록"은 열 이름만 있고, 예시는 둘째 시트 "작성 예시"에 있어 지우지 않고 올려도
들어가지 않는다. 한 행이라도 틀리면 전체를 저장하지 않는다(all-or-nothing).

API로도 같다(`delete_missing=true`는 엑셀에 없는 기록도 지움):

```bash
curl -F "file=@정비기록.xlsx" http://127.0.0.1:8000/api/replacement-records/compare   # 비교만
curl -F "file=@정비기록.xlsx" http://127.0.0.1:8000/api/replacement-records/sync      # 반영
```

올린 기록은 예정일뿐 아니라 교체주기에도 반영된다(`psd/calibrate.py`): 날씨 공식 값을 사전값으로 두고,
역 x 부품마다 고장 기록을 와이블(형상 2) + 베이지안으로 보정한다. 고장 없이 오래 버틴 기록이 쌓여 계산이 10년을 넘어도
예정일에는 제조사 권장 10년까지만 쓴다(부품 화면에 "계산상 ○년"으로 따로 보여 준다).

"점검(이상 없음)"은 부품을 바꾸지 않았다는 기록이라 설치일·교체주기는 그대로이고, 그 부품을 "기록이 관리되는
부품"으로 표시해 예정일이 지나도 "확인 필요"가 아니라 교체 대상으로 본다. 엑셀 부품 칸에 "전체"라고 쓰면 그 문의
부품 전부다.

**데이터 파일 (`data/dummy/`)**

| 파일 | 쓰는 곳 |
| --- | --- |
| `초기데이터_교체기록_1230건.xlsx` | 설치 6단계(선택). 더미 고장 기록 |
| `정비기록_샘플_162건.xlsx` | "정비기록 시연" 탭의 입력. 몇 달치 정비 기록(고장·예방 교체·함께 교체 30건 + 2개 역 정기 점검 132건)을 모아 올린다고 가정 |

다시 만들려면 `python scripts/make_sample_records.py`(샘플), `python scripts/generate_dummy_records.py --seed 1 --out FILE.xlsx`(더미)를
쓴다. 둘 다 그때의 DB 상태를 보고 만들기 때문에 지금 파일과 똑같이 나오지는 않는다.

## 보고서 출력

- 엑셀: `GET /api/report`(전체 역) 또는 `GET /api/report?station_id=1`(역 하나)
- PDF: `GET /api/report.pdf`(운영 중인 역 전부를 역 순서로 묶음, 몇 초 걸림) 또는 `GET /api/stations/{역 번호}/report.pdf`(역 하나, A4 2쪽)

화면에서는 현황(범위·형식 선택)과 역 화면(그 역, 형식 선택)의 "보고서 내려받기"에서 받는다.

## 테스트

```bash
python -m pytest tests/ -v
```

`tests/test_cycle.py`는 설계 문서 손계산 예시와 계산의 기본 성질(AWS 지점별 묶음 포함)을,
`tests/test_status.py`는 경고 상태(빨강·노랑·초록·확인 필요) 판정 규칙을, `tests/test_excel_io.py`는
엑셀 업로드 파싱을, `tests/test_sync.py`는 엑셀과 DB 기록 비교(새 기록·고친 기록·빠진 기록·중복)를,
`tests/test_calibrate.py`는 고장 기록으로 주기를 보정하는 계산을 검증한다.
`tests/test_api.py`만 예외로 **실제 MySQL DB가 필요하다**(설치 5단계까지 해 둔 DB).

## 코레일에서 받은 자료에 대하여

`PSD_부품_리스트.png`, `증빙/`, `서식5_프롬프트기록부_학생배포본.xlsx` 등은 수업·코레일 제공 비공개 자료라
이 저장소(공개)에 올리지 않는다(`.gitignore` 참고). `중간발표/`도 올리지 않는다.
