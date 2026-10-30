"""
응급 의료정보 QR 공유 API + 모바일 조회 페이지 (Flask Blueprint)

흐름
  1) 앱이 선택된 환자의 스냅샷을 POST /api/qr-share 로 보낸다.
  2) 서버는 임의 토큰(256bit)과 만료 시각을 발급한다. 토큰 원문은 저장하지 않고 SHA-256 해시만 저장한다.
  3) QR에는 환자 정보 대신 https://<서버>/share#t=<토큰> 주소만 담긴다.
     URL fragment(#...)는 브라우저가 서버로 보내지 않으므로 접속 로그에 토큰이 남지 않는다.
  4) 조회 페이지(/share)가 fragment에서 토큰을 읽어 GET /api/qr-share 를 X-Share-Token 헤더로 호출한다.
  5) 만료·폐기되면 스냅샷은 즉시(조회 시점 또는 다음 생성 시점에) 지워지고 상태만 남는다.

환경변수
  QR_MODE              demo(기본) | production
                       demo       : 로그인 없이 동작, 가상 환자 정보로만 시연 (응답에 demo=true, 조회 화면에 안내 배너)
                       production : 생성은 QR_CREATOR_TOKENS, 조회는 QR_VIEWER_TOKENS 의 Bearer 인증을 요구한다.
                                    값이 비어 있으면 요청을 거부한다(fail closed).
  QR_CREATOR_TOKENS    쉼표로 구분한 생성자(앱) 자격 증명
  QR_VIEWER_TOKENS     쉼표로 구분한 조회자(구급대원·의료진) 자격 증명
  QR_DB_PATH           SQLite 경로 (기본: 이 파일 옆 qr_shares.db). Railway에서 재배포 후에도 유지하려면 Volume 경로 지정
  QR_DEFAULT_TTL_MIN   기본 유효 시간(분, 기본 60). 요청값은 5분~24시간으로 제한
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, send_from_directory

bp = Blueprint("qr_share", __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "share_static")

MIN_TTL = 5 * 60
MAX_TTL = 24 * 60 * 60
TOMBSTONE_KEEP = 7 * 24 * 60 * 60          # 만료·폐기 표시만 남겨두는 기간(스냅샷은 이미 삭제됨)
MAX_BODY = 96 * 1024
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")      # secrets.token_urlsafe(32)
HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_auto_db = None
_db_lock = threading.Lock()
_db_ready = set()


# ─────────────────────────────────────────
# 설정 / 저장소
# ─────────────────────────────────────────
def _mode():
    return "production" if os.environ.get("QR_MODE", "demo").strip().lower() == "production" else "demo"


def _db_path():
    """QR_DB_PATH > 앱 폴더 > 임시 폴더 순. 앱 폴더가 읽기 전용인 배포 환경에서도 동작하도록 한다."""
    env = os.environ.get("QR_DB_PATH")
    if env:
        return env
    global _auto_db
    if _auto_db is None:
        if _writable(BASE_DIR):
            _auto_db = os.path.join(BASE_DIR, "qr_shares.db")
        else:
            import tempfile
            _auto_db = os.path.join(tempfile.gettempdir(), "qr_shares.db")
    return _auto_db


def _writable(folder):
    try:
        probe = os.path.join(folder, ".qr_write_test")
        with open(probe, "w") as f:
            f.write("1")
        os.remove(probe)
        return True
    except OSError:
        return False


def _default_ttl():
    try:
        v = int(os.environ.get("QR_DEFAULT_TTL_MIN", "60")) * 60
    except ValueError:
        v = 3600
    return max(MIN_TTL, min(MAX_TTL, v))


def _db():
    path = _db_path()
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    if path not in _db_ready:
        with _db_lock:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS shares (
                       id          TEXT PRIMARY KEY,      -- sha256(token)
                       revoke_hash TEXT NOT NULL,         -- sha256(revoke_key)
                       snapshot    TEXT,                  -- JSON, 만료·폐기 시 NULL
                       demo        INTEGER NOT NULL,
                       created_at  INTEGER NOT NULL,
                       expires_at  INTEGER NOT NULL,
                       revoked_at  INTEGER
                   )"""
            )
            conn.commit()
            _db_ready.add(path)
    return conn


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _purge(conn, now):
    """만료·폐기된 공유의 환자 정보를 지우고, 오래된 표시도 정리한다."""
    conn.execute(
        "UPDATE shares SET snapshot=NULL WHERE snapshot IS NOT NULL AND (revoked_at IS NOT NULL OR expires_at<=?)",
        (now,),
    )
    conn.execute("DELETE FROM shares WHERE expires_at<? ", (now - TOMBSTONE_KEEP,))


