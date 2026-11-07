"""Shared Codebusters workspace: PDF import, saved questions, and live answers.

Run from this directory with ``uvicorn server:app --host 0.0.0.0 --port 8000``.
Set CODEBUSTERS_DATA_DIR to a persistent directory when deploying the app.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import fitz
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CODEBUSTERS_DATA_DIR", ROOT / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
WEB_DIR = ROOT / "web"
DB_PATH = DATA_DIR / "codebusters.sqlite3"
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 150
MAX_NODES_PER_QUESTION = 50_000
MAX_ANSWER_CHARACTERS = 10_000
MAX_POSITION_DEPTH = 256
MAX_WS_BYTES = 2 * 1024 * 1024
MAX_WORK_CELLS = 5_000
MAX_LAYOUT_BYTES = 120_000
CipherKind = Literal["aristocrat", "patristocrat", "xenocrypt", "baconian", "nihilist", "checkerboard", "cryptarithm", "fractionated_morse", "generic"]
SESSION_SECONDS = 30 * 24 * 60 * 60
JOIN_WINDOW_SECONDS = 10 * 60
JOIN_MAX_FAILURES = 8
ALLOWED_ORIGINS = [origin.strip().rstrip("/") for origin in
                   os.environ.get("CODEBUSTERS_ALLOWED_ORIGINS", "").split(",") if origin.strip()]
_DUMMY_PASSWORD_SALT = secrets.token_bytes(16)
_join_failures: dict[tuple[str, str], deque[float]] = {}
_join_lock = threading.Lock()
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _init_db() -> None:
    with closing(_connect()) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                pdf_name TEXT,
                pdf_path TEXT,
                page_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                number INTEGER NOT NULL,
                page INTEGER NOT NULL,
                prompt TEXT NOT NULL DEFAULT '',
                cipher TEXT NOT NULL DEFAULT '',
                points INTEGER NOT NULL DEFAULT 0,
                checked INTEGER NOT NULL DEFAULT 0,
                answer_nodes TEXT NOT NULL DEFAULT '[]',
                cipher_kind TEXT NOT NULL DEFAULT 'generic',
                cipher_layout TEXT NOT NULL DEFAULT '{}',
                work_cells TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS questions_room_order
            ON questions(room_id, number, created_at);
            CREATE TABLE IF NOT EXISTS room_sessions (
                token_hash TEXT PRIMARY KEY,
                room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS room_sessions_room
            ON room_sessions(room_id);
            """
        )
        # Databases created by the earlier local version did not have room
        # passwords. Existing rooms remain locked until recreated.
        room_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rooms)")}
        if "password_salt" not in room_columns:
            conn.execute("ALTER TABLE rooms ADD COLUMN password_salt BLOB")
        if "password_hash" not in room_columns:
            conn.execute("ALTER TABLE rooms ADD COLUMN password_hash BLOB")
        question_columns = {row["name"] for row in conn.execute("PRAGMA table_info(questions)")}
        if "cipher_kind" not in question_columns:
            conn.execute("ALTER TABLE questions ADD COLUMN cipher_kind TEXT NOT NULL DEFAULT 'generic'")
        if "cipher_layout" not in question_columns:
            conn.execute("ALTER TABLE questions ADD COLUMN cipher_layout TEXT NOT NULL DEFAULT '{}'")
        if "work_cells" not in question_columns:
            conn.execute("ALTER TABLE questions ADD COLUMN work_cells TEXT NOT NULL DEFAULT '{}'")
        conn.commit()


_init_db()


def _visible_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (node for node in nodes if not node["deleted"] and node["pos"] and node["c"]),
        key=lambda node: (tuple(node["pos"]), node["id"]),
    )


def _question_from_row(row: sqlite3.Row) -> dict[str, Any]:
    nodes = json.loads(row["answer_nodes"])
    kind = row["cipher_kind"] or "generic"
    layout = json.loads(row["cipher_layout"] or "{}")
    if not layout or "lines" not in layout or "tables" not in layout:
        kind, layout = _infer_layout_from_text(row["prompt"], row["cipher"], kind)
    return {
        "id": row["id"],
        "number": row["number"],
        "page": row["page"],
        "prompt": row["prompt"],
        "cipher": row["cipher"],
        "cipher_kind": kind,
        "cipher_layout": layout,
        "work_cells": json.loads(row["work_cells"] or "{}"),
        "points": row["points"],
        "checked": bool(row["checked"]),
        "answer_nodes": nodes,
        "answer": "".join(node["c"] for node in _visible_nodes(nodes)),
    }


def _layout_needs_refresh(row: sqlite3.Row) -> bool:
    try:
        layout = json.loads(row["cipher_layout"] or "{}")
    except (TypeError, ValueError):
        return True
    if not isinstance(layout, dict) or not isinstance(layout.get("lines"), list):
        return True
    kind = row["cipher_kind"] or "generic"
    if kind == "generic" and (_cipher_kind(row["prompt"] + " " + row["cipher"][:250]) != "generic"
                              or (not layout["lines"] and bool(row["cipher"].strip()))):
        return True
    tables = layout.get("tables")
    if not isinstance(tables, list):
        return True
    expected = {"aristocrat": "frequency", "patristocrat": "frequency", "xenocrypt": "frequency",
                "baconian": "baconian", "nihilist": "keyed_square", "checkerboard": "checkerboard",
                "fractionated_morse": "morse_fraction", "cryptarithm": "cryptarithm"}.get(kind)
    if expected is None:
        return False
    chart = next((table for table in tables if isinstance(table, dict) and table.get("type") == expected), None)
    if not chart:
        return True
    if kind in ("aristocrat", "patristocrat", "xenocrypt", "baconian"):
        headers = chart.get("headers", [])
        chart_rows = chart.get("rows", [])
        if (not isinstance(headers, list) or not isinstance(chart_rows, list)
                or any(not isinstance(item, dict) or not isinstance(item.get("values"), list)
                       for item in chart_rows)):
            return True
        row_labels = [item.get("label") for item in chart_rows]
        key_label = _key_label(row["prompt"] + " " + str(chart.get("header_label", "")), [])
        expected_labels = (["Replacement", "K2", "Frequency"] if key_label == "K2" and kind != "baconian" else
                           [key_label or "Alphabet", "Baconian" if kind == "baconian" else "Frequency", "Replacement"])
        target_headers = 27 if kind == "xenocrypt" else 26
        if (len(headers) not in (24, 26, 27) or (kind != "baconian" and len(headers) != target_headers)
                or row_labels != expected_labels or any(len(item["values"]) != len(headers)
                                                      for item in chart_rows)):
            return True
        if kind == "xenocrypt" and "Ñ" not in headers:
            return True
    elif kind == "nihilist":
        if (chart.get("column_headers") != list("12345") or chart.get("row_headers") != list("12345")
                or not isinstance(chart.get("values"), list) or len(chart["values"]) != 5
                or any(not isinstance(values, list) or len(values) != 5 for values in chart["values"])):
            return True
    elif kind == "checkerboard":
        if chart.get("values") != [[""] * 6 for _ in range(6)]:
            return True
    elif kind == "cryptarithm":
        if chart.get("digits") != list("0123456789") or chart.get("letters") != list("ABCDEFGHIJ"):
            return True
        if chart.get("formula_rows") and not layout["lines"]:
            return True
    elif kind == "fractionated_morse":
        if chart.get("fractions") != _morse_chart()["fractions"] or len(chart.get("replacement", [])) != 26:
            return True
    return False


