"""Regression checks for editable PDF worksheets and live shared cells.

Run from the project root: python tests/layout_smoke.py
The generated PDF deliberately positions table labels in separate text spans,
the way a test PDF often does; flattening its text produces scrambled lists.
"""

import io
import itertools
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import fitz
from fastapi.testclient import TestClient


ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SPANISH_ALPHABET = "ABCDEFGHIJKLMNÑOPQRSTUVWXYZ"
HERE = Path(__file__).resolve().parent.parent


def legacy_database(directory):
    """An older installation's schema and an already solved question."""
    with sqlite3.connect(Path(directory) / "codebusters.sqlite3") as db:
        db.executescript("""
            CREATE TABLE rooms (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, pdf_name TEXT,
                pdf_path TEXT, page_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE questions (
                id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                number INTEGER NOT NULL, page INTEGER NOT NULL,
                prompt TEXT NOT NULL DEFAULT '', cipher TEXT NOT NULL DEFAULT '',
                points INTEGER NOT NULL DEFAULT 0, checked INTEGER NOT NULL DEFAULT 0,
                answer_nodes TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
        """)
        db.execute("INSERT INTO rooms (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                   ("legacy-room", "Existing work", 1, 1))
        nodes = [{"id": "old:1", "pos": [100], "c": "O", "deleted": False},
                 {"id": "old:2", "pos": [200], "c": "K", "deleted": False}]
        db.execute("INSERT INTO questions (id, room_id, number, page, prompt, cipher, points, checked, answer_nodes, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("legacy-question", "legacy-room", 1, 1, "Old prompt", "ABC", 50, 1, json.dumps(nodes), 1, 1))


def cell(page, x, y, text, *, size=9):
    page.insert_text((x, y), str(text), fontsize=size)


def title(page, number, points, kind, instructions="Decode this cipher."):
    cell(page, 35, 42, f"{number}) [{points} points] {instructions} Use the {kind} cipher.", size=9)


def grid(page, x, y, columns, rows, step_x, step_y, *, blank_header=False):
    """Draw actual cell borders as well as individually positioned text spans."""
    for r in range(rows):
        for c in range(columns):
            page.draw_rect(fitz.Rect(x + c * step_x, y + r * step_y,
                                     x + (c + 1) * step_x, y + (r + 1) * step_y),
                           color=(0.3, 0.3, 0.3), width=0.4)


def alphabet_table(page, *, baconian=False):
    x0, dx, y = 95, 18.5, 173
    cell(page, 32, y, "Alphabet", size=8)
    for i, letter in enumerate(ALPHABET):
        cell(page, x0 + i * dx, y, letter, size=8)
    cell(page, 32, y + 25, "Baconian" if baconian else "Frequency", size=8)
    for i in range(26):
        value = format(i, "05b").replace("0", "A").replace("1", "B") if baconian else str((i * 3 + 1) % 10)
        cell(page, x0 + i * dx, y + 25, value, size=5 if baconian else 8)
    cell(page, 32, y + 50, "Replacement", size=8)
    grid(page, x0 - 3, y + 30, 26, 1, dx, 24)


def fixture_pdf():
    pdf = fitz.open()

    aristocrat = pdf.new_page()
    title(aristocrat, 1, 120, "Aristocrat")
    cell(aristocrat, 54, 87, "YMJ VZNHP GWTBS KTC")
    cell(aristocrat, 54, 114, "OZRUX TAJW YMJ QFED ITL")
    alphabet_table(aristocrat)

    patristocrat = pdf.new_page()
    title(patristocrat, 2, 80, "Patristocrat")
    cell(patristocrat, 54, 87, "QEB NRFZH YOLTK CLU GRJMP LSBO QEB IXWV ALD")
    alphabet_table(patristocrat)

    baconian = pdf.new_page()
    title(baconian, 3, 90, "Baconian")
    cell(baconian, 54, 87, "AABAA ABBBA BAAAA ABABA AAAAA")
    alphabet_table(baconian, baconian=True)

    nihilist = pdf.new_page()
    title(nihilist, 4, 110, "Nihilist")
    cell(nihilist, 54, 87, "23 54 12 35 41 25 52 13 24")
    cell(nihilist, 55, 113, "Fill in the keyed square.")
    grid(nihilist, 110, 152, 6, 6, 40, 30)
    for i in range(5):
        cell(nihilist, 110 + 40 * (i + 1) + 15, 171, i + 1)
        cell(nihilist, 125, 152 + 30 * (i + 1) + 19, i + 1)

    crypt = pdf.new_page()
    cell(crypt, 25, 35, "10) [313 points] A quote uses the Cryptarithm Cipher. What do the values 2734851 decode to?", size=9)
    cell(crypt, 50, 82, "Values to decode for solution", size=12)
    for i, value in enumerate("2734851"):
        cell(crypt, 65 + i * 26, 108, value, size=11)
    grid(crypt, 61, 118, 7, 1, 26, 24)
    cell(crypt, 50, 175, "Cryptarithm formula", size=12)
    cell(crypt, 125, 206, "O T I S", size=12)
    # The plus sign often sits on another PDF baseline beside the second
    # formula row; keep it detached so extraction must reunite the equation.
    cell(crypt, 92, 227, "+", size=12)
    cell(crypt, 109, 238, "B Y R O N", size=12)
    cell(crypt, 109, 270, "S P R O U T", size=12)
    grid(crypt, 320, 95, 10, 10, 22, 28)
    for i, digit in enumerate("0123456789"):
        cell(crypt, 328 + i * 22, 84, digit, size=8)
    for i, letter in enumerate("BINOPRSTUY"):
        cell(crypt, 302, 114 + 28 * i, letter, size=8)
    cell(crypt, 40, 745, "LASSO Division C Invitational 2026", size=8)
    cell(crypt, 40, 765, "Team Number: C____________", size=8)

    morse = pdf.new_page()
    cell(morse, 25, 38, "14) [303 points] Decode the quote with the Fractionated Morse cipher.", size=9)
    for line_index, text in enumerate(("LIGNAXHPCSYKQNCP", "ETFARXBCEBF", "YVLFARAAVVICIJ", "YERQRCURAYBD", "LGQLLIHA")):
        cell(morse, 55, 100 + line_index * 80, "   ".join(text), size=10)
    x0, dx = 123, 17.2
    cell(morse, 27, 560, "Replacement", size=8)
    cell(morse, 40, 587, "Morse", size=8)
    cell(morse, 35, 599, "fraction", size=8)
    grid(morse, x0 - 5, 548, 26, 2, dx, 55)
    # Every column has a unique three-character Morse fraction; the row
    # above it is intentionally blank for collaborative replacement letters.
    for i in range(26):
        fraction = "".join(".-x"[(i // divisor) % 3] for divisor in (9, 3, 1))
        cell(morse, x0 + i * dx, 582, fraction, size=6)
    cell(morse, 40, 745, "11/27/25, 9:07 AM", size=8)

    binary = pdf.tobytes()
    pdf.close()
    return binary


def no_table_pdf():
    """Cipher headings and ciphertext only, so tables must be generated."""
    pdf = fitz.open()
    cases = (
        (21, "Aristocrat K2", ("ABBA ZZYY",)),
        (22, "Aristocrat K1", ("XYZ XYZ ABC",)),
        (23, "Patristocrat K3", ("FGHI JKLM NOPQ",)),
        (24, "Aristocrat", ("QQRR AABB ZZYY",)),
        (25, "Xenocrypt K2", ("NiÑa Camino PiÑata",)),
        (26, "Nihilist", ("23 54 12 35 41 25 52 13 24",)),
        (27, "Checkerboard", ("AABCD AABCD EEFFD",)),
        (28, "Cryptarithm", ("Cryptarithm formula", "B A S E", "+ B A L L", "G A M E S")),
        (29, "Baconian", ("AABAA ABBBA BAAAA ABABA AAAAA",)),
        (30, "Fractionated Morse", ("L I G N A X H P C S Y K Q N C P", "E T F A R X B C E B F")),
    )
    for number, kind, lines in cases:
        page = pdf.new_page()
        directions = "What do the values 315257 decode to?" if number == 28 else "Decode this cipher."
        title(page, number, 100 + number, kind, instructions=directions)
        for i, line in enumerate(lines):
            cell(page, 55, 92 + 38 * i, line, size=10)
    data = pdf.tobytes()
    pdf.close()
    return data


def generated_layout_checks(questions):
    by_number = {question["number"]: question for question in questions}
    assert set(range(21, 31)) <= by_number.keys(), by_number.keys()
    expected_kinds = {21: "aristocrat", 22: "aristocrat", 23: "patristocrat", 24: "aristocrat",
                      25: "xenocrypt", 26: "nihilist", 27: "checkerboard", 28: "cryptarithm",
                      29: "baconian", 30: "fractionated_morse"}
    for number, kind in expected_kinds.items():
        q = by_number[number]
        assert q["cipher_kind"] == kind, q
        assert q["points"] == number + 100, q
        assert q["cipher_layout"]["lines"], (number, q)
        assert q["cipher_layout"]["tables"], (number, q)

    for number, label, row_labels in (
        (21, "K2", ["Replacement", "K2", "Frequency"]),
        (22, "K1", ["K1", "Frequency", "Replacement"]),
        (23, "K3", ["K3", "Frequency", "Replacement"]),
        (24, "Alphabet", ["Alphabet", "Frequency", "Replacement"]),
        (25, "K2", ["Replacement", "K2", "Frequency"]),
    ):
        table = by_number[number]["cipher_layout"]["tables"][0]
        assert table["type"] == "frequency", table
        assert table["header_label"] == label, table
        alphabet = SPANISH_ALPHABET if number == 25 else ALPHABET
        assert table["headers"] == list(alphabet), table
        assert [row["label"] for row in table["rows"]] == row_labels, table
        assert len(table["rows"]) == 3 and all(len(row["values"]) == len(alphabet) for row in table["rows"]), table
        alphabet_row = table["rows"][row_labels.index(label)]["values"]
        assert alphabet_row == list(alphabet), table
        assert table["rows"][row_labels.index("Replacement")]["values"] == [""] * len(alphabet), table
    k2 = by_number[21]["cipher_layout"]["tables"][0]
    frequency_values = k2["rows"][2]["values"]
    assert [frequency_values[ALPHABET.index(c)] for c in "ABCYZ"] == ["2", "2", "0", "2", "2"], k2
    xenocrypt = by_number[25]["cipher_layout"]["tables"][0]
    assert xenocrypt["rows"][2]["values"][SPANISH_ALPHABET.index("Ñ")] == "2", xenocrypt
    spanish_lines = by_number[25]["cipher_layout"]["lines"]
    assert any("NiÑa Camino PiÑata" in line for line in spanish_lines), spanish_lines
    upper = "".join(spanish_lines).upper()
    assert xenocrypt["rows"][2]["values"] == [str(upper.count(letter)) for letter in SPANISH_ALPHABET], xenocrypt

    nihilist = by_number[26]["cipher_layout"]["tables"][0]
    assert nihilist["type"] == "keyed_square", nihilist
    assert nihilist["column_headers"] == list("12345") and nihilist["row_headers"] == list("12345"), nihilist
    assert nihilist["values"] == [[""] * 5 for _ in range(5)], nihilist
    checkerboard = by_number[27]["cipher_layout"]["tables"][0]
    assert checkerboard["type"] == "checkerboard", checkerboard
    assert checkerboard["values"] == [[""] * 6 for _ in range(6)], checkerboard
    assert not checkerboard.get("column_headers") and not checkerboard.get("row_headers"), checkerboard

    crypt = by_number[28]["cipher_layout"]["tables"][0]
    assert crypt["type"] == "cryptarithm" and crypt["decode_digits"] == "315257", crypt
    assert crypt["digits"] == [str(i) for i in range(1, 11)], crypt
    assert crypt["letters"] == list("ABCDEFGHIJ"), crypt
    assert ["".join(char for char in row if char.isalpha() or char == "+") for row in crypt["formula_rows"]] == ["BASE", "+BALL", "GAMES"], crypt

    baconian = by_number[29]["cipher_layout"]["tables"][0]
    assert baconian["type"] == "baconian", baconian
    assert [row["label"] for row in baconian["rows"]] == ["Alphabet", "Baconian", "Replacement"], baconian
    assert baconian["rows"][0]["values"] == list(ALPHABET), baconian
    assert baconian["rows"][2]["values"] == [""] * 26, baconian
    morse = by_number[30]["cipher_layout"]["tables"][0]
    assert morse["type"] == "morse_fraction" and morse["replacement"] == [""] * 26, morse
    assert morse["fractions"] == ["".join(chars) for chars in itertools.product(".-x", repeat=3) if chars != ("x", "x", "x")], morse


def layout_checks(questions):
    by_number = {question["number"]: question for question in questions}
    assert {1, 2, 3, 4, 10, 14} <= by_number.keys(), by_number.keys()
    expected = {1: "aristocrat", 2: "patristocrat", 3: "baconian", 4: "nihilist",
                10: "cryptarithm", 14: "fractionated_morse"}
    for number, kind in expected.items():
        q = by_number[number]
        assert q["cipher_kind"] == kind, (number, q)
        assert q["points"] == {1: 120, 2: 80, 3: 90, 4: 110, 10: 313, 14: 303}[number], q
        assert isinstance(q["cipher_layout"].get("lines"), list), q
        assert isinstance(q.get("work_cells", {}), dict), q
    for number in (1, 2):
        table = next(table for table in by_number[number]["cipher_layout"]["tables"] if table["type"] == "frequency")
        assert table["headers"] == list(ALPHABET), table
        assert [row["label"] for row in table["rows"]] == ["Alphabet", "Frequency", "Replacement"], table
        assert all(len(row["values"]) == 26 for row in table["rows"]), table
        assert table["rows"][0]["values"] == list(ALPHABET), table
        ciphertext = "".join(by_number[number]["cipher_layout"]["lines"]).upper()
        assert table["rows"][1]["values"] == [str(ciphertext.count(char)) for char in ALPHABET], table
    baconian_table = next(table for table in by_number[3]["cipher_layout"]["tables"] if table["type"] == "baconian")
    assert baconian_table["headers"] == list(ALPHABET), baconian_table
    assert [row["label"] for row in baconian_table["rows"]] == ["Alphabet", "Baconian", "Replacement"], baconian_table
    assert baconian_table["rows"][0]["values"] == list(ALPHABET), baconian_table
    assert baconian_table["rows"][1]["values"][:5] == ["AAAAA", "AAAAB", "AAABA", "AAABB", "AABAA"], baconian_table
    assert len(baconian_table["rows"][-1]["values"]) == 26, baconian_table
    square = next(table for table in by_number[4]["cipher_layout"]["tables"] if table["type"] == "keyed_square")
    assert square["column_headers"] == list("12345"), square
    assert square["row_headers"] == list("12345"), square
    assert len(square["values"]) == 5 and all(len(row) == 5 for row in square["values"]), square
    crypt = by_number[10]
    assert "2734851" in crypt["prompt"] and "012" not in crypt["prompt"], crypt["prompt"]
    assert "LASSO" not in crypt["prompt"] and "Team Number" not in crypt["cipher"], crypt
    table = next(table for table in crypt["cipher_layout"]["tables"] if table["type"] == "cryptarithm")
    assert table["decode_digits"] == "2734851", table
    assert table["digits"] == [str(i) for i in range(1, 11)], table
    assert table["letters"] == list("ABCDEFGHIJ"), table
    assert ["".join(char for char in row if char.isalnum() or char == "+") for row in table["formula_rows"]] == ["OTIS", "+BYRON", "SPROUT"], table
    assert any("OTIS" in line.replace(" ", "") for line in crypt["cipher_layout"]["lines"]), crypt
    morse_layout = by_number[14]["cipher_layout"]
    assert len(morse_layout["lines"]) == 5, morse_layout
    assert "LIGNAXHPCSYKQNCP" in morse_layout["lines"][0].replace(" ", ""), morse_layout
    chart = next(table for table in morse_layout["tables"] if table["type"] == "morse_fraction")
    assert len(chart["fractions"]) == 26, chart
    assert len(chart["replacement"]) == len(chart["fractions"]), chart
    assert chart["fractions"] == ["".join(chars) for chars in itertools.product(".-x", repeat=3) if chars != ("x", "x", "x")], chart


def next_type(socket, wanted):
    for _ in range(12):
        message = socket.receive_json()
        if message.get("type") == "error":
            raise AssertionError(message)
        if message.get("type") == wanted:
            return message
    raise AssertionError(f"Missing websocket event {wanted}")


def run():
    with tempfile.TemporaryDirectory() as temporary:
        os.environ["CODEBUSTERS_DATA_DIR"] = temporary
        legacy_database(temporary)
        sys.path.insert(0, str(HERE))
        from server import _parse_pdf, _room_state, app

        old_room = _room_state("legacy-room")
        assert old_room["score"] == 50, old_room
        old_question = old_room["questions"][0]
        assert old_question["answer"] == "OK" and old_question["answer_nodes"][0]["id"] == "old:1", old_question
        assert old_question["cipher"] == "ABC" and old_question["prompt"] == "Old prompt", old_question
        assert old_question["work_cells"] == {}, old_question

        fixture = fixture_pdf()
        page_count, parsed = _parse_pdf(fixture)
        assert page_count == 6, page_count
        layout_checks([{**q, "work_cells": {}} for q in parsed])
        no_table = no_table_pdf()
        page_count, generated = _parse_pdf(no_table)
        assert page_count == 10, page_count
        generated_layout_checks([{**q, "work_cells": {}} for q in generated])

        with TestClient(app) as client:
            response = client.post("/api/rooms", json={"title": "Worksheet", "password": "a-shared-passphrase"})
            assert response.status_code == 200, response.text
            room_id, alice = response.json()["room"]["id"], response.json()["token"]
            join = client.post(f"/api/rooms/{room_id}/join", json={"password": "a-shared-passphrase"})
            assert join.status_code == 200, join.text
            bob = join.json()["token"]
            auth = {"Authorization": f"Bearer {alice}"}
            response = client.post(f"/api/rooms/{room_id}/upload", headers=auth,
                                   files={"file": ("positioned-grids.pdf", io.BytesIO(fixture), "application/pdf")})
            assert response.status_code == 200, response.text
            room = response.json()["room"]
            layout_checks(room["questions"])
            question_id = next(q["id"] for q in room["questions"] if q["number"] == 10)
            morse_id = next(q["id"] for q in room["questions"] if q["number"] == 14)

            # An older room can recover printable grids from its retained PDF.
            with sqlite3.connect(Path(temporary) / "codebusters.sqlite3") as db:
                db.execute("UPDATE questions SET cipher_layout = '{}', cipher_kind = 'generic', "
                           "prompt = prompt || ' 0 1 2' WHERE id = ?", (question_id,))
            migrated = client.get(f"/api/rooms/{room_id}", headers=auth).json()
            old_crypt = next(q for q in migrated["questions"] if q["id"] == question_id)
            assert old_crypt["cipher_kind"] == "cryptarithm", old_crypt
            assert old_crypt["cipher_layout"]["tables"][0]["letters"] == list("ABCDEFGHIJ"), old_crypt
            assert old_crypt["cipher_layout"]["tables"][0]["decode_digits"] == "2734851", old_crypt
            assert "0 1 2" not in old_crypt["prompt"], old_crypt["prompt"]

            aristocrat = next(q for q in room["questions"] if q["number"] == 1)
            corrected = client.patch(f"/api/rooms/{room_id}/questions/{aristocrat['id']}",
                                     json={"cipher": "AB CD\nEF"}, headers=auth)
            assert corrected.status_code == 200, corrected.text
            assert corrected.json()["question"]["cipher_layout"]["lines"] == ["AB CD", "EF"], corrected.text
            assert corrected.json()["question"]["cipher_layout"]["tables"][0]["type"] == "frequency"

            legacy_table = {"type": "frequency", "headers": list(ALPHABET),
                            "rows": [{"label": "Frequency", "values": ["0"] * 26},
                                     {"label": "Replacement", "values": [""] * 26}]}
            for label, expected_row in (("K2", 0), ("K1", 2)):
                added = client.post(f"/api/rooms/{room_id}/questions",
                                    json={"prompt": f"Decode this Aristocrat {label} cipher.",
                                          "cipher": "ABBA ZZYY", "points": 21}, headers=auth)
                assert added.status_code == 200, added.text
                old_id = added.json()["question"]["id"]
                with sqlite3.connect(Path(temporary) / "codebusters.sqlite3") as db:
                    db.execute("UPDATE questions SET cipher_layout = ?, work_cells = ? WHERE id = ?",
                               (json.dumps({"lines": ["ABBA ZZYY"], "tables": [legacy_table]}),
                                json.dumps({"table:0:row:1:col:0": "R", "line:0:char:0": "H"}), old_id))
                refreshed = client.get(f"/api/rooms/{room_id}", headers=auth).json()
                upgraded = next(q for q in refreshed["questions"] if q["id"] == old_id)
                ordered = [row["label"] for row in upgraded["cipher_layout"]["tables"][0]["rows"]]
                assert ordered[expected_row] == "Replacement", upgraded
                assert upgraded["work_cells"][f"table:0:row:{expected_row}:col:0"] == "R", upgraded
                assert upgraded["work_cells"]["line:0:char:0"] == "H", upgraded

            with client.websocket_connect(f"/ws/{room_id}") as partner_a:
                partner_a.send_json({"type": "auth", "token": alice})
                next_type(partner_a, "snapshot")
                with client.websocket_connect(f"/ws/{room_id}") as partner_b:
                    partner_b.send_json({"type": "auth", "token": bob})
                    next_type(partner_b, "snapshot")
                    partner_a.send_json({"type": "cell_update", "question_id": question_id,
                                         "key": "crypt:grid:3:4", "value": "7"})
                    assert next_type(partner_b, "cell_update")["value"] == "7"
                    assert next_type(partner_a, "cell_update")["key"] == "crypt:grid:3:4"
                    partner_b.send_json({"type": "cell_update", "question_id": question_id,
                                         "key": "crypt:grid:3:4", "value": "9"})
                    assert next_type(partner_a, "cell_update")["value"] == "9"
                    assert next_type(partner_b, "cell_update")["value"] == "9"
                    partner_a.send_json({"type": "cell_update", "question_id": question_id,
                                         "key": "crypt:decode:0", "value": "A"})
                    assert next_type(partner_b, "cell_update")["value"] == "A"
                    next_type(partner_a, "cell_update")
                    partner_b.send_json({"type": "cell_update", "question_id": question_id,
                                         "key": "crypt:formula:1:1", "value": "B"})
                    assert next_type(partner_a, "cell_update")["key"] == "crypt:formula:1:1"
                    next_type(partner_b, "cell_update")

                    # Simultaneous work on adjacent characters keeps both
                    # contributions; an entire line must not be overwritten.
                    partner_a.send_json({"type": "cell_update", "question_id": morse_id,
                                         "key": "line:0:char:0", "value": "T"})
                    partner_b.send_json({"type": "cell_update", "question_id": morse_id,
                                         "key": "line:0:char:1", "value": "E"})
                    assert {next_type(partner_a, "cell_update")["key"],
                            next_type(partner_a, "cell_update")["key"]} == {"line:0:char:0", "line:0:char:1"}
                    assert {next_type(partner_b, "cell_update")["key"],
                            next_type(partner_b, "cell_update")["key"]} == {"line:0:char:0", "line:0:char:1"}

            room = client.get(f"/api/rooms/{room_id}", headers={"Authorization": f"Bearer {bob}"}).json()
            crypt = next(q for q in room["questions"] if q["id"] == question_id)
            assert crypt["work_cells"] == {"crypt:grid:3:4": "9", "crypt:decode:0": "A",
                                           "crypt:formula:1:1": "B"}, crypt
            morse_after = next(q for q in room["questions"] if q["id"] == morse_id)
            assert morse_after["work_cells"] == {"line:0:char:0": "T", "line:0:char:1": "E"}, morse_after
            with client.websocket_connect(f"/ws/{room_id}") as reconnect:
                reconnect.send_json({"type": "auth", "token": bob})
                snapshot = next_type(reconnect, "snapshot")["room"]
                found = next(q for q in snapshot["questions"] if q["id"] == question_id)
                assert found["work_cells"] == crypt["work_cells"], found

            isolated = client.post("/api/rooms", json={"title": "Different room", "password": "another-passphrase"})
            assert isolated.status_code == 200, isolated.text
            other_id, other_token = isolated.json()["room"]["id"], isolated.json()["token"]
            created = client.post(f"/api/rooms/{other_id}/questions", json={"prompt": "Private", "points": 5},
                                  headers={"Authorization": f"Bearer {other_token}"})
            assert created.status_code == 200, created.text
            other_question = created.json()["question"]["id"]
            with client.websocket_connect(f"/ws/{room_id}") as intruder:
                intruder.send_json({"type": "auth", "token": alice})
                next_type(intruder, "snapshot")
                intruder.send_json({"type": "cell_update", "question_id": other_question,
                                   "key": "crypt:decode:0", "value": "stolen"})
                error = intruder.receive_json()
                if error["type"] == "presence":
                    error = intruder.receive_json()
                assert error["type"] == "error", error
            isolated_room = client.get(f"/api/rooms/{other_id}", headers={"Authorization": f"Bearer {other_token}"}).json()
            assert isolated_room["questions"][0]["work_cells"] == {}, isolated_room

            imported = client.post(f"/api/rooms/{other_id}/upload",
                                   headers={"Authorization": f"Bearer {other_token}"},
                                   files={"file": ("cipher-no-tables.pdf", io.BytesIO(no_table), "application/pdf")})
            assert imported.status_code == 200, imported.text
            generated_layout_checks(imported.json()["room"]["questions"])
            checker = next(q for q in imported.json()["room"]["questions"] if q["number"] == 27)
            with client.websocket_connect(f"/ws/{other_id}") as solve:
                solve.send_json({"type": "auth", "token": other_token})
                next_type(solve, "snapshot")
                solve.send_json({"type": "cell_update", "question_id": checker["id"],
                                 "key": "table:0:row:5:col:5", "value": "Z"})
                assert next_type(solve, "cell_update")["value"] == "Z"
            saved = client.get(f"/api/rooms/{other_id}",
                               headers={"Authorization": f"Bearer {other_token}"}).json()
            saved_checker = next(q for q in saved["questions"] if q["id"] == checker["id"])
            assert saved_checker["work_cells"]["table:0:row:5:col:5"] == "Z", saved_checker

    print("Layout smoke passed: sixteen PDFs with and without grids, stale layout migration, live editors, persistence, isolation.")


if __name__ == "__main__":
    run()
