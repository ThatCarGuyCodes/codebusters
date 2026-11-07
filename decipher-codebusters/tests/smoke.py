"""Exercise PDF import, shared scoring, persistence, and two live clients.

Run from the project root with: python tests/smoke.py
"""

import io
import os
import sys
import tempfile

import fitz
from fastapi.testclient import TestClient


def next_type(socket, wanted):
    for _ in range(15):
        msg = socket.receive_json()
        if msg.get("type") == wanted:
            return msg
    raise AssertionError(f"Never received {wanted}")


def example_pdf():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 80), "1) [120 points] Decode this Aristocrat cipher.")
    page.insert_text((60, 115), "YMJ VZNHP GWTBS KTC")
    page.insert_text((60, 190), "2) [80 points] Decode this Caesar cipher.")
    page.insert_text((60, 225), "KHOOR ZRUOG")
    raw = doc.tobytes()
    doc.close()
    return raw


def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CODEBUSTERS_DATA_DIR"] = tmp
        os.environ["CODEBUSTERS_ALLOWED_ORIGINS"] = "https://example.github.io"
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from server import app

        with TestClient(app) as client:
            response = client.post("/api/rooms", json={"title": "Practice together", "password": "unique-room-passphrase"})
            assert response.status_code == 200, response.text
            room_id = response.json()["room"]["id"]
            token = response.json()["token"]
            auth = {"Authorization": f"Bearer {token}"}
            assert client.get(f"/api/rooms/{room_id}").status_code == 401
            assert client.get(f"/api/rooms/{room_id}/pages/1").status_code == 401
            assert client.post(f"/api/rooms/{room_id}/join", json={"password": "wrong-password"}).status_code == 401
            joined = client.post(f"/api/rooms/{room_id}/join", json={"password": "unique-room-passphrase"})
            assert joined.status_code == 200, joined.text
            partner_token = joined.json()["token"]
            preflight = client.options(
                f"/api/rooms/{room_id}",
                headers={"Origin": "https://example.github.io", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"}
            )
            assert preflight.headers.get("access-control-allow-origin") == "https://example.github.io", preflight.headers
            response = client.post(
                f"/api/rooms/{room_id}/upload",
                files={"file": ("practice.pdf", io.BytesIO(example_pdf()), "application/pdf")},
                headers=auth,
            )
            assert response.status_code == 200, response.text
            room = client.get(f"/api/rooms/{room_id}", headers=auth).json()
            assert room["page_count"] == 1, room
            assert len(room["questions"]) >= 2, room["questions"]
            assert room["questions"][0]["points"] == 120, room["questions"][0]
            assert client.get(f"/api/rooms/{room_id}/pages/1", headers=auth).headers["content-type"].startswith("image/png")

            question_id = room["questions"][0]["id"]
            from starlette.websockets import WebSocketDisconnect
            with client.websocket_connect(f"/ws/{room_id}", headers={"Origin": "https://unapproved.example"}) as disallowed:
                try:
                    disallowed.receive_json()
                    raise AssertionError("Disallowed origin received room data")
                except WebSocketDisconnect as exc:
                    assert exc.code == 4403
            with client.websocket_connect(f"/ws/{room_id}") as unauthorized:
                unauthorized.send_json({"type": "auth", "token": "incorrect"})
                try:
                    unauthorized.receive_json()
                    raise AssertionError("Unauthorized socket received room data")
                except WebSocketDisconnect as exc:
                    assert exc.code == 4401
            with client.websocket_connect(f"/ws/{room_id}") as a:
                a.send_json({"type": "auth", "token": token})
                next_type(a, "snapshot")
                with client.websocket_connect(f"/ws/{room_id}") as b:
                    b.send_json({"type": "auth", "token": partner_token})
                    next_type(b, "snapshot")
                    a.send_json({
                        "type": "answer_ops", "question_id": question_id,
                        "inserts": [{"id": "a:1", "pos": [200, 11, 1], "c": "A"}],
                        "deletes": [], "op_id": "op-a-1",
                    })
                    next_type(b, "answer_ops")
                    b.send_json({
                        "type": "answer_ops", "question_id": question_id,
                        "inserts": [{"id": "b:1", "pos": [300, 22, 1], "c": "B"}],
                        "deletes": [], "op_id": "op-b-1",
                    })
                    next_type(a, "answer_ops")
                    a.send_json({
                        "type": "answer_ops", "question_id": question_id,
                        "inserts": [{"id": "a:1", "pos": [200, 11, 1], "c": "A"}],
                        "deletes": [], "op_id": "op-a-1-retry",
                    })
                    next_type(b, "answer_ops")

            room = client.get(f"/api/rooms/{room_id}", headers=auth).json()
            assert room["questions"][0]["answer"] == "AB", room["questions"][0]
            response = client.patch(
                f"/api/rooms/{room_id}/questions/{question_id}", json={"checked": True}, headers=auth
            )
            assert response.status_code == 200, response.text
            room = client.get(f"/api/rooms/{room_id}", headers={"Authorization": f"Bearer {partner_token}"}).json()
            assert room["score"] == 120, room
            assert room["questions"][0]["answer"] == "AB", room
    print("Smoke checks passed: password, protected PDF, two live clients, merged answer, score, persistence.")


if __name__ == "__main__":
    main()