def _remap_worksheet_cells(cells: dict[str, str], old_layout: dict[str, Any],
                           new_layout: dict[str, Any]) -> dict[str, str]:
    """Move replacement entries with their letter when a chart changes order."""
    old_tables, new_tables = old_layout.get("tables", []), new_layout.get("tables", [])
    if not isinstance(old_tables, list) or not isinstance(new_tables, list):
        return cells
    updates: dict[str, str] = {}
    for table_index, old in enumerate(old_tables):
        if (table_index >= len(new_tables) or not isinstance(old, dict)
                or old.get("type") not in ("frequency", "baconian")):
            continue
        new = new_tables[table_index]
        if not isinstance(new, dict) or new.get("type") != old["type"]:
            continue
        old_rows, new_rows = old.get("rows", []), new.get("rows", [])
        old_index = next((i for i, item in enumerate(old_rows) if item.get("label") == "Replacement"), None)
        new_index = next((i for i, item in enumerate(new_rows) if item.get("label") == "Replacement"), None)
        if old_index is None or new_index is None:
            continue
        old_headers, new_headers = old.get("headers", []), new.get("headers", [])
        for old_col, letter in enumerate(old_headers):
            old_key = f"table:{table_index}:row:{old_index}:col:{old_col}"
            if old_key not in cells:
                continue
            new_col = new_headers.index(letter) if letter in new_headers else old_col
            updates[old_key] = f"table:{table_index}:row:{new_index}:col:{new_col}"
    if not updates:
        return cells
    return {updates.get(key, key): value for key, value in cells.items()}


