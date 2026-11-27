"""원본 엑셀 연동 (날씨_역분석_엑셀연동_설계.md 4장, 2026-11-27).

현장 엑셀 파일 하나를 "연동 파일"로 정해 두면 서버가 INTERVAL_SECONDS마다 그 파일을 들여다보고, 내용이 바뀌었으면
DB와 **비교만** 해 둔다. DB에 넣는 건 사람이 화면에서 비교 결과를 보고 "반영"을 눌렀을 때뿐이다
(2026-11-27 사용자 결정: 자동 반영 없음 — 새·고친·지울 기록 모두). 반영은 psd/api.py가 화면 업로드와 같은 _sync로 하고,
업로드 이력에 source="link"("연동 파일 감지")로 남아 되돌릴 수 있다.

감지
- 수정 시각·크기가 그대로면 다시 읽지 않는다. 바뀌었으면 읽어서 내용 해시(SHA-256)를 본다 — 내용이 같으면
  (고치지 않고 저장만 함) 알리지 않는다.
- 저장 중이면 기다린다: 수정된 지 SETTLE_SECONDS가 안 됐거나, 읽는 동안 크기·수정 시각이 바뀌었거나, 잠겨서
  못 읽으면 이번엔 건너뛰고 조금 뒤 다시 본다.
- Excel에서 **열려 있는 것(~$ 임시 파일)만으로는 기다리지 않는다.** 저장하고 창을 열어 둔 채 두는 게 보통이라,
  닫을 때까지 기다리면 저장한 내용을 몇 시간씩 못 본다. 대신 열려 있을 때 파일이 깨져(zip이 아니게) 보이면 저장 중으로 본다.
- 서버가 꺼져 있던 동안 바뀐 것: 켜질 때는 수정 시각과 상관없이 한 번 읽어, 마지막으로 본 내용과 해시를 비교한다.
- 감지·비교·반영은 한 번에 하나(_lock). 감시는 스레드 하나가 한다(서버 프로세스 하나를 전제로 한다).

상태는 모두 DB(linked_files 한 줄, link_events 기록)에 있어 서버를 다시 켜도 이어진다.

보안: 경로는 .xlsx 파일의 전체 경로만 받는다(validate_path). 환경변수 PSD_LINK_ROOTS(;로 구분한 폴더)를 두면
그 폴더 안의 파일만 받는다 — 로그인이 아직 없어서, 서버 PC의 아무 엑셀이나 읽히지 않게 운영 PC에서는 두는 걸 권한다.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import stat as stat_mod
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import delete, inspect, select, text
from sqlalchemy.orm import Session

from psd.db import get_session
from psd.models import LinkedFile, LinkEvent

log = logging.getLogger("psd.link")

INTERVAL_SECONDS = float(os.environ.get("PSD_LINK_INTERVAL", "10"))  # 파일을 들여다보는 간격
SAVING_RETRY_SECONDS = 3.0  # 저장 중이었으면 이만큼 뒤에 다시 본다
SETTLE_SECONDS = 2.0  # 수정된 지 이만큼 지나야 읽는다(저장이 끝났다고 본다)
LOCKED_ALERT = timedelta(minutes=5)  # 잠김이 이만큼 이어지면 화면 위에 알린다
MAX_BYTES = 50 * 1024 * 1024  # 이보다 큰 파일은 받지 않는다(장애 이력 누적 파일은 수 MB)
MAX_PATH_LEN = 400
KEEP_EVENTS = 5000  # 감지 기록은 최근 이만큼만 둔다
LOCK_TIMEOUT = 120.0

STATE_LABEL = {
    "waiting": "아직 안 봄", "same": "DB와 같음", "pending": "바뀜 — 반영 기다림", "error": "틀린 줄 있음 — 반영 못 함",
    "applied": "반영함", "dismissed": "이번 변경은 반영 안 함", "undone": "반영을 되돌림",
}
ISSUE_LABEL = {"saving": "저장 중", "locked": "잠김", "missing": "파일 없음", "failed": "확인 실패"}
EVENT_LABEL = {
    "set": "연동 시작", "switch": "연동 파일 바꿈", "unset": "연동 끊음", "detected": "변경 감지", "nochange": "변경 감지(DB와 같음)",
    "error": "틀린 줄 있음", "applied": "반영", "dismissed": "반영 안 함", "undone": "반영 되돌림", "missing": "파일 없음",
    "failed": "확인 실패", "recovered": "다시 읽힘",
}

_lock = threading.Lock()


class LinkPathError(ValueError):
    """경로가 틀렸다(사람이 고쳐야 함)."""


class FileNotReady(Exception):
    """지금은 못 읽는다(저장 중·잠김·없음) — 조금 뒤 다시 본다. issue는 LinkedFile.issue 값."""

    def __init__(self, issue: str, message: str):
        self.issue = issue
        super().__init__(message)


class CompareFailed(Exception):
    """파일 내용이 틀려 비교할 수 없다(양식이 아님·틀린 줄). detail은 _sync가 내는 400 응답과 같은 모양(글 또는 {message, errors})."""

    def __init__(self, detail):
        self.detail = detail
        super().__init__(str(detail))


class LinkBusy(Exception):
    """다른 확인·반영이 진행 중."""


Compare = Callable[[bytes, str], dict]  # (파일 내용, 파일 이름) -> _sync 비교 결과. 파일 문제는 CompareFailed


@dataclass(frozen=True)
class Snapshot:
    content: bytes
    hash: str
    mtime_ns: int
    size: int
    excel_open: bool


# ---------- 경로 ----------


def allowed_roots() -> list[Path]:
    raw = os.environ.get("PSD_LINK_ROOTS", "")
    return [Path(os.path.abspath(r.strip().strip('"'))) for r in raw.split(";") if r.strip().strip('"')]


def _under(p: Path, root: Path) -> bool:
    a = os.path.normcase(os.path.realpath(p))
    b = os.path.normcase(os.path.realpath(root)).rstrip("\\/")
    return a.startswith(b + os.sep)


def validate_path(raw) -> Path:
    """연동 파일 경로 검사. 서버 PC에서 보이는 .xlsx 파일의 전체 경로(공유 폴더 \\\\서버\\폴더\\... 도 됨)만 받는다.
    탐색기 "경로로 복사"가 붙이는 따옴표는 떼어 준다. 파일이 실제 있는지는 읽을 때 본다(read_stable)."""
    if not isinstance(raw, str):
        raise LinkPathError("경로를 글로 적으세요")
    s = raw.strip().strip('"').strip()
    if not s:
        raise LinkPathError("연동할 엑셀 파일의 경로를 적으세요")
    if len(s) > MAX_PATH_LEN:
        raise LinkPathError(f"경로가 너무 깁니다({MAX_PATH_LEN}자까지)")
    if any(ord(c) < 32 for c in s) or re.search(r'[<>|?*"]', s):
        raise LinkPathError('경로에 쓸 수 없는 글자(< > | ? * " 또는 줄바꿈)가 있습니다')
    if ":" in s and not re.match(r"^[A-Za-z]:[\\/]", s) or s.count(":") > 1:
        raise LinkPathError("경로의 ':'는 드라이브 이름(C:\\ 등)에만 쓸 수 있습니다")
    p = Path(os.path.normpath(s))
    if not p.is_absolute() or (os.name == "nt" and not (re.match(r"^[A-Za-z]:\\", str(p)) or str(p).startswith("\\\\"))):
        raise LinkPathError("전체 경로로 적으세요(예: D:\\정비\\장애이력.xlsx 또는 \\\\서버\\공유폴더\\장애이력.xlsx)")
    if p.suffix.lower() != ".xlsx":
        raise LinkPathError(".xlsx 파일만 연동할 수 있습니다")
    if p.name.startswith("~$"):
        raise LinkPathError("Excel이 만든 임시 파일(~$로 시작)입니다 — 원래 파일을 고르세요")
    roots = allowed_roots()
    if roots and not any(_under(p, r) for r in roots):
        raise LinkPathError("연동할 수 있는 폴더 밖의 파일입니다(허용 폴더: " + "; ".join(map(str, roots)) + ")")
    return p


def excel_open(p: Path) -> bool:
    """Excel에서 열려 있는지: 같은 폴더의 ~$ 임시 파일(이름이 길면 앞 두 글자를 바꿔 붙인다)."""
    try:
        return p.with_name("~$" + p.name).exists() or (len(p.name) > 2 and p.with_name("~$" + p.name[2:]).exists())
    except OSError:
        return False


def _read_bytes(p: Path) -> bytes:
    with open(p, "rb") as fh:
        return fh.read(MAX_BYTES + 1)


def read_stable(p: Path, now: float | None = None) -> Snapshot:
    """저장이 끝난 파일만 읽는다. 지금 못 읽으면 FileNotReady(조금 뒤 다시), 파일 자체가 안 되면 LinkPathError."""
    try:
        st1 = p.stat()
    except FileNotFoundError:
        raise FileNotReady("missing", f"파일이 없습니다: {p}")
    except PermissionError:
        raise FileNotReady("locked", "파일 정보를 읽을 권한이 없습니다(공유 폴더 권한 확인)")
    except OSError as e:
        raise FileNotReady("missing", f"파일에 닿을 수 없습니다(공유 폴더 연결 확인): {e.strerror or e}")
    if not stat_mod.S_ISREG(st1.st_mode):
        raise LinkPathError("파일이 아닙니다(폴더 등)")
    if st1.st_size > MAX_BYTES:
        raise LinkPathError(f"파일이 너무 큽니다({st1.st_size / 1e6:.0f}MB, {MAX_BYTES // 2**20}MB까지)")
    if (time.time() if now is None else now) - st1.st_mtime < SETTLE_SECONDS:
        raise FileNotReady("saving", "방금 저장된 파일이라 저장이 끝나길 기다립니다")
    opened = excel_open(p)
    try:
        content = _read_bytes(p)
    except FileNotFoundError:
        raise FileNotReady("saving", "저장하는 중이라 잠깐 파일이 없습니다")
    except PermissionError:
        raise FileNotReady("locked", "다른 프로그램이 파일을 잠가 읽을 수 없습니다 — 조금 뒤 다시 봅니다")
    except OSError as e:
        raise FileNotReady("locked", f"파일을 읽지 못했습니다 — 조금 뒤 다시 봅니다({e.strerror or e})")
    try:
        st2 = p.stat()
    except OSError:
        raise FileNotReady("saving", "저장하는 중이라 잠깐 파일이 없습니다")
    if (st2.st_mtime_ns, st2.st_size) != (st1.st_mtime_ns, st1.st_size) or len(content) != st1.st_size:
        raise FileNotReady("saving", "읽는 동안 파일이 바뀌었습니다 — 저장이 끝나길 기다립니다")
    if opened and not zipfile.is_zipfile(io.BytesIO(content)):
        raise FileNotReady("saving", "Excel이 저장하는 중으로 보입니다")
    return Snapshot(content, hashlib.sha256(content).hexdigest(), st1.st_mtime_ns, st1.st_size, opened)


# ---------- DB ----------


def ensure_schema(engine) -> list[str]:
    """연동 표 2개와 upload_batches.source 칸이 없으면 만든다(서버가 켜질 때·scripts/init_db.py). 한 일 목록."""
    done = []
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for t in (LinkedFile.__table__, LinkEvent.__table__):
        if t.name not in tables:
            t.create(engine, checkfirst=True)
            done.append(f"{t.name} 표 만듦")
    if "upload_batches" in tables and "source" not in {c["name"] for c in insp.get_columns("upload_batches")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE upload_batches ADD COLUMN source VARCHAR(20) NULL"))
        done.append("upload_batches.source 칸 추가")
    return done


def get_state(db: Session) -> LinkedFile:
    """연동 상태 한 줄(잠가서 읽는다 — _lock 안에서만 부른다). 없으면 만든다."""
    st = db.execute(select(LinkedFile).where(LinkedFile.id == 1).with_for_update()).scalar_one_or_none()
    if st is None:
        st = LinkedFile(id=1, state="waiting", excel_open=False)
        db.add(st)
        db.flush()
    return st


def add_event(db: Session, kind: str, message: str, path: str | None = None, file_hash: str | None = None) -> None:
    ev = LinkEvent(at=datetime.now(), kind=kind, path=path, file_hash=file_hash, message=message[:500])
    db.add(ev)
    db.flush()
    log.info("연동 %s: %s (%s)", EVENT_LABEL.get(kind, kind), message, path)
    if ev.id % 100 == 0:
        db.execute(delete(LinkEvent).where(LinkEvent.id <= ev.id - KEEP_EVENTS))


@contextmanager
def exclusive(timeout: float | None = None):
    """감지·비교·반영·설정 바꾸기를 한 번에 하나만."""
    if not _lock.acquire(timeout=LOCK_TIMEOUT if timeout is None else timeout):
        raise LinkBusy("다른 확인·반영이 진행 중입니다. 잠시 뒤 다시 하세요")
    try:
        yield
    finally:
        _lock.release()


def counts_text(c: dict) -> str:
    return f"새 {c['new']} · 고친 {c['changed']} · 엑셀에 없는 {c['missing']}"


def has_changes(result: dict) -> bool:
    c = result["counts"]
    return bool(c["new"] or c["changed"] or c["missing"] or c["db_duplicates"])


def _summary(result: dict) -> dict:
    sc = result["scope"]
    return {"file": result["file"], "counts": result["counts"], "warning_count": result["warning_count"],
            "missing_warn": result["missing_warn"],
            "scope": {"from": str(sc["from"]), "to": str(sc["to"]), "stations": sc["stations"]}}


def error_summary(detail) -> dict:
    if isinstance(detail, dict):
        errors = [str(e) for e in detail.get("errors", [])]
        return {"message": str(detail.get("message", "")), "errors": errors[:100], "error_count": len(errors)}
    return {"message": str(detail), "errors": [], "error_count": 0}


def set_issue(db: Session, st: LinkedFile, issue: str, message: str) -> None:
    """잠깐의 문제를 적는다. 파일 없음·확인 실패만 기록(로그)에 남긴다 — 저장 중·잠김은 몇 초짜리라 쌓이기만 한다."""
    if st.issue != issue:
        st.issue_since = datetime.now()
        if issue in ("missing", "failed"):
            add_event(db, issue, message, st.path)
    st.issue, st.issue_message = issue, message[:500]


def clear_issue(db: Session, st: LinkedFile) -> None:
    if st.issue in ("missing", "failed", "locked"):
        add_event(db, "recovered", f"{ISSUE_LABEL[st.issue]} 상태가 풀려 다시 읽었습니다", st.path)
    st.issue = st.issue_message = st.issue_since = None


def note_compare(db: Session, st: LinkedFile, snap: Snapshot, result: dict | None, error=None) -> None:
    """파일을 비교한 결과를 상태에 적는다. 내용이 새로 바뀐 것이면 감지 기록을 남긴다.

    같은 내용을 다시 비교한 것이면(화면에서 비교 결과 보기·지금 확인) 건수만 새로 고친다 — 이미 반영했거나
    "반영 안 함"으로 둔 내용이 엑셀에 없는 기록 때문에 다시 알림으로 뜨지 않게, 그 상태는 그대로 둔다.
    """
    now = datetime.now()
    new_content = snap.hash != st.seen_hash
    st.compared_at = now
    st.file_mtime_ns, st.file_size, st.excel_open = snap.mtime_ns, snap.size, snap.excel_open
    if new_content:
        st.seen_hash, st.seen_at = snap.hash, now
    if error is not None:
        st.summary = error_summary(error)
        if new_content or st.state != "error":  # 문구에 이미 "N건 오류"가 들어 있다. 첫 틀린 줄을 붙여 둔다
            first = st.summary["errors"][:1]
            add_event(db, "error", st.summary["message"] + (f" ({first[0]}{' 등' if st.summary['error_count'] > 1 else ''})" if first else ""),
                      st.path, snap.hash)
        st.state = "error"
        return
    st.summary = _summary(result)
    if not new_content and st.state in ("applied", "dismissed", "undone"):
        return
    state = "pending" if has_changes(result) else "same"
    if new_content:
        add_event(db, "detected" if state == "pending" else "nochange", counts_text(result["counts"]), st.path, snap.hash)
    st.state = state


def set_link(db: Session, st: LinkedFile, p: Path, set_by: str | None) -> None:
    """연동 파일을 정하거나 바꾼다. 앞 파일의 상태는 지우고(기록에는 남음) 새 파일은 처음부터 본다."""
    old = st.path
    st.path, st.set_at, st.set_by = str(p), datetime.now(), (set_by or "").strip()[:50] or None
    _reset(st)
    if old and old != str(p):
        add_event(db, "switch", f"{old} → {p}", str(p))
    else:
        add_event(db, "set", str(p), str(p))


def unset_link(db: Session, st: LinkedFile) -> None:
    if st.path:
        add_event(db, "unset", st.path, st.path)
    st.path = st.set_at = st.set_by = None
    _reset(st)


def _reset(st: LinkedFile) -> None:
    st.state, st.summary, st.seen_hash, st.seen_at, st.compared_at, st.checked_at = "waiting", None, None, None, None, None
    st.file_mtime_ns = st.file_size = None
    st.issue = st.issue_message = st.issue_since = None
    st.excel_open = False
    st.applied_hash = st.applied_at = st.applied_batch_id = None


def mark_applied(db: Session, st: LinkedFile, snap: Snapshot, result: dict) -> None:
    """사람이 "반영"을 눌러 저장한 뒤(같은 트랜잭션). 바뀐 게 없었으면 업로드 이력은 없다."""
    note_compare(db, st, snap, result)
    st.state, st.applied_hash, st.applied_at, st.applied_batch_id = "applied", snap.hash, datetime.now(), result["batch_id"]
    what = (f"업로드 이력 {result['batch_id']}번 — 넣음 {result['inserted']} · 고침 {result['counts']['changed']} · 지움 {result['deleted']}"
            if result["batch_id"] else "바뀐 기록이 없어 저장할 것이 없었습니다")
    add_event(db, "applied", what, st.path, snap.hash)


def dismiss(db: Session, st: LinkedFile) -> None:
    st.state = "dismissed"
    add_event(db, "dismissed", "이번 변경은 반영하지 않기로 했습니다(파일이 또 바뀌면 다시 알립니다)", st.path, st.seen_hash)


def on_batch_undone(db: Session, batch_id: int) -> None:
    """연동 파일로 반영한 업로드를 되돌리면 상태를 "되돌림"으로(같은 내용으로 다시 알리지는 않는다)."""
    st = db.get(LinkedFile, 1)
    if st is not None and st.path and st.applied_batch_id == batch_id:
        st.state = "undone"
        add_event(db, "undone", f"업로드 이력 {batch_id}번을 되돌렸습니다 — 다시 반영하려면 '비교 결과 보기'", st.path, st.applied_hash)


# ---------- 감시 ----------


def check_once(compare: Compare, *, full: bool = False, recompare: bool = False) -> dict | None:
    """연동 파일을 한 번 들여다본다. full: 수정 시각이 같아도 읽는다(서버가 켜질 때). recompare: 내용이 같아도 다시 비교.
    한 일 {"action": ...}을 돌려준다. 다른 확인·반영이 돌고 있으면 None."""
    try:
        with exclusive():
            db = get_session()
            try:
                out = _check(db, compare, full, recompare)
                db.commit()
                return out
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
    except LinkBusy:
        return None


def _check(db: Session, compare: Compare, full: bool, recompare: bool) -> dict:
    st = get_state(db)
    if not st.path:
        return {"action": "off"}
    st.checked_at = datetime.now()
    try:
        p = validate_path(st.path)  # 허용 폴더(PSD_LINK_ROOTS)를 나중에 바꿨을 수 있다
    except LinkPathError as e:
        set_issue(db, st, "failed", str(e))
        return {"action": "failed"}
    if not (full or recompare) and st.issue is None and st.file_mtime_ns is not None:
        try:
            s = p.stat()
            if (s.st_mtime_ns, s.st_size) == (st.file_mtime_ns, st.file_size):
                return {"action": "unchanged"}
        except OSError:
            pass
    try:
        snap = read_stable(p)
    except FileNotReady as e:
        set_issue(db, st, e.issue, str(e))
        return {"action": e.issue}
    except LinkPathError as e:
        set_issue(db, st, "failed", str(e))
        return {"action": "failed"}
    if snap.hash == st.seen_hash and not recompare:
        st.file_mtime_ns, st.file_size, st.excel_open = snap.mtime_ns, snap.size, snap.excel_open
        clear_issue(db, st)
        return {"action": "same_content"}
    try:
        result, error = compare(snap.content, p.name), None
    except CompareFailed as e:
        result, error = None, e.detail
    except Exception as e:  # 버그·DB 문제: 내용 상태는 그대로 두고 다음에 다시 본다
        log.exception("연동 파일 비교 실패")
        set_issue(db, st, "failed", f"비교하다 오류가 났습니다: {type(e).__name__}: {e}")
        return {"action": "failed"}
    clear_issue(db, st)
    note_compare(db, st, snap, result, error)
    return {"action": st.state}


class Watcher:
    """서버가 켜져 있는 동안 연동 파일을 들여다보는 스레드 하나."""

    def __init__(self, compare: Compare, interval: float = INTERVAL_SECONDS):
        self.compare, self.interval = compare, interval
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="psd-link-watch", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        full = True  # 켜질 때는 수정 시각과 상관없이 읽어 본다(꺼져 있던 동안 바뀐 것)
        while not self._stop.is_set():
            wait = self.interval
            try:
                out = check_once(self.compare, full=full)
                full = False
                self.last_error = None
                if out and out["action"] == "saving":
                    wait = min(self.interval, SAVING_RETRY_SECONDS)
            except Exception as e:  # DB가 잠깐 끊겨도 스레드는 살아 있게
                log.exception("연동 파일 확인 실패")
                self.last_error = f"{type(e).__name__}: {e}"
            self.last_run = datetime.now()
            self._stop.wait(wait)


# ---------- 화면에 줄 상태 ----------


def alert_of(st: LinkedFile | None, now: datetime | None = None) -> str | None:
    """화면 위(머리 영역)에 띄울 알림 종류: pending | error | missing | failed | locked | None."""
    if st is None or not st.path:
        return None
    if st.state in ("pending", "error"):
        return st.state
    if st.issue in ("missing", "failed"):
        return st.issue
    if st.issue == "locked" and st.issue_since and (now or datetime.now()) - st.issue_since >= LOCKED_ALERT:
        return "locked"
    return None


def status_dict(db: Session, watcher: Watcher | None = None, events: int = 15) -> dict:
    st = db.get(LinkedFile, 1)
    evs = db.execute(select(LinkEvent).order_by(LinkEvent.id.desc()).limit(events)).scalars().all()
    on = st is not None and bool(st.path)
    g = (lambda k: getattr(st, k)) if on else (lambda k: None)  # noqa: E731
    return {
        "path": g("path"), "file_name": Path(st.path).name if on else None, "set_at": g("set_at"), "set_by": g("set_by"),
        "state": st.state if on else None, "state_label": STATE_LABEL.get(st.state) if on else None,
        "summary": g("summary"), "seen_hash": g("seen_hash"), "seen_at": g("seen_at"), "compared_at": g("compared_at"),
        "checked_at": g("checked_at"), "size": g("file_size"),
        "modified": datetime.fromtimestamp(st.file_mtime_ns / 1e9).replace(microsecond=0) if on and st.file_mtime_ns else None,
        "issue": g("issue"), "issue_label": ISSUE_LABEL.get(st.issue) if on and st.issue else None,
        "issue_message": g("issue_message"), "issue_since": g("issue_since"), "excel_open": bool(on and st.excel_open),
        "applied_at": g("applied_at"), "applied_batch_id": g("applied_batch_id"),
        "alert": alert_of(st),
        "events": [{"at": e.at, "kind": e.kind, "label": EVENT_LABEL.get(e.kind, e.kind), "message": e.message, "path": e.path}
                   for e in evs],
        "watch": {"running": bool(watcher and watcher.running), "interval": watcher.interval if watcher else INTERVAL_SECONDS,
                  "last_run": watcher.last_run if watcher else None, "error": watcher.last_error if watcher else None},
        "roots": [str(r) for r in allowed_roots()],
    }
