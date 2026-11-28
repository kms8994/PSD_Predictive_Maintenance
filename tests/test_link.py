"""원본 엑셀 연동(psd/link.py, 2026-11-27) 검증.

경로 검사·저장 중/잠김 판단은 DB 없이, 감지 상태(linked_files·link_events)와 API는 실제 MySQL로 본다.
연동 상태는 개발 DB에서 다른 세션과 같이 쓰는 한 줄이라, 테스트 앞뒤로 그대로 돌려놓는다(fixture state_guard).
실제 반영 시험은 되돌린 뒤 그 업로드 이력 줄까지 지운다.
"""
import os
import time
import uuid
from datetime import date
from pathlib import Path

import pytest

from psd import link

# ---------- 경로 검사(DB 없음) ----------


def test_validate_path_accepts_full_paths_and_strips_quotes(tmp_path):
    p = tmp_path / "장애이력.xlsx"
    assert link.validate_path(f'  "{p}"  ') == p
    assert link.validate_path(str(p).replace(".xlsx", ".XLSX")).suffix == ".XLSX"
    if os.name == "nt":
        assert str(link.validate_path(r"\\server\share\정비\장애이력.xlsx")) == r"\\server\share\정비\장애이력.xlsx"


@pytest.mark.parametrize("raw, why", [
    ("", "경로를 적으세요"),
    ("장애이력.xlsx", "전체 경로"),
    ("..\\장애이력.xlsx", "전체 경로"),
    ("C:\\정비\\장애이력.csv", ".xlsx 파일만"),
    ("C:\\정비\\장애이력.xlsm", ".xlsx 파일만"),
    ("C:\\정비\\~$장애이력.xlsx", "임시 파일"),
    ("C:\\정비\\장애이력.xlsx:숨김.xlsx", "':'"),
    ("C:\\정비\\장애\n이력.xlsx", "쓸 수 없는 글자"),
    ("\\\\?\\C:\\정비\\장애이력.xlsx", "쓸 수 없는 글자"),
    ("C:\\" + "가" * 500 + ".xlsx", "너무 깁니다"),
])
def test_validate_path_rejects_bad_paths(raw, why):
    with pytest.raises(link.LinkPathError, match=why):
        link.validate_path(raw)


def test_validate_path_keeps_to_allowed_folders(tmp_path, monkeypatch):
    allowed = tmp_path / "공유"
    allowed.mkdir()
    monkeypatch.setenv("PSD_LINK_ROOTS", f"{allowed};{tmp_path / '다른'}")
    assert link.validate_path(str(allowed / "a.xlsx")) == allowed / "a.xlsx"
    for bad in (tmp_path / "a.xlsx", allowed.parent / "공유2" / "a.xlsx", allowed / ".." / "a.xlsx"):
        with pytest.raises(link.LinkPathError, match="폴더 밖"):
            link.validate_path(str(bad))


# ---------- 저장 중·잠김(DB 없음) ----------


def _write(p: Path, content: bytes, age: float = 10.0) -> None:
    """파일을 쓰고 수정 시각을 age초 전으로(저장이 끝난 파일). 같은 크기로 다시 써도 수정 시각이 달라지게 현재 시각 기준."""
    p.write_bytes(content)
    t = time.time() - age
    os.utime(p, (t, t))


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook
    from io import BytesIO

    buf = BytesIO()
    Workbook().save(buf)
    return buf.getvalue()


def test_read_waits_while_file_was_just_saved(tmp_path):
    p = tmp_path / "a.xlsx"
    _write(p, b"x", age=0)
    with pytest.raises(link.FileNotReady) as e:
        link.read_stable(p)
    assert e.value.issue == "saving"
    _write(p, b"x", age=link.SETTLE_SECONDS + 1)
    assert link.read_stable(p).content == b"x"


def test_read_reports_locked_and_missing_files(tmp_path, monkeypatch):
    p = tmp_path / "a.xlsx"
    with pytest.raises(link.FileNotReady) as e:
        link.read_stable(p)
    assert e.value.issue == "missing"
    _write(p, b"x")

    def locked(_p):
        raise PermissionError(13, "다른 프로세스가 사용 중")

    monkeypatch.setattr(link, "_read_bytes", locked)
    with pytest.raises(link.FileNotReady) as e:
        link.read_stable(p)
    assert e.value.issue == "locked"


def test_read_waits_when_file_changes_while_reading(tmp_path, monkeypatch):
    p = tmp_path / "a.xlsx"
    _write(p, b"old")
    real = link._read_bytes

    def growing(path):
        data = real(path)
        _write(path, b"old and more", age=5)  # 읽는 사이 Excel이 저장함
        return data

    monkeypatch.setattr(link, "_read_bytes", growing)
    with pytest.raises(link.FileNotReady) as e:
        link.read_stable(p)
    assert e.value.issue == "saving"