def _upgrade_existing_layouts(conn: sqlite3.Connection, room: sqlite3.Row,
                              rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Rebuild outdated worksheet formats, retaining answers and cell entries."""
    old_rows = [row for row in rows if _layout_needs_refresh(row)]
    if not old_rows:
        return rows
    document = None
    try:
        if room["pdf_path"]:
            document = fitz.open(room["pdf_path"])
    except (OSError, RuntimeError, ValueError):
        pass  # A text fallback can still restore the requested blank grids.
    try:
        for row in old_rows:
            try:
                old_layout = json.loads(row["cipher_layout"] or "{}")
            except (TypeError, ValueError):
                old_layout = {}
            if not isinstance(old_layout, dict):
                old_layout = {}
            prompt = row["prompt"]
            old_table = next((item for item in old_layout.get("tables", [])
                              if isinstance(item, dict) and item.get("type") in ("frequency", "baconian")), {})
            old_label = str(old_table.get("header_label", ""))
            key_context = prompt + " " + old_label
            detected_kind = _cipher_kind(prompt + " " + row["cipher"][:300])
            kind = row["cipher_kind"] if row["cipher_kind"] != "generic" else detected_kind
            if kind == "cryptarithm" and "?" in prompt:
                prompt = prompt.split("?", 1)[0] + "?"
            layout = None
            if document is not None and 0 < row["page"] <= document.page_count:
                page = document[row["page"] - 1]
                headings = [(parsed, y) for text, y in _extract_positioned_lines(page)
                            if (parsed := _heading(text)) is not None]
                matched = next(((parsed, y) for parsed, y in headings if parsed[0] == row["number"]), None)
                if matched:
                    heading, start_y = matched
                    end_y = next((y - 3 for _parsed, y in headings if y > start_y + 5), page.rect.height * .95)
                    if kind == "generic":
                        kind = _cipher_kind(heading[2] + " " + " ".join(
                            text for text, y in _extract_positioned_lines(page) if start_y <= y < end_y))
                    layout = _layout_from_page(page, start_y + 16, end_y, kind,
                                               key_context if old_label else prompt, row["cipher"])
            if layout is None:
                kind, layout = _infer_layout_from_text(key_context, row["cipher"], kind)
            try:
                cells = json.loads(row["work_cells"] or "{}")
                cells = _remap_worksheet_cells(cells, old_layout, layout)
                conn.execute("UPDATE questions SET cipher_kind = ?, cipher_layout = ?, prompt = ?, work_cells = ? "
                             "WHERE id = ? AND cipher_layout = ?", (
                                 kind, _serialize_layout(layout), prompt,
                                 json.dumps(cells, ensure_ascii=False, separators=(",", ":")),
                                 row["id"], row["cipher_layout"]))
            except (TypeError, ValueError):
                continue
        conn.commit()
        return conn.execute("SELECT * FROM questions WHERE room_id = ? ORDER BY number, created_at, id",
                            (room["id"],)).fetchall()
    finally:
        if document is not None:
            document.close()


def _room_state(room_id: str) -> dict[str, Any] | None:
    with closing(_connect()) as conn:
        room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if room is None:
            return None
        rows = conn.execute(
            "SELECT * FROM questions WHERE room_id = ? ORDER BY number, created_at, id",
            (room_id,),
        ).fetchall()
        rows = _upgrade_existing_layouts(conn, room, rows)
    questions = [_question_from_row(row) for row in rows]
    total = sum(q["points"] for q in questions)
    score = sum(q["points"] for q in questions if q["checked"])
    return {
        "id": room["id"],
        "title": room["title"],
        "pdf_name": room["pdf_name"],
        "has_pdf": bool(room["pdf_path"]),
        "page_count": room["page_count"],
        "questions": questions,
        "score": score,
        "total_points": total,
        "created_at": room["created_at"],
        "updated_at": room["updated_at"],
    }


def _require_room(room_id: str) -> dict[str, Any]:
    state = _room_state(room_id)
    if state is None:
        raise HTTPException(404, "Room not found. Check the invitation link.")
    return state


def _serialize_layout(layout: dict[str, Any]) -> str:
    if (not isinstance(layout.get("lines", []), list) or
            not isinstance(layout.get("tables", []), list) or
            len(layout.get("lines", [])) > 300 or len(layout.get("tables", [])) > 12):
        raise HTTPException(422, "Cipher layout has too many lines or tables.")
    encoded = json.dumps(layout, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_LAYOUT_BYTES:
        raise HTTPException(422, "Cipher layout is too large.")
    return encoded


class RoomCreate(BaseModel):
    title: str = Field(default="Untitled test", max_length=160)
    password: str = Field(min_length=8, max_length=128)


class RoomJoin(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class QuestionCreate(BaseModel):
    page: int = Field(default=1, ge=1, le=MAX_PDF_PAGES)
    prompt: str = Field(default="", max_length=20_000)
    cipher: str = Field(default="", max_length=100_000)
    points: int = Field(default=0, ge=0, le=100_000)
    cipher_kind: CipherKind = "generic"
    cipher_layout: dict[str, Any] = Field(default_factory=dict)


class QuestionPatch(BaseModel):
    page: int | None = Field(default=None, ge=1, le=MAX_PDF_PAGES)
    prompt: str | None = Field(default=None, max_length=20_000)
    cipher: str | None = Field(default=None, max_length=100_000)
    points: int | None = Field(default=None, ge=0, le=100_000)
    checked: bool | None = None
    cipher_kind: CipherKind | None = None
    cipher_layout: dict[str, Any] | None = None


app = FastAPI(title="Codebusters Together")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS + ["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
app.mount("/web", StaticFiles(directory=str(WEB_DIR), check_dir=False), name="web")


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _issue_token(conn: sqlite3.Connection, room_id: str) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO room_sessions (token_hash, room_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (hashlib.sha256(token.encode("ascii")).hexdigest(), room_id, time.time(), time.time() + SESSION_SECONDS),
    )
    return token


def _token_has_access(room_id: str, token: str | None) -> bool:
    if not isinstance(token, str) or not token or len(token) > 256:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT 1 FROM room_sessions WHERE room_id = ? AND token_hash = ? AND expires_at > ?",
            (room_id, digest, time.time()),
        ).fetchone()
    return row is not None


def _authorize_room(room_id: str, authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer ") or not _token_has_access(room_id, authorization[7:]):
        raise HTTPException(401, "Join this room with its password to continue.")


def _rate_limit_key(request: Request, room_id: str) -> tuple[str, str]:
    return (request.client.host if request.client else "unknown", room_id)


def _check_join_limit(key: tuple[str, str]) -> None:
    now = time.monotonic()
    with _join_lock:
        recent = _join_failures.get(key)
        if recent is not None:
            while recent and now - recent[0] >= JOIN_WINDOW_SECONDS:
                recent.popleft()
            if len(recent) >= JOIN_MAX_FAILURES:
                raise HTTPException(429, "Too many attempts. Try again in a few minutes.")


def _record_join_failure(key: tuple[str, str]) -> None:
    with _join_lock:
        if len(_join_failures) > 2000:
            _join_failures.clear()
        _join_failures.setdefault(key, deque()).append(time.monotonic())


def _socket_origin_allowed(socket: WebSocket) -> bool:
    origin = socket.headers.get("origin")
    if not origin:
        return True  # Non-browser clients authenticate with the same room token.
    host = socket.headers.get("host", "")
    # Reverse proxies may forward WSS as plain WS to Uvicorn. The browser's
    # Origin retains HTTPS, so accept either scheme for the exact host.
    return origin.rstrip("/") in (*ALLOWED_ORIGINS, f"http://{host}", f"https://{host}")


@app.get("/")
async def index() -> FileResponse:
    if not (WEB_DIR / "index.html").is_file():
        raise HTTPException(503, "The web app has not been installed yet.")
    return FileResponse(WEB_DIR / "index.html")


@app.get("/r/{room_id}")
async def legacy_room_link(room_id: str) -> RedirectResponse:
    return RedirectResponse(f"/?room={quote(room_id, safe='')}", status_code=302)


@app.get("/style.css")
async def stylesheet() -> FileResponse:
    return FileResponse(WEB_DIR / "style.css", media_type="text/css")


@app.get("/app.js")
async def script() -> FileResponse:
    return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")


@app.get("/config.js")
async def frontend_config() -> FileResponse:
    return FileResponse(WEB_DIR / "config.js", media_type="text/javascript")


@app.post("/api/rooms")
async def create_room(payload: RoomCreate) -> dict[str, Any]:
    room_id = secrets.token_urlsafe(18)
    now = time.time()
    salt = secrets.token_bytes(16)
    password_digest = await run_in_threadpool(_password_hash, payload.password, salt)
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO rooms (id, title, password_salt, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (room_id, payload.title.strip() or "Untitled test", salt, password_digest, now, now),
        )
        token = _issue_token(conn, room_id)
        conn.commit()
    return {"room": _require_room(room_id), "token": token}


@app.post("/api/rooms/{room_id}/join")
async def join_room(room_id: str, payload: RoomJoin, request: Request) -> dict[str, Any]:
    key = _rate_limit_key(request, room_id)
    _check_join_limit(key)
    with closing(_connect()) as conn:
        row = conn.execute("SELECT password_salt, password_hash FROM rooms WHERE id = ?", (room_id,)).fetchone()
    salt = row["password_salt"] if row and row["password_salt"] else _DUMMY_PASSWORD_SALT
    candidate = await run_in_threadpool(_password_hash, payload.password, salt)
    if not row or not row["password_hash"] or not hmac.compare_digest(candidate, row["password_hash"]):
        _record_join_failure(key)
        raise HTTPException(401, "Room or password is incorrect.")
    with closing(_connect()) as conn:
        token = _issue_token(conn, room_id)
        conn.commit()
    with _join_lock:
        _join_failures.pop(key, None)
    return {"room": _require_room(room_id), "token": token}


@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str, _auth: None = Depends(_authorize_room)) -> dict[str, Any]:
    return _require_room(room_id)


_HEADING = re.compile(r"^\s*(?:(?:Question|Problem)\s*#?\s*)?(\d{1,3})\s*[).:]\s*(.*)$", re.I)
_TIMED_HEADING = re.compile(r"^\s*Timed\s+Question\s*(.*)$", re.I)
_POINTS = re.compile(
    r"^\s*(?:\[\s*(\d{1,5})\s*(?:points?|pts?)?\s*\]|"
    r"\(\s*(\d{1,5})\s*(?:points?|pts?)\s*\)|"
    r"(\d{1,5})\s*(?:points?|pts?)\s*[:—–-]?)\s*(.*)$",
    re.I,
)
_HEADER = re.compile(
    r"^(?:test|test answer key|codebusters(?: practice test)?|science olympiad(?:\s*[—–-]\s*codebusters)?|"
    r"school\s*:\s*_*|page\s*\d+\s*(?:of\s*\d+)?|\d{1,2}/\d{1,2}/\d{2,4})$",
    re.I,
)
_CIPHER_TASK = re.compile(r"\b(?:decode|encode|decrypt|encrypt|cipher|cryptogram|cryptarithm|aristocrat|patristocrat|xenocrypt|checkerboard|baconian|nihilist)\b", re.I)
_PAGE_NOISE = re.compile(r"^(?:team\s*(?:number|name)\s*[:_]|(?:lasso|science olympiad).*(?:invitational|division)|page\s+\d+|\d{1,2}/\d{1,2}/\d{2,4}(?:,?\s+\d{1,2}:\d{2}\s*[AP]M)?)", re.I)
_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(pattern, re.I)) for kind, pattern in (
        ("cryptarithm", r"\bcryptarithm\b"),
        ("fractionated_morse", r"\bfractionated\s+morse\b"),
        ("patristocrat", r"\bpatristocrat\b"),
        ("aristocrat", r"\baristocrat\b"),
        ("xenocrypt", r"\bxenocrypt\b"),
        ("checkerboard", r"\bchecker\s*board\b"),
        ("baconian", r"\bbaconian\b"),
        ("nihilist", r"\bnihilist\b"),
    )
)


def _cipher_kind(text: str) -> str:
    return next((kind for kind, pattern in _KINDS if pattern.search(text)), "generic")


def _extract_positioned_lines(page: fitz.Page) -> list[tuple[str, float]]:
    """Read heading text and its vertical position without flattening the cipher."""
    lines: list[tuple[str, float]] = []
    for block in page.get_text("dict", sort=True)["blocks"]:
        if "lines" not in block:
            continue
        for line in block["lines"]:
            spans = sorted(line["spans"], key=lambda span: span["bbox"][0])
            text = "".join(span["text"] for span in spans).strip()
            if text and not _HEADER.fullmatch(text) and not _PAGE_NOISE.match(text):
                lines.append((text, float(line["bbox"][1])))
    return lines


def _extract_lines(page: fitz.Page) -> list[str]:
    return [text for text, _y in _extract_positioned_lines(page)]


def _visual_rows(page: fitz.Page, start_y: float, end_y: float) -> list[dict[str, Any]]:
    """Group words on their printed baselines, including individually drawn grid cells.

    PDF text order can read a table vertically down each column. Using bounding
    boxes lets us reconstruct the rows of cipher text and labeled worksheets.
    """
    words = []
    for x0, y0, x1, y1, word, *_ in page.get_text("words", sort=False):
        if y1 < start_y or y0 > end_y or not word.strip():
            continue
        words.append({"x": float(x0), "right": float(x1), "y": (y0 + y1) / 2,
                      "height": float(y1 - y0), "text": word.strip()})
    rows: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda item: (item["y"], item["x"])):
        row = min(rows, key=lambda item: abs(item["y"] - word["y"]), default=None)
        if row is None or abs(row["y"] - word["y"]) > max(3.2, word["height"] * .29):
            rows.append({"y": word["y"], "words": [word]})
        else:
            row["words"].append(word)
            row["y"] = sum(item["y"] for item in row["words"]) / len(row["words"])
    for row in rows:
        row["words"].sort(key=lambda item: item["x"])
        row["text"] = " ".join(item["text"] for item in row["words"])
    return sorted(rows, key=lambda item: item["y"])


def _row_cipher(row: dict[str, Any]) -> str | None:
    """Return a ciphertext row without treating prompts or table legends as one."""
    text = row["text"]
    if _PAGE_NOISE.match(text) or _HEADER.fullmatch(text):
        return None
    letters = [char for char in text if char.isalpha()]
    if len(letters) >= 6 and sum(char.isupper() for char in letters) / len(letters) >= .86:
        return text
    if len(text) >= 10 and len(re.findall(r"\b\d{2,3}\b", text)) >= 4:
        return text
    return None


def _table_row(rows: list[dict[str, Any]], pattern: str) -> dict[str, Any] | None:
    matcher = re.compile(pattern, re.I)
    return next((row for row in rows if matcher.search(row["text"])), None)


def _alphabet_headers(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Find a genuine ordered alphabet header, not a spaced ciphertext row."""
    words = [w for w in row["words"] if re.fullmatch(r"[A-ZÑ](?:/?[A-ZÑ])?", w["text"])]
    if not 24 <= len(words) <= 27:
        return []
    alphabet = "ABCDEFGHIJKLMNÑOPQRSTUVWXYZ"
    indices = [alphabet.find(w["text"][0]) for w in words]
    # Baconian keys sometimes combine J with I and V with U; count the
    # remaining alphabet in order without treating arbitrary cipher text as a key.
    if -1 in indices or indices != sorted(set(indices)) or indices[0] != 0 or indices[-1] != 26:
        return []
    return words


def _key_label(prompt: str, rows: list[dict[str, Any]]) -> str | None:
    label = re.search(r"\bK\s*[-–]?\s*([123])\b", prompt, re.I)
    if label:
        return "K" + label.group(1)
    for row in rows:
        label = re.search(r"\bK\s*[-–]?\s*([123])\b", row["text"], re.I)
        if label:
            return "K" + label.group(1)
    return None


def _alphabet_table(rows: list[dict[str, Any]], kind: str, cipher_lines: list[str],
                    prompt: str = "") -> dict[str, Any]:
    alphabet = list("ABCDEFGHIJKLMNÑOPQRSTUVWXYZ" if kind == "xenocrypt" else "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    header = next((row for row in rows if _alphabet_headers(row)), None)
    header_words = _alphabet_headers(header) if header else []
    # A genuine Baconian reference may combine I/J and U/V into 24 columns.
    if len(header_words) >= 24:
        headers = [word["text"] for word in header_words]
        # Some PDFs put the table's row label in the same typography as the header.
        if (not (len(headers) <= 27 and len(set(headers)) >= 24)
                or (kind == "xenocrypt" and (len(headers) != 27 or "Ñ" not in headers))
                or (kind in ("aristocrat", "patristocrat") and len(headers) != 26)):
            headers = alphabet
    else:
        headers = alphabet
    value_label = "Frequency" if kind != "baconian" else "Baconian"
    value_row = _table_row(rows, r"\bFrequency\b" if kind != "baconian" else r"\b(?:Baconian|Pattern|Code)\b")
    values: list[str] = []
    if value_row and header_words and len(header_words) == len(headers):
        candidates = [w for w in value_row["words"] if
                      re.fullmatch(r"\d+|[ABab]{5}", w["text"])]
        for word in header_words:
            match = min(candidates, key=lambda item: abs((item["x"] + item["right"]) / 2 -
                                 (word["x"] + word["right"]) / 2), default=None)
            values.append(match["text"] if match and abs(match["x"] - word["x"]) < 13 else "")
    if not any(values):
        if kind != "baconian":
            cipher_text = "".join(cipher_lines).upper()
            values = [str(cipher_text.count(letter)) for letter in headers]
        else:
            values = ["".join("B" if i & (1 << bit) else "A" for bit in (4, 3, 2, 1, 0))
                      for i in range(len(headers))]
    label = next((word["text"] for word in header["words"] if header_words and
                  word["x"] < header_words[0]["x"] - 4 and
                  re.fullmatch(r"(?:Alphabet|K\d+)", word["text"], re.I)), "Alphabet") if header else "Alphabet"
    label = _key_label(prompt, rows) or label
    if kind != "baconian":
        # Counts must track the actual ciphertext even when a PDF's printed
        # frequency row is missing or has been extracted in column order.
        if cipher_lines:
            cipher_text = "".join(cipher_lines).upper()
            values = [str(cipher_text.count(letter)) for letter in headers]
        if label == "K2":
            table_rows = [{"label": "Replacement", "values": [""] * len(headers)},
                          {"label": "K2", "values": headers.copy()},
                          {"label": "Frequency", "values": values}]
        else:
            table_rows = [{"label": label, "values": headers.copy()},
                          {"label": "Frequency", "values": values},
                          {"label": "Replacement", "values": [""] * len(headers)}]
    else:
        table_rows = [{"label": label, "values": headers.copy()},
                      {"label": value_label, "values": values},
                      {"label": "Replacement", "values": [""] * len(headers)}]
    return {"type": "baconian" if kind == "baconian" else "frequency", "headers": headers,
            "header_label": label, "rows": table_rows}


def _keyed_square(rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = list("12345")
    header = next((row for row in rows if len([w for w in row["words"] if
        w["text"] in labels]) >= 5), None)
    values = [[""] * 5 for _ in range(5)]
    if header:
        header_words = [w for w in header["words"] if w["text"] in labels][-5:]
        for row in rows:
            if row["y"] <= header["y"] + 6:
                continue
            label_word = next((w for w in row["words"] if w["text"] in labels and
                w["x"] < header_words[0]["x"] - 5), None)
            if label_word is None:
                continue
            row_index = labels.index(label_word["text"])
            for col, column in enumerate(header_words):
                matching = next((w for w in row["words"] if w is not label_word and
                                 abs(w["x"] - column["x"]) < 13 and
                                 re.fullmatch(r"[A-Za-z]", w["text"])), None)
                if matching:
                    values[row_index][col] = matching["text"].upper()
    return {"type": "keyed_square", "column_headers": labels, "row_headers": labels,
            "values": values}


def _checkerboard() -> dict[str, Any]:
    return {"type": "checkerboard", "values": [[""] * 6 for _ in range(6)]}


def _inline_formula(text: str) -> list[str]:
    compact = re.sub(r"\s+", "", text.upper())
    match = re.fullmatch(r"([A-Z]{2,18}(?:\+[A-Z]{2,18}){1,10})=([A-Z]{2,18})", compact)
    if not match:
        return []
    operands = match.group(1).split("+")
    return [operands[0], *("+" + operand for operand in operands[1:]), match.group(2)]


def _formula_from_fragments(fragments: list[str]) -> list[str]:
    """Assemble all addends before the final result, even with detached + glyphs."""
    words: list[str] = []
    for fragment in fragments:
        inline = _inline_formula(fragment)
        if inline:
            return inline
        compact = re.sub(r"[^A-Z+]", "", fragment.upper())
        if compact == "+":
            continue
        word = compact.lstrip("+")
        if re.fullmatch(r"[A-Z]{2,18}", word):
            words.append(word)
        if len(words) >= 12:
            break
    if len(words) < 3:
        return []
    return [words[0], *("+" + word for word in words[1:-1]), words[-1]]


def _cryptarithm(rows: list[dict[str, Any]], prompt: str, page_width: float) -> dict[str, Any]:
    left = [row for row in rows if row["words"] and row["words"][0]["x"] < page_width * .52]
    digits = re.search(r"\b(?:values?|digits?)\s*(\d{5,12})\b", prompt, re.I)
    decode_digits = digits.group(1) if digits else ""
    decode_heading = _table_row(left, r"Values\s+to\s+decode|decode\s+for\s+solution")
    if decode_heading:
        for row in left:
            if not decode_heading["y"] < row["y"] < decode_heading["y"] + 65:
                continue
            printable = [w["text"] for w in row["words"] if w["x"] < page_width * .52]
            candidate = "".join(printable)
            if re.fullmatch(r"\d{5,12}", candidate):
                decode_digits = candidate
                break
    formula_label = _table_row(left, r"Cryptarithm\s+formula|(?:the\s+)?formula")
    formula_rows: list[str] = []
    if formula_label:
        heading_edge = max((w["right"] for w in formula_label["words"] if
                            w["x"] < page_width * .42), default=page_width * .32)
        formula_edge = min(page_width * .52, max(page_width * .42, heading_edge + 65))
        fragments: list[str] = []
        last_word_y: float | None = None
        word_count = 0
        for row in left:
            if not formula_label["y"] + 5 < row["y"] < formula_label["y"] + 380:
                continue
            pieces = [w["text"] for w in row["words"] if w["x"] < formula_edge]
            fragment = " ".join(pieces)
            if fragment and not _PAGE_NOISE.match(fragment):
                word = re.sub(r"[^A-Z]", "", fragment.upper())
                if (re.fullmatch(r"[A-Z]{2,18}", word) and
                        re.sub(r"[^A-Z+]", "", fragment.upper()).lstrip("+") == word):
                    if word_count >= 3 and last_word_y is not None and row["y"] - last_word_y > 85:
                        break  # A footer or next section starts well below the arithmetic.
                    last_word_y = row["y"]
                    word_count += 1
                fragments.append(fragment)
        formula_rows = _formula_from_fragments(fragments)
    if not formula_rows:
        formula_rows = next((formula for row in left if
                             (formula := _inline_formula(row["text"]))), [])
    # This work area uses editable A–J row placeholders and 0–9 columns,
    # independent of the letters or digits appearing in a particular puzzle.
    return {"type": "cryptarithm", "decode_digits": decode_digits,
            "formula_rows": formula_rows, "digits": list("0123456789"),
            "letters": list("ABCDEFGHIJ")}


def _morse_chart() -> dict[str, Any]:
    fractions = ["".join(triple) for triple in itertools.product(".-x", repeat=3) if triple != ("x", "x", "x")]
    return {"type": "morse_fraction", "columns": [str(i) for i in range(1, 27)],
            "fractions": fractions, "replacement": [""] * 26}


def _layout_from_page(page: fitz.Page, start_y: float, end_y: float, kind: str,
                      prompt: str, cipher: str) -> dict[str, Any]:
    rows = _visual_rows(page, start_y, end_y)
    if kind == "generic":
        return {"lines": [line for line in cipher.splitlines() if line.strip()], "tables": []}
    if kind == "cryptarithm":
        chart = _cryptarithm(rows, prompt, page.rect.width)
        formula_compact = {re.sub(r"[^A-Z]", "", formula.upper()) for formula in chart["formula_rows"]}
        formula_compact.add("".join(re.sub(r"[^A-Z]", "", formula.upper()) for formula in chart["formula_rows"]))
        lines = [text for row in rows if (text := _row_cipher(row)) and not re.search(
            r"\b(?:replacement|frequency|cryptarithm formula|values to decode)\b", text, re.I)
            and re.sub(r"[^A-Z]", "", text.upper()) not in formula_compact]
        for formula in chart["formula_rows"]:
            lines.append(formula)
        return {"lines": lines, "tables": [chart]}
    table_start = next((row["y"] for row in rows if re.search(
        r"\b(?:Frequency|Replacement|Morse\s+fraction)\b", row["text"], re.I)), end_y)
    if kind in ("aristocrat", "patristocrat", "xenocrypt", "baconian"):
        alpha_start = next((row["y"] for row in rows if _alphabet_headers(row)), end_y)
        table_start = min(table_start, alpha_start)
    if kind == "nihilist":
        numbered_header = next((row["y"] for row in rows if len([w for w in row["words"] if
            w["text"] in "12345" and len(w["text"]) == 1]) >= 5), end_y)
        table_start = min(table_start, numbered_header)
    if kind in ("baconian", "xenocrypt"):
        lines = [row["text"] for row in rows if row["y"] + 3 < table_start
                 and len(row["text"]) >= 6 and not _PAGE_NOISE.match(row["text"])
                 and not re.match(r"^\s*Answer\s*:", row["text"], re.I)
                 and not (row["y"] < start_y + 25 and not _row_cipher(row))
                 and not re.match(r"^(?:Decode|Encode|The following|This quote|For this question|What|A quote|Descifra|Decodifica|Las siguientes|Se (?:sabe|dice))\b",
                                  row["text"], re.I)]
    else:
        lines = [text for row in rows if row["y"] + 3 < table_start
                 if (text := _row_cipher(row))]
    if not lines:
        lines = [line for line in cipher.splitlines() if _row_cipher({"text": line})]
    tables: list[dict[str, Any]] = []
    if kind in ("aristocrat", "patristocrat", "xenocrypt", "baconian"):
        tables.append(_alphabet_table(rows, kind, lines, prompt))
    elif kind == "nihilist":
        tables.append(_keyed_square(rows))
    elif kind == "checkerboard":
        tables.append(_checkerboard())
    elif kind == "fractionated_morse":
        tables.append(_morse_chart())
    return {"lines": lines, "tables": tables}


def _infer_layout_from_text(prompt: str, cipher: str, kind: str = "generic") -> tuple[str, dict[str, Any]]:
    kind = kind if kind != "generic" else _cipher_kind(prompt)
    lines = [line.strip() for line in cipher.splitlines() if line.strip() and not _PAGE_NOISE.match(line)]
    tables: list[dict[str, Any]] = []
    if kind in ("aristocrat", "patristocrat", "xenocrypt", "baconian"):
        tables = [_alphabet_table([], kind, lines, prompt)]
    elif kind == "nihilist":
        tables = [_keyed_square([])]
    elif kind == "checkerboard":
        tables = [_checkerboard()]
    elif kind == "cryptarithm":
        crypt = _cryptarithm([], prompt, 600)
        original_lines = lines
        label_at = next((i for i, line in enumerate(original_lines) if re.search(r"cryptarithm\s+formula", line, re.I)), -1)
        crypt["formula_rows"] = _formula_from_fragments(original_lines[label_at + 1:])
        tables = [crypt]
        formula_words = {re.sub(r"[^A-Z]", "", formula.upper()) for formula in crypt["formula_rows"]}
        formula_words.add("".join(re.sub(r"[^A-Z]", "", formula.upper()) for formula in crypt["formula_rows"]))
        lines = [line for line in original_lines if _row_cipher({"text": line}) and
                 not re.search(r"\b(?:replacement|frequency|cryptarithm formula|values to decode)\b", line, re.I)
                 and re.sub(r"[^A-Z]", "", line.upper()) not in formula_words]
        for formula in crypt["formula_rows"]:
            lines.append(formula)
    elif kind == "fractionated_morse":
        tables = [_morse_chart()]
    return kind, {"lines": lines, "tables": tables}


def _heading(line: str) -> tuple[int, int, str] | None:
    timed = _TIMED_HEADING.match(line)
    if timed:
        points = _POINTS.match(timed.group(1))
        if points:
            return 0, int(next(value for value in points.groups()[:3] if value is not None)), "Timed question. " + points.group(4).strip()
    matched = _HEADING.match(line)
    if not matched:
        return None
    number = int(matched.group(1))
    if number > 150:
        return None
    rest = matched.group(2).strip()
    points = _POINTS.match(rest)
    if points:
        return number, int(next(value for value in points.groups()[:3] if value is not None)), points.group(4).strip()
    if _CIPHER_TASK.search(rest[:150]):
        return number, 0, rest
    return None


def _looks_like_cipher(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    if len(letters) >= 9 and sum(c.isupper() for c in letters) / len(letters) > 0.76:
        return True
    digits = sum(c.isdigit() for c in line)
    return len(line) >= 12 and digits >= 8 and digits / len(line) > 0.3


def _split_question(number: int, points: int, heading_text: str, body: list[str], page: int) -> dict[str, Any]:
    prompt_lines = [heading_text] if heading_text else []
    cipher_lines: list[str] = []
    started_cipher = False
    for line in body:
        if _looks_like_cipher(line):
            started_cipher = True
        if started_cipher:
            cipher_lines.append(line)
        elif len(prompt_lines) < 4 and len(" ".join(prompt_lines)) < 450:
            prompt_lines.append(line)
        else:
            cipher_lines.append(line)
    if not cipher_lines and len(prompt_lines) > 1:
        cipher_lines = prompt_lines[1:]
        prompt_lines = prompt_lines[:1]
    kind = _cipher_kind(heading_text)
    if kind == "generic":
        kind = _cipher_kind(" ".join(body[:35]))
    prompt = " ".join(prompt_lines).strip() or f"Question {number}"
    if kind == "cryptarithm":
        # Some PDFs place the first mapping-grid digits on the same physical
        # baseline as the printed question, so text-order parsing joins them.
        prompt = heading_text.split("?", 1)[0].strip() + ("?" if "?" in heading_text else "")
    return {
        "number": number,
        "page": page,
        "prompt": prompt,
        "cipher": "\n".join(cipher_lines).strip(),
        "points": points,
        "cipher_kind": kind,
    }


def _parse_pdf(data: bytes) -> tuple[int, list[dict[str, Any]]]:
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("This file could not be read as a PDF.") from exc
    with document:
        if document.needs_pass:
            raise ValueError("This PDF is password protected. Upload an unlocked copy.")
        count = document.page_count
        if count < 1 or count > MAX_PDF_PAGES:
            raise ValueError(f"The PDF must have 1 to {MAX_PDF_PAGES} pages.")
        found: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        body: list[str] = []
        scanned_pages: list[int] = []

        def finish(end_y: float | None = None) -> None:
            if current is None:
                return
            question = _split_question(current["number"], current["points"], current["heading"], body, current["page"])
            page = document[current["page"] - 1]
            layout = _layout_from_page(page, current["heading_y"] + 16,
                                       end_y if end_y is not None else page.rect.height * .95,
                                       question["cipher_kind"], question["prompt"], question["cipher"])
            question["cipher_layout"] = layout
            if question["cipher_kind"] != "generic":
                # The printable worksheet's grids belong to structured layout,
                # never to the unstructured ciphertext displayed above it.
                question["cipher"] = "\n".join(layout["lines"])
            found.append(question)

        for page_number in range(1, count + 1):
            lines = _extract_positioned_lines(document[page_number - 1])
            if not lines:
                scanned_pages.append(page_number)
            for line, position_y in lines:
                heading = _heading(line)
                if heading:
                    if current is not None:
                        finish(position_y - 3 if current["page"] == page_number else None)
                    current = {"number": heading[0], "points": heading[1],
                               "heading": heading[2], "heading_y": position_y, "page": page_number}
                    body = []
                elif current is not None:
                    body.append(line)
        if current is not None:
            finish()
        if not found:
            # Scans and unusual layouts remain fully usable through the page image.
            return count, [
                {"number": n, "page": n, "prompt": f"Page {n} — add question details", "cipher": "", "points": 0,
                 "cipher_kind": "generic", "cipher_layout": {"lines": [], "tables": []}}
                for n in range(1, count + 1)
            ]
        for page_number in scanned_pages:
            if not any(q["page"] == page_number for q in found):
                found.append({"number": max(q["number"] for q in found) + 1, "page": page_number,
                              "prompt": f"Page {page_number} — add question details", "cipher": "", "points": 0,
                              "cipher_kind": "generic", "cipher_layout": {"lines": [], "tables": []}})
        return count, sorted(found, key=lambda q: (q["page"], q["number"]))[:500]


class RoomConnections:
    def __init__(self) -> None:
        self.clients: dict[WebSocket, str] = {}
        self.lock = asyncio.Lock()

    async def broadcast(self, event: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for client in list(self.clients):
            try:
                await client.send_json(event)
            except Exception:
                dead.append(client)
        for client in dead:
            self.clients.pop(client, None)

    def presence(self) -> dict[str, Any]:
        return {"type": "presence", "count": len(self.clients), "users": list(self.clients.values())}


_rooms: dict[str, RoomConnections] = {}


def _connections(room_id: str) -> RoomConnections:
    return _rooms.setdefault(room_id, RoomConnections())


@app.post("/api/rooms/{room_id}/upload")
async def upload_pdf(room_id: str, file: UploadFile = File(...), _auth: None = Depends(_authorize_room)) -> dict[str, Any]:
    _require_room(room_id)
    data = await file.read(MAX_PDF_BYTES + 1)
    await file.close()
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "PDFs must be at most 25 MB.")
    if not data.lstrip().startswith(b"%PDF-"):
        raise HTTPException(422, "Choose a PDF file.")
    try:
        page_count, parsed = await run_in_threadpool(_parse_pdf, data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    connections = _connections(room_id)
    async with connections.lock:
        destination = UPLOAD_DIR / f"{room_id}.pdf"
        temporary = UPLOAD_DIR / f"{room_id}-{secrets.token_hex(8)}.part"
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
            now = time.time()
            with closing(_connect()) as conn:
                conn.execute("DELETE FROM questions WHERE room_id = ?", (room_id,))
                conn.execute(
                    "UPDATE rooms SET pdf_name = ?, pdf_path = ?, page_count = ?, updated_at = ? WHERE id = ?",
                    ((Path(file.filename or "test.pdf").name)[:255], str(destination), page_count, now, room_id),
                )
                for q in parsed:
                    conn.execute(
                        "INSERT INTO questions (id, room_id, number, page, prompt, cipher, points, cipher_kind, cipher_layout, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (secrets.token_urlsafe(12), room_id, q["number"], q["page"], q["prompt"], q["cipher"], q["points"],
                         q.get("cipher_kind", "generic"), _serialize_layout(q.get("cipher_layout", {})), now, now),
                    )
                conn.commit()
        finally:
            temporary.unlink(missing_ok=True)
        state = _require_room(room_id)
        await connections.broadcast({"type": "room", "room": state})
    return {"room": state}


@app.get("/api/rooms/{room_id}/pages/{page}")
async def page_image(room_id: str, page: int, _auth: None = Depends(_authorize_room)) -> Response:
    with closing(_connect()) as conn:
        room = conn.execute("SELECT pdf_path, page_count FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        raise HTTPException(404, "Room not found.")
    if not room["pdf_path"] or page < 1 or page > room["page_count"]:
        raise HTTPException(404, "Page not found.")

    def render() -> bytes:
        with fitz.open(room["pdf_path"]) as document:
            pixmap = document[page - 1].get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
            return pixmap.tobytes("png")

    try:
        png = await run_in_threadpool(render)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(503, "The PDF page is temporarily unavailable.") from exc
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@app.post("/api/rooms/{room_id}/questions")
async def add_question(room_id: str, payload: QuestionCreate, _auth: None = Depends(_authorize_room)) -> dict[str, Any]:
    connections = _connections(room_id)
    async with connections.lock:
        state = _require_room(room_id)
        if state["page_count"] and payload.page > state["page_count"]:
            raise HTTPException(422, "Page number is outside this PDF.")
        now = time.time()
        question_id = secrets.token_urlsafe(12)
        with closing(_connect()) as conn:
            next_number = conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM questions WHERE room_id = ?", (room_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO questions (id, room_id, number, page, prompt, cipher, points, cipher_kind, cipher_layout, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (question_id, room_id, next_number, payload.page, payload.prompt, payload.cipher, payload.points,
                 payload.cipher_kind, _serialize_layout(payload.cipher_layout), now, now),
            )
            conn.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, room_id))
            conn.commit()
            question = _question_from_row(conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone())
        await connections.broadcast({"type": "question_created", "question": question})
    return {"question": question}


@app.patch("/api/rooms/{room_id}/questions/{question_id}")
async def edit_question(room_id: str, question_id: str, payload: QuestionPatch, _auth: None = Depends(_authorize_room)) -> dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    if not changes or any(value is None for value in changes.values()):
        raise HTTPException(422, "Provide at least one question field with a value.")
    connections = _connections(room_id)
    async with connections.lock:
        state = _require_room(room_id)
        if "page" in changes and state["page_count"] and changes["page"] > state["page_count"]:
            raise HTTPException(422, "Page number is outside this PDF.")
        with closing(_connect()) as conn:
            row = conn.execute("SELECT * FROM questions WHERE id = ? AND room_id = ?", (question_id, room_id)).fetchone()
            if row is None:
                raise HTTPException(404, "Question not found.")
            if "prompt" in changes or "cipher" in changes or "cipher_kind" in changes:
                old_kind = row["cipher_kind"] or "generic"
                new_prompt = changes.get("prompt", row["prompt"])
                new_cipher = changes.get("cipher", row["cipher"])
                detected = _cipher_kind(new_prompt)
                new_kind = changes.get("cipher_kind") or (detected if detected != "generic" else old_kind)
                if "cipher_kind" not in changes and new_kind != old_kind:
                    changes["cipher_kind"] = new_kind
                if "cipher_layout" not in changes:
                    old_layout = json.loads(row["cipher_layout"] or "{}")
                    if new_kind != old_kind or not old_layout:
                        _kind, new_layout = _infer_layout_from_text(new_prompt, new_cipher, new_kind)
                    else:
                        new_layout = old_layout
                        if "cipher" in changes:
                            new_layout["lines"] = [line.strip() for line in new_cipher.splitlines() if line.strip()]
                        if new_kind == "cryptarithm" and ("prompt" in changes or "cipher" in changes):
                            _kind, inferred = _infer_layout_from_text(new_prompt, new_cipher, new_kind)
                            old_table = next((table for table in new_layout.get("tables", [])
                                              if table.get("type") == "cryptarithm"), None)
                            inferred_table = inferred["tables"][0]
                            if old_table:
                                if "prompt" in changes and inferred_table["decode_digits"]:
                                    old_table["decode_digits"] = inferred_table["decode_digits"]
                                if "cipher" in changes and inferred_table["formula_rows"]:
                                    old_table["formula_rows"] = inferred_table["formula_rows"]
                                    old_table["letters"] = inferred_table["letters"]
                                new_layout["lines"] = []
                    changes["cipher_layout"] = new_layout
            changes["updated_at"] = now = time.time()
            if "cipher_layout" in changes:
                changes["cipher_layout"] = _serialize_layout(changes["cipher_layout"])
            if "checked" in changes:
                changes["checked"] = int(changes["checked"])
            assignments = ", ".join(f"{column} = ?" for column in changes)
            conn.execute(f"UPDATE questions SET {assignments} WHERE id = ?", (*changes.values(), question_id))
            conn.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, room_id))
            conn.commit()
            question = _question_from_row(conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone())
        await connections.broadcast({"type": "question_updated", "question": question})
    return {"question": question}


