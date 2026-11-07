# Decipher — Codebusters together

A small site for working through an uploaded Codebusters PDF with a partner. Each room has a secret link. Both people see each other's answer edits as they happen, including edits to the same answer. The original PDF page remains visible beside the extracted question and cipher, and you can correct the extraction. Checking a question adds its points to the shared score; unchecking removes them. There is no automatic grading.

## Run it

You need Python 3.11 or later. On Windows, extract the ZIP and double-click `start.bat`. On macOS or Linux, run `./start.sh` from the extracted directory. The launchers install the dependencies in a local virtual environment on the first run.

To run it manually instead, from this directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` and create a room with a unique password of at least eight characters. Upload a PDF, then use **Copy invite link** to share the room with your partner; they enter the password to join. Both browsers must reach **the same running server**. On a home or school network, replace `localhost` in the invite link with the host computer's LAN address; your network firewall must allow port 8000. To publish the interface on GitHub Pages and collaborate across the internet, follow [DEPLOY.md](DEPLOY.md).

The room link contains its ID; the password is also required to join. Anyone with both can read and change the test. Keep both within your team and avoid reusing an account password. The PDF, answers, and scores are stored on the server in `data/` by default. Set `CODEBUSTERS_DATA_DIR` to another writable, persistent directory before starting if desired. Back up that directory to preserve rooms. Use a persistent volume mounted there when running the provided Dockerfile.

## Working with tests

- Upload a text based PDF (up to 25 MB and 150 pages). The import looks for numbered questions and point labels, then separates likely cipher text from the instructions. Check the result against the original PDF shown in the app and edit any fields that were parsed incorrectly.
- The app generates working tables for Aristocrat, Patristocrat, Xenocrypt, Baconian, Nihilist, Checkerboard, Fractionated Morse, and Cryptarithm questions, even when the PDF's table text cannot be extracted. K2 substitution tables show Replacement, K2 alphabet, and Frequency in that order; other substitution keys show Alphabet, Frequency, and Replacement. Xenocrypt includes Ñ. Frequency counts come from the extracted ciphertext. Nihilist shows the numbered 1–5 keyed square, Checkerboard has 36 blank cells, and Cryptarithm has its answer boxes and A–J by 1–10 grid. Fractionated Morse has 26 editable replacement boxes above the printed Morse triples. Every cipher, including Cryptarithm, has editable work spaces under each extracted ciphertext line. All entered boxes and lines save to the room and update live for both partners.
- Existing rooms rebuild outdated worksheets when opened after an update; their saved answers, checkbox scores, and editable entries stay in the same room. Keep the server's existing data volume when redeploying.
- For scanned or image only PDFs, the page still appears, but automatic text extraction cannot read it. Use **Add question** and enter the prompt, cipher, and points manually.
- Open the same room in two browsers to work together. Answer letters merge between browsers in real time; grid cells, line entries, checked questions, points, and question edits update for both collaborators too. Work is stored automatically.
- Answers can hold up to 10,000 visible characters each.
- A checkmark means that **you decided to award** the question's points. It does not verify the answer.

## Container hosting

```bash
docker build -t codebusters-together .
docker run --rm -p 8000:8000 -v codebusters-data:/app/data codebusters-together
```

For an internet deployment, point your host's persistent volume at `/app/data`. Keep at least one server process per SQLite database; this app coordinates live sockets in one process.

For a server check after installing the development dependencies, run `pip install -r requirements-dev.txt`, `python tests/smoke.py`, and `python tests/layout_smoke.py`. With Node.js installed, run `node tests/crdt_smoke.cjs` and `node tests/worksheet_ui_smoke.cjs` to exercise concurrent edits and worksheet rendering.