def test_open_in_excel_still_reads_saved_file_but_waits_on_broken_one(tmp_path):
    """Excel에서 열어 둔 채(~$ 파일)라도 저장이 끝난 파일은 읽는다. 열려 있을 때 깨져 보이면 저장 중으로 기다린다."""
    p = tmp_path / "장애이력.xlsx"
    (tmp_path / "~$장애이력.xlsx").write_bytes(b"owner")
    _write(p, _xlsx_bytes())
    snap = link.read_stable(p)
    assert snap.excel_open and len(snap.hash) == 64
    _write(p, b"half written")
    with pytest.raises(link.FileNotReady) as e:
        link.read_stable(p)
    assert e.value.issue == "saving"


# ---------- 감지 상태(실제 DB, 가짜 비교) ----------


@pytest.fixture
def state_guard():
    """연동 상태 한 줄과 감지 기록을 테스트 전 그대로 돌려놓는다."""
    from sqlalchemy import delete, func, select

    from psd.db import engine, get_session
    from psd.models import LinkedFile, LinkEvent

    link.ensure_schema(engine)
    db = get_session()
    row = db.get(LinkedFile, 1)
    saved = {c.name: getattr(row, c.name) for c in LinkedFile.__table__.columns} if row else None
    max_event = db.scalar(select(func.coalesce(func.max(LinkEvent.id), 0)))
    db.close()
    yield
    db = get_session()
    try:
        row = db.get(LinkedFile, 1)
        if saved is None:
            if row is not None:
                db.delete(row)
        else:
            if row is None:
                row = LinkedFile(id=1)
                db.add(row)
            for k, v in saved.items():
                setattr(row, k, v)
        db.execute(delete(LinkEvent).where(LinkEvent.id > max_event))
        db.commit()
    finally:
        db.close()


class FakeCompare:
    """내용 → 비교 결과. b"bad..."는 틀린 파일. 몇 번 불렸는지 센다."""

    def __init__(self):
        self.calls = 0

    def __call__(self, content: bytes, file_name: str) -> dict:
        self.calls += 1
        if content.startswith(b"bad"):
            raise link.CompareFailed({"message": "2건 오류 — 아무것도 저장하지 않았습니다", "errors": ["13행: 역 이름을 못 찾음", "14행: 미래 날짜"]})
        n = content.count(b"+")
        return {"file": file_name, "counts": {"new": n, "changed": 0, "missing": 0, "db_duplicates": 0, "same": 5, "file_duplicates": 0},
                "warning_count": 0, "missing_warn": False, "scope": {"from": date(2026, 1, 1), "to": date(2026, 11, 1), "stations": 2}}


def _link_to(p: Path) -> None:
    from psd.db import get_session

    db = get_session()
    try:
        link.set_link(db, link.get_state(db), p, "테스트")
        db.commit()
    finally:
        db.close()


def _status() -> dict:
    from psd.db import get_session

    db = get_session()
    try:
        return link.status_dict(db)
    finally:
        db.close()


def _kinds(st: dict) -> list[str]:
    return [e["kind"] for e in st["events"]]


def test_change_is_detected_compared_once_and_shown_as_alert(tmp_path, state_guard):
    p = tmp_path / "장애이력.xlsx"
    _write(p, b"rows")
    _link_to(p)
    cmp = FakeCompare()
    assert link.check_once(cmp, full=True) == {"action": "same"}
    assert _status()["alert"] is None
    assert link.check_once(cmp) == {"action": "unchanged"}  # 수정 시각·크기 그대로면 읽지도 않는다
    assert cmp.calls == 1

    _write(p, b"rows+++", age=5)  # 현장이 3줄 적고 저장
    assert link.check_once(cmp) == {"action": "pending"}
    st = _status()
    assert (st["state"], st["alert"], st["summary"]["counts"]["new"]) == ("pending", "pending", 3)
    assert _kinds(st)[0] == "detected" and "새 3 · 고친 0 · 엑셀에 없는 0" in st["events"][0]["message"]

    _write(p, b"rows+++", age=3)  # 고치지 않고 다시 저장만: 수정 시각은 바뀌어도 내용이 같으면 다시 비교·알림 안 함
    assert link.check_once(cmp) == {"action": "same_content"}
    assert cmp.calls == 2 and _kinds(_status()).count("detected") == 1

    _write(p, b"rows+++++", age=2.5)  # 반영하기 전에 또 바뀜 → 최신 내용으로 다시 비교
    assert link.check_once(cmp) == {"action": "pending"}
    assert _status()["summary"]["counts"]["new"] == 5