@app.delete("/api/rooms/{room_id}/questions/{question_id}", status_code=204)
async def delete_question(room_id: str, question_id: str, _auth: None = Depends(_authorize_room)) -> Response:
    connections = _connections(room_id)
    async with connections.lock:
        _require_room(room_id)
        with closing(_connect()) as conn:
            cursor = conn.execute("DELETE FROM questions WHERE id = ? AND room_id = ?", (question_id, room_id))
            if cursor.rowcount != 1:
                raise HTTPException(404, "Question not found.")
            conn.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (time.time(), room_id))
            conn.commit()
        await connections.broadcast({"type": "question_deleted", "question_id": question_id})
    return Response(status_code=204)


def _valid_id(value: Any, max_length: int = 120) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= max_length and not any(ord(c) < 33 for c in value)


def _merge_answer_ops(room_id: str, event: dict[str, Any]) -> dict[str, Any]:
    question_id = event.get("question_id")
    op_id = event.get("op_id")
    inserts = event.get("inserts", [])
    deletes = event.get("deletes", [])
    if not _valid_id(question_id) or not _valid_id(op_id) or not isinstance(inserts, list) or not isinstance(deletes, list):
        raise ValueError("Invalid answer update.")
    if len(inserts) + len(deletes) > 1000:
        raise ValueError("Answer update is too large.")
    cleaned: list[dict[str, Any]] = []
    for node in inserts:
        if not isinstance(node, dict) or not _valid_id(node.get("id")):
            raise ValueError("Invalid answer character ID.")
        pos, char = node.get("pos"), node.get("c")
        if (not isinstance(pos, list) or not 1 <= len(pos) <= MAX_POSITION_DEPTH or
                any(type(n) is not int or not 0 <= n <= 65535 for n in pos) or
                not isinstance(char, str) or len(char) != 1):
            raise ValueError("Invalid answer character.")
        cleaned.append({"id": node["id"], "pos": pos, "c": char})
    if any(not _valid_id(node_id) for node_id in deletes):
        raise ValueError("Invalid deleted character ID.")
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM questions WHERE id = ? AND room_id = ?", (question_id, room_id)).fetchone()
        if row is None:
            raise ValueError("Question not found.")
        nodes: dict[str, dict[str, Any]] = {node["id"]: node for node in json.loads(row["answer_nodes"])}
        for node in cleaned:
            if node["id"] not in nodes:
                nodes[node["id"]] = {**node, "deleted": False}
            elif not nodes[node["id"]]["pos"]:  # A delete reached the server before its insertion.
                nodes[node["id"]].update(node)
        for node_id in deletes:
            if node_id in nodes:
                nodes[node_id]["deleted"] = True
            else:
                nodes[node_id] = {"id": node_id, "pos": [], "c": "", "deleted": True}
        if len(nodes) > MAX_NODES_PER_QUESTION:
            raise ValueError("Answer has reached its character limit.")
        if sum(bool(node["pos"] and node["c"] and not node["deleted"]) for node in nodes.values()) > MAX_ANSWER_CHARACTERS:
            raise ValueError(f"An answer can have at most {MAX_ANSWER_CHARACTERS:,} characters.")
        now = time.time()
        conn.execute(
            "UPDATE questions SET answer_nodes = ?, updated_at = ? WHERE id = ?",
            (json.dumps(list(nodes.values()), ensure_ascii=False, separators=(",", ":")), now, question_id),
        )
        conn.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, room_id))
        conn.commit()
    return {
        "type": "answer_ops", "question_id": question_id, "inserts": cleaned,
        "deletes": deletes, "op_id": op_id,
    }