# ─────────────────────────────────────────
# 응답 공통 처리 (캐시 금지, 검색·참조 차단)
# ─────────────────────────────────────────
def _api(payload, status=200, headers=None):
    resp = jsonify(payload)
    resp.status_code = status
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


@bp.after_request
def _secure_headers(resp):
    if request.path.startswith("/api/qr-share") or request.path.startswith("/share"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        resp.headers["Vary"] = "Origin, X-Share-Token, Authorization"
    if request.path.startswith("/share"):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        resp.headers["X-Frame-Options"] = "DENY"
    return resp


# ─────────────────────────────────────────
# 요청 제한 (토큰 추측·남용 방지, 프로세스 메모리 기준)
# ─────────────────────────────────────────
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[-1].strip()          # 신뢰하는 프록시(Railway)가 덧붙인 마지막 값
    return request.remote_addr or "?"


def _limited(bucket, limit, window):
    now = time.monotonic()
    key = (bucket, _client_ip())
    with _hits_lock:
        q = _hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return int(window - (now - q[0])) + 1
        q.append(now)
        if len(_hits) > 5000:                       # 메모리 상한
            for k in [k for k, v in _hits.items() if not v][:1000]:
                _hits.pop(k, None)
    return 0


# ─────────────────────────────────────────
# 인증 (production)
# ─────────────────────────────────────────
def _bearer_ok(env_name):
    """(ok, error_response). demo 모드는 인증 없이 통과."""
    if _mode() == "demo":
        return True, None
    allowed = [t.strip() for t in os.environ.get(env_name, "").split(",") if t.strip()]
    if not allowed:
        return False, _api({"error": "auth_not_configured"}, 503)
    auth = request.headers.get("Authorization", "")
    given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if given and any(hmac.compare_digest(given.encode(), a.encode()) for a in allowed):
        return True, None
    return False, _api({"error": "unauthorized"}, 401, {"WWW-Authenticate": 'Bearer realm="qr-share"'})


# ─────────────────────────────────────────
# 입력 검증 (스냅샷)
# ─────────────────────────────────────────
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f  ]")


def _txt(v, maxlen, field):
    if not isinstance(v, str):
        raise ValueError(field)
    v = _CTRL_RE.sub("", v).replace("\r", " ").replace("\n", " ").strip()
    if not v or len(v) > maxlen:
        raise ValueError(field)
    return v


def _list(v, field):
    """None = 미입력, [] = '없음'으로 입력, [..] = 항목들"""
    if v is None:
        return None
    if not isinstance(v, list) or len(v) > 30:
        raise ValueError(field)
    return [_txt(x, 120, field) for x in v]


BLOOD_TYPES = {"A+", "A-", "B+", "B-", "O+", "O-", "AB+", "AB-", "모름"}


def clean_snapshot(d):
    if not isinstance(d, dict):
        raise ValueError("patient")
    age = d.get("age")
    if age is not None:
        if isinstance(age, bool) or not isinstance(age, int) or not (0 <= age <= 120):
            raise ValueError("age")
    gender = d.get("gender")
    if gender is not None:
        gender = _txt(gender, 10, "gender")
    blood = d.get("blood_type")
    if blood is not None:
        if blood not in BLOOD_TYPES:
            raise ValueError("blood_type")
    hosp = d.get("hospital")
    if hosp is not None:
        if isinstance(hosp, str):
            hosp = {"name": hosp}
        if not isinstance(hosp, dict):
            raise ValueError("hospital")
        name = _txt(hosp["name"], 80, "hospital") if hosp.get("name") else None
        dept = _txt(hosp["department"], 60, "hospital") if hosp.get("department") else None
        hosp = {"name": name, "department": dept} if (name or dept) else None
    return {
        "name": _txt(d.get("name"), 60, "name"),
        "age": age,
        "gender": gender,
        "blood_type": blood,
        "conditions": _list(d.get("conditions"), "conditions"),
        "medications": _list(d.get("medications"), "medications"),
        "allergies": _list(d.get("allergies"), "allergies"),
        "hospital": hosp,
    }


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# ─────────────────────────────────────────
# API
# ─────────────────────────────────────────
@bp.route("/api/qr-share", methods=["POST"])
def create_share():
    wait = _limited("create", 20, 600)
    if wait:
        return _api({"error": "rate_limited"}, 429, {"Retry-After": str(wait)})
    ok, err = _bearer_ok("QR_CREATOR_TOKENS")
    if not ok:
        return err
    if (request.content_length or 0) > MAX_BODY:
        return _api({"error": "too_large"}, 413)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _api({"error": "invalid_json"}, 400)
    try:
        snapshot = clean_snapshot(body.get("patient"))
    except (ValueError, KeyError) as e:
        return _api({"error": "invalid_patient", "field": str(e)}, 400)

    ttl = body.get("ttl_minutes")
    ttl = _default_ttl() if ttl is None else ttl * 60 if isinstance(ttl, int) and not isinstance(ttl, bool) else None
    if ttl is None:
        return _api({"error": "invalid_ttl"}, 400)
    ttl = max(MIN_TTL, min(MAX_TTL, ttl))

    token = secrets.token_urlsafe(32)
    revoke_key = secrets.token_urlsafe(24)
    now = int(time.time())
    with _db() as conn:
        _purge(conn, now)
        conn.execute(
            "INSERT INTO shares (id, revoke_hash, snapshot, demo, created_at, expires_at) VALUES (?,?,?,?,?,?)",
            (_sha(token), _sha(revoke_key), json.dumps(snapshot, ensure_ascii=False),
             1 if _mode() == "demo" else 0, now, now + ttl),
        )
    return _api(
        {
            "token": token,                  # 응답으로만 전달, 서버에는 해시만 남음
            "revoke_key": revoke_key,        # 폐기용 비밀값(생성한 앱만 보관)
            "view_path": "/share",
            "created_at": _iso(now),
            "expires_at": _iso(now + ttl),
            "expires_in": ttl,
            "demo": _mode() == "demo",
        },
        201,
    )