def test_error_file_is_not_applicable_and_lists_wrong_rows(tmp_path, state_guard):
    p = tmp_path / "a.xlsx"
    _write(p, b"bad rows")
    _link_to(p)
    assert link.check_once(FakeCompare(), full=True) == {"action": "error"}
    st = _status()
    assert st["alert"] == "error" and st["summary"]["error_count"] == 2 and "13행" in st["summary"]["errors"][0]
    _write(p, b"fixed+", age=5)  # 엑셀에서 고쳐 저장
    assert link.check_once(FakeCompare()) == {"action": "pending"}


def test_change_made_while_server_was_off_is_found_on_start(tmp_path, state_guard):
    """서버가 꺼진 동안 바뀜: 상태는 DB에 있어 이어지고, 켜질 때(full) 수정 시각이 같아 보여도 내용 해시로 찾는다."""
    p = tmp_path / "a.xlsx"
    _write(p, b"rows")
    _link_to(p)
    link.check_once(FakeCompare(), full=True)
    mtime = p.stat().st_mtime_ns
    p.write_bytes(b"ro+s")  # 크기 같고
    os.utime(p, ns=(mtime, mtime))  # 수정 시각까지 같게 덮어씀(시각을 보존하는 복사)
    assert link.check_once(FakeCompare()) == {"action": "unchanged"}  # 평소 확인은 못 알아챈다
    w = link.Watcher(FakeCompare(), interval=60)  # 다시 켜진 서버
    w.start()
    try:
        for _ in range(100):
            if _status()["state"] == "pending":
                break
            time.sleep(0.1)
    finally:
        w.stop()
    st = _status()
    assert (st["state"], st["path"]) == ("pending", str(p)) and not w.running and w.last_error is None


def test_locked_or_missing_file_keeps_state_and_recovers(tmp_path, state_guard, monkeypatch):
    p = tmp_path / "a.xlsx"
    _write(p, b"rows+")
    _link_to(p)
    link.check_once(FakeCompare(), full=True)
    real = link._read_bytes

    def locked(_p):
        raise PermissionError(13, "잠김")

    monkeypatch.setattr(link, "_read_bytes", locked)
    _write(p, b"rows++", age=5)
    assert link.check_once(FakeCompare()) == {"action": "locked"}
    st = _status()
    assert (st["state"], st["issue"], st["alert"]) == ("pending", "locked", "pending")  # 잠깐 잠긴 건 알림으로 안 띄움
    monkeypatch.setattr(link, "_read_bytes", real)
    assert link.check_once(FakeCompare()) == {"action": "pending"}
    assert _status()["issue"] is None and _status()["summary"]["counts"]["new"] == 2

    p.unlink()  # 공유 폴더가 끊기거나 파일을 옮김
    assert link.check_once(FakeCompare()) == {"action": "missing"}
    assert link.check_once(FakeCompare()) == {"action": "missing"}
    st = _status()
    assert st["issue"] == "missing" and _kinds(st).count("missing") == 1  # 10초마다 기록이 쌓이지 않는다
    _write(p, b"rows++")
    link.check_once(FakeCompare())
    assert _status()["issue"] is None and _kinds(_status())[0] == "recovered"


def test_only_one_check_at_a_time(tmp_path, state_guard, monkeypatch):
    """비교·반영이 도는 동안(다른 스레드가 잡고 있음) 다음 확인은 기다리다 건너뛰고, 화면 요청은 '진행 중'을 받는다."""
    import threading

    started, release = threading.Event(), threading.Event()

    def hold():
        with link.exclusive():
            started.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    started.wait(5)
    monkeypatch.setattr(link, "LOCK_TIMEOUT", 0.2)
    try:
        cmp = FakeCompare()
        assert link.check_once(cmp) is None and cmp.calls == 0
        with pytest.raises(link.LinkBusy):
            with link.exclusive(timeout=0.2):
                pass
    finally:
        release.set()
        t.join()


def test_switching_file_starts_over_and_is_logged(tmp_path, state_guard):
    a, b = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    _write(a, b"a+")
    _write(b, b"b")
    _link_to(a)
    link.check_once(FakeCompare(), full=True)
    _link_to(b)
    st = _status()
    assert (st["path"], st["state"], st["seen_hash"]) == (str(b), "waiting", None)
    assert st["events"][0]["kind"] == "switch" and str(a) in st["events"][0]["message"]
    assert link.check_once(FakeCompare()) == {"action": "same"}


# ---------- API(실제 DB·실제 비교) ----------