_CELL_KEY = re.compile(r"^(?:line:\d{1,3}(?::char:\d{1,4})?|table:\d{1,2}:row:\d{1,3}:col:\d{1,3}|crypt:(?:decode:\d{1,2}|grid:\d{1,2}:\d{1,2}|formula:\d{1,2}:\d{1,2}|label:[0-9]))$")


def _merge_cell_update(room_id: str, event: dict[str, Any]) -> dict[str, Any]:
    question_id, key, value = event.get("question_id"), event.get("key"), event.get("value")
    if (not _valid_id(question_id) or not isinstance(key, str) or len(key) > 120 or
            not _CELL_KEY.fullmatch(key) or not isinstance(value, str) or len(value) > 1000 or
            any(ord(char) < 32 and char not in "\n\t" for char in value) or
            ((":char:" in key or key.startswith("crypt:label:")) and len(value) > 1)):
        raise ValueError("Invalid worksheet cell update.")
    with closing(_connect()) as conn:
        row = conn.execute("SELECT work_cells FROM questions WHERE room_id = ? AND id = ?", (room_id, question_id)).fetchone()
        if row is None:
            raise ValueError("Question not found.")
        cells = json.loads(row["work_cells"] or "{}")
        if key not in cells and len(cells) >= MAX_WORK_CELLS:
            raise ValueError("Worksheet has reached its cell limit.")
        if value:
            cells[key] = value
        else:
            cells.pop(key, None)
        now = time.time()
        conn.execute("UPDATE questions SET work_cells = ?, updated_at = ? WHERE id = ?", (
            json.dumps(cells, ensure_ascii=False, separators=(",", ":")), now, question_id))
        conn.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, room_id))
        conn.commit()
    return {"type": "cell_update", "question_id": question_id, "key": key, "value": value}


