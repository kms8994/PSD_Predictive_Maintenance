# UI 개발 환경 설정 (Windows)

작업 브랜치: `ui/hoyun-redesign`

이 브랜치는 `main`의 `d97c6dd`에서 시작했다. 실제 화면 파일은
`psd/web/index.html`이며, 이 환경 설정 단계에서는 화면과 계산 로직을 바꾸지 않았다.

## 1. 저장소 받기

GitHub Desktop에서는 **File → Clone repository → URL**에 아래 주소를 입력한다.

```text
https://github.com/kms8994/PSD_Predictive_Maintenance.git
```

받은 후 **Current branch → ui/hoyun-redesign**을 선택한다. 이미 저장소가 있다면
변경 사항을 보관한 뒤 **Fetch origin**을 누르고 해당 브랜치를 선택한다.

Git 명령으로 새로 받을 경우 PowerShell에서 실행한다.

```powershell
git clone --branch ui/hoyun-redesign https://github.com/kms8994/PSD_Predictive_Maintenance.git
cd PSD_Predictive_Maintenance
git branch --show-current
```

이후 명령은 모두 저장소 폴더에서 실행한다.

## 2. Python 패키지 설치

Git, Python 3.12, MySQL 8.0 서버를 준비한다. 아래 명령은 Python의 Windows
런처인 `py`를 사용한다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

가상환경을 활성화하지 않고 전용 Python을 직접 실행하므로 PowerShell 실행 정책을
바꿀 필요가 없다. `requirements-dev.txt`는 기존 실행 패키지에 API 테스트용 `httpx`와
PDF 검증용 `pypdf`를 추가한다.

## 3. 개인용 DB와 설정 파일 만들기

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

`DB_PASSWORD`를 개인 개발용 비밀번호로 바꾼다. 현재 연결 코드가 URL 문자열을 직접
조합하므로 처음 설정할 때는 영문과 숫자로 구성한 비밀번호를 사용한다.
기존 전처리 날씨로 UI를 실행할 때 `KMA_AUTH_KEY`는 비워 두어도 된다.

MySQL Workbench의 로컬 관리자 연결에서 아래 SQL을 실행한다. 비밀번호는 `.env`에
적은 값으로 바꾼다. 이 설정은 개인 PC의 별도 DB를 사용한다.

```sql
CREATE DATABASE IF NOT EXISTS psd_hoyun_ui
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'psd_hoyun_ui'@'localhost'
  IDENTIFIED BY 'replace_with_your_local_password';
GRANT ALL PRIVILEGES ON psd_hoyun_ui.* TO 'psd_hoyun_ui'@'localhost';
```

`.env`와 `.venv/`는 기존 `.gitignore`에 포함되어 있다.

## 4. 초기 데이터와 계산 결과 생성

```powershell
.\.venv\Scripts\python.exe scripts/init_db.py
.\.venv\Scripts\python.exe scripts/compute_cycles.py
```

전처리된 AWS 날씨는 저장소에 이미 있으므로 다시 수집할 필요가 없다.
위 명령으로 역, 문, 부품 및 교체주기 계산 결과를 만든다.
ASOS 보관용 날씨 표도 채우려면 추가로 실행한다.

```powershell
.\.venv\Scripts\python.exe scripts/load_weather.py
```

초기 상태에는 교체 기록이 없다. 기록이 있는 화면도 확인하려면 서버 실행 후
**교체 기록 → 엑셀 업로드**에서 저장소의
`data/dummy/replacement_records_dummy.xlsx`를 한 번 업로드한다. 이는 실제 고장 이력이
아닌 개발·시연용 더미데이터다. 같은 파일을 반복 업로드하면 기록이 중복 추가된다.

`init_db.py --reset`은 해당 DB의 기존 표와 기록을 지우므로, 일반적인 UI 작업에는
위의 기본 명령만 사용한다.

## 5. 실행과 화면 수정

```powershell
.\.venv\Scripts\python.exe -m uvicorn psd.api:app --reload
```

- 실제 화면: http://127.0.0.1:8000/
- API 문서: http://127.0.0.1:8000/docs
- UI 수정 파일: `psd/web/index.html` (HTML, CSS, JavaScript)
- 참고용 목업: `목업/동해선_PSD_교체주기_목업.html`

실제 화면은 API와 MySQL을 사용하므로 HTML 파일을 더블클릭하거나 VS Code Live Server로
열지 말고 위 FastAPI 주소로 확인한다. UI 파일을 저장한 뒤 브라우저를 새로고침한다.
서버는 `Ctrl+C`로 종료한다.

## 6. 검증과 변경 저장

DB 초기화 및 주기 계산 후 전체 테스트를 실행한다. API 테스트는 위에서 설정한 로컬
MySQL을 사용한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

MySQL 연결 전에도 계산·상태·엑셀 처리 등 DB 연결이 필요 없는 테스트는 실행할 수 있다.
`.env` 파일은 먼저 준비해야 한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ --ignore=tests/test_api.py -q
```

작업 시작 전 브랜치 이름을 확인하고, 수정 후 해당 브랜치에 커밋·푸시한다.
GitHub Desktop에서는 **Commit to ui/hoyun-redesign → Push origin** 순서로 진행한다.
Git 명령을 사용할 경우에는 다음과 같다.

```powershell
git branch --show-current
git add psd/web/index.html
git commit -m "Update PSD interface"
git push
```

팀의 `main` 반영은 UI 작업을 마친 뒤 Pull Request로 진행한다.