def _record_xlsx(**over) -> bytes:
    from psd.excel_io import record_row, rows_to_excel_bytes

    base = dict(day=date(2026, 10, 3), station="부전", reason="prev", unit_code="RF", action="reset",
                symptom_detail=f"연동 테스트 {uuid.uuid4().hex[:8]}")
    return rows_to_excel_bytes([record_row(**{**base, **over})])


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from psd.api import app

    return TestClient(app)


def test_link_api_preview_set_compare_apply_and_undo(tmp_path, state_guard, client):
    """완료 기준: 연동 파일 지정 → 엑셀을 고쳐 저장 → 감지 알림 → 비교 결과 → 반영 → 업로드 이력("연동 파일 감지") → 되돌리기."""
    from psd.db import get_session
    from psd.models import UploadBatch

    p = tmp_path / "장애이력.xlsx"
    _write(p, _record_xlsx())
    assert client.post("/api/link/preview", json={"path": "장애이력.xlsx"}).status_code == 400

    pv = client.post("/api/link/preview", json={"path": str(p)}).json()
    assert pv["error"] is None and pv["result"]["counts"]["new"] == 1 and pv["file_name"] == "장애이력.xlsx"
    assert client.post("/api/link", json={"path": str(p), "hash": "0" * 64}).status_code == 409  # 본 내용과 다름
    st = client.post("/api/link", json={"path": str(p), "hash": pv["hash"], "set_by": "테스트"}).json()
    assert (st["path"], st["state"], st["alert"], st["set_by"]) == (str(p), "pending", "pending", "테스트")

    cmp = client.post("/api/link/compare", json={}).json()
    _write(p, _record_xlsx(), age=5)  # 비교 결과를 보는 사이 현장이 또 고침
    r = client.post("/api/link/apply", json={"hash": cmp["hash"]})
    assert r.status_code == 409  # 본 것과 다른 내용은 넣지 않는다
    assert client.get("/api/link").json()["seen_hash"] != cmp["hash"]  # 최신 내용으로 다시 비교해 둠

    cmp = client.post("/api/link/compare", json={}).json()
    assert cmp["result"]["counts"]["new"] == 1
    res = client.post("/api/link/apply", json={"hash": cmp["hash"], "uploaded_by": "테스트"})
    assert res.status_code == 201
    batch_id = res.json()["batch_id"]
    try:
        assert batch_id
        st = client.get("/api/link").json()
        assert (st["state"], st["alert"], st["applied_batch_id"]) == ("applied", None, batch_id)
        hist = client.get("/api/upload-batches?limit=5").json()["batches"]
        row = next(b for b in hist if b["id"] == batch_id)
        assert (row["source"], row["source_label"], row["file_name"], row["can_undo"]) == ("link", "연동 파일 감지", "장애이력.xlsx", True)
        # 다시 비교해도(같은 내용) 이미 반영한 것은 알림으로 다시 뜨지 않는다
        client.post("/api/link/check")
        assert client.get("/api/link").json()["state"] == "applied"
    finally:
        if batch_id:
            assert client.post(f"/api/upload-batches/{batch_id}/undo").status_code == 200
            st = client.get("/api/link").json()
            assert st["state"] == "undone" and st["events"][0]["kind"] == "undone"
            db = get_session()  # 시험으로 만든 업로드 이력 줄은 지운다(되돌려서 남은 기록이 없다)
            db.delete(db.get(UploadBatch, batch_id))
            db.commit()
            db.close()


def test_link_api_error_file_has_no_apply_and_unset(tmp_path, state_guard, client):
    p = tmp_path / "틀림.xlsx"
    _write(p, _record_xlsx(station="없는역"))
    pv = client.post("/api/link/preview", json={"path": str(p)}).json()
    assert pv["result"] is None and "없는역" in " ".join(pv["error"]["errors"])
    st = client.post("/api/link", json={"path": str(p), "hash": pv["hash"]}).json()
    assert (st["state"], st["alert"]) == ("error", "error")
    assert client.post("/api/link/apply", json={"hash": pv["hash"]}).status_code == 400  # 틀린 파일은 아무것도 저장하지 않는다
    d = client.post("/api/link/dismiss", json={"hash": pv["hash"]}).json()
    assert (d["state"], d["alert"]) == ("dismissed", None)
    st = client.delete("/api/link").json()
    assert st["path"] is None and st["alert"] is None and st["events"][0]["kind"] == "unset"
    assert client.post("/api/link/compare", json={}).status_code == 404


def test_link_api_waits_for_file_being_saved(tmp_path, state_guard, client):
    p = tmp_path / "a.xlsx"
    _write(p, _record_xlsx(), age=0)
    r = client.post("/api/link/preview", json={"path": str(p)})
    assert r.status_code == 409 and "저장" in r.json()["detail"]