@app.websocket("/ws/{room_id}")
async def room_socket(socket: WebSocket, room_id: str) -> None:
    await socket.accept()
    if not _socket_origin_allowed(socket):
        await socket.close(code=4403)
        return
    try:
        raw_auth = await asyncio.wait_for(socket.receive_text(), timeout=10)
        if not isinstance(raw_auth, str) or len(raw_auth) > 1024:
            await socket.close(code=4401)
            return
        authentication = json.loads(raw_auth)
        if not isinstance(authentication, dict) or authentication.get("type") != "auth" or not _token_has_access(room_id, authentication.get("token")):
            await socket.close(code=4401)
            return
    except (asyncio.TimeoutError, WebSocketDisconnect, json.JSONDecodeError, TypeError):
        await socket.close(code=4401)
        return
    connections = _connections(room_id)
    async with connections.lock:
        connections.clients[socket] = "Partner"
        await socket.send_json({"type": "snapshot", "room": _require_room(room_id),
                                "presence": len(connections.clients)})
        await connections.broadcast(connections.presence())
    try:
        while True:
            raw = await socket.receive_text()
            if len(raw.encode("utf-8")) > MAX_WS_BYTES:
                await socket.send_json({"type": "error", "message": "Update is too large."})
                continue
            event: Any = None
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("Invalid message.")
                if event.get("type") == "presence":
                    name = str(event.get("name", "Partner")).strip()[:40] or "Partner"
                    connections.clients[socket] = name
                    await connections.broadcast(connections.presence())
                elif event.get("type") == "answer_ops":
                    async with connections.lock:
                        result = _merge_answer_ops(room_id, event)
                        await connections.broadcast(result)
                elif event.get("type") == "cell_update":
                    async with connections.lock:
                        result = _merge_cell_update(room_id, event)
                        await connections.broadcast(result)
                else:
                    raise ValueError("Unknown message type.")
            except (json.JSONDecodeError, ValueError) as exc:
                response: dict[str, Any] = {"type": "error", "message": str(exc)}
                if isinstance(event, dict) and event.get("type") == "answer_ops":
                    if _valid_id(event.get("op_id")):
                        response["op_id"] = event["op_id"]
                    if _valid_id(event.get("question_id")):
                        response["question_id"] = event["question_id"]
                elif isinstance(event, dict) and event.get("type") == "cell_update":
                    if _valid_id(event.get("question_id")):
                        response["question_id"] = event["question_id"]
                    if isinstance(event.get("key"), str) and len(event["key"]) <= 120:
                        response["key"] = event["key"]
                await socket.send_json(response)
    except WebSocketDisconnect:
        pass
    finally:
        async with connections.lock:
            connections.clients.pop(socket, None)
            await connections.broadcast(connections.presence())