def _token_from_request():
    t = request.headers.get("X-Share-Token", "").strip()
    return t if TOKEN_RE.match(t) else None


@bp.route("/api/qr-share", methods=["GET"])
def view_share():
    wait = _limited("view", 60, 60)
    if wait:
        return _api({"error": "rate_limited"}, 429, {"Retry-After": str(wait)})
    ok, err = _bearer_ok("QR_VIEWER_TOKENS")
    if not ok:
        return err
    token = _token_from_request()
    if not token:
        return _api({"error": "invalid_link"}, 400)
    now = int(time.time())
    with _db() as conn:
        row = conn.execute("SELECT * FROM shares WHERE id=?", (_sha(token),)).fetchone()
        if row is None:
            return _api({"error": "invalid_link"}, 404)       # 존재하지 않음 = 잘못된 링크
        if row["revoked_at"] is not None:
            _purge(conn, now)
            return _api({"error": "revoked", "revoked_at": _iso(row["revoked_at"])}, 410)
        if row["expires_at"] <= now:
            _purge(conn, now)
            return _api({"error": "expired", "expired_at": _iso(row["expires_at"])}, 410)
        patient = json.loads(row["snapshot"])
    return _api(
        {
            "patient": patient,
            "created_at": _iso(row["created_at"]),
            "expires_at": _iso(row["expires_at"]),
            "server_time": _iso(now),
            "demo": bool(row["demo"]),
        }
    )


@bp.route("/api/qr-share", methods=["DELETE"])
def revoke_share():
    wait = _limited("revoke", 60, 60)
    if wait:
        return _api({"error": "rate_limited"}, 429, {"Retry-After": str(wait)})
    token = _token_from_request()
    key = request.headers.get("X-Revoke-Key", "").strip()
    if not token or not key or len(key) > 128:
        return _api({"error": "invalid_request"}, 400)
    now = int(time.time())
    with _db() as conn:
        row = conn.execute("SELECT revoke_hash, revoked_at FROM shares WHERE id=?", (_sha(token),)).fetchone()
        if row is None or not hmac.compare_digest(row["revoke_hash"], _sha(key)):
            return _api({"error": "not_found"}, 404)          # 토큰 존재 여부를 구분해 알려주지 않음
        if row["revoked_at"] is None:
            conn.execute("UPDATE shares SET revoked_at=?, snapshot=NULL WHERE id=?", (now, _sha(token)))
    return _api({"status": "revoked"})


# ─────────────────────────────────────────
# 모바일 조회 페이지 (정적 파일, 토큰은 fragment로만 전달)
# ─────────────────────────────────────────
@bp.route("/share")
def share_page():
    return send_from_directory(STATIC_DIR, "view.html", mimetype="text/html")


@bp.route("/share/view.js")
def share_js():
    return send_from_directory(STATIC_DIR, "view.js", mimetype="application/javascript")


@bp.route("/share/view.css")
def share_css():
    return send_from_directory(STATIC_DIR, "view.css", mimetype="text/css")
