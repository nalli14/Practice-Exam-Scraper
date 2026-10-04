"""Web front end for scrape_results: upload a saved results page, get the JSON back.

Nothing is stored: the upload is parsed in memory and the JSON is returned to the
browser, which saves it to the user's downloads folder.

Run locally:  flask --app app run
On Azure App Service (Linux, Python), gunicorn finds `app` in app.py on its own.
"""

import datetime as dt
import json
from pathlib import Path

from flask import Flask, jsonify, request

from scrape_results import ParseError, file_stem, fill_gaps, parse_results

ALLOWED_SUFFIXES = {".html", ".htm", ".txt"}

app = Flask(__name__, static_folder="static", static_url_path="/static")
# A saved results page is about 1 MB; leave plenty of room but refuse anything silly.
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.json.sort_keys = False


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/parse")
def parse():
    upload = request.files.get("page")
    if upload is None or not upload.filename:
        return error("Choose a saved results page to upload.")
    filename = Path(upload.filename).name
    if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
        return error("Upload the saved page as .html or .htm (or a .txt copy of the Answer Summary).")

    date = request.form.get("date") or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        return error(f"The attempt date must be YYYY-MM-DD, got {date!r}.")
    first_attempt = {"first": True, "retake": False}.get(request.form.get("attempt"))

    text = upload.read().decode("utf-8", errors="replace")
    try:
        result, warnings, unrecorded = parse_results(text, filename, date, first_attempt)
    except ParseError as e:
        return error(str(e), 422)

    questions = {q["number"]: q for q in result["questions"]}
    gaps = [{
        "number": n,
        "type": questions[n].get("type"),
        "question": questions[n]["question"],
        "explanation": questions[n]["explanation"],
        "slots": slots,
    } for n, slots in unrecorded.items()]
    return jsonify({**saved_file(result), "warnings": warnings,
                    "unrecorded": list(unrecorded), "gaps": gaps, "result": result})


@app.post("/api/fill")
def fill():
    """Apply hand-picked answers to a result from /api/parse and return the file again."""
    body = request.get_json(silent=True) or {}
    result, fills = body.get("result"), body.get("fills")
    if not isinstance(result, dict) or not isinstance(result.get("questions"), list) \
            or not isinstance(fills, list):
        return error("Send the parsed result and the answers to fill in.")
    try:
        filled = fill_gaps(result, fills)
    except (ValueError, KeyError, TypeError) as e:
        return error(f"Couldn't fill in those answers: {e}.")
    return jsonify({**saved_file(result), "filled": filled})


def saved_file(result):
    """The download name and the file exactly as the command-line tool writes it."""
    return {
        "filename": f"{file_stem(result)}.json",
        "file_text": json.dumps(result, indent=2, ensure_ascii=False) + "\n",
    }


@app.errorhandler(413)
def too_large(_):
    return error("That file is too large. Saved results pages are usually about 1 MB.", 413)


def error(message, status=400):
    return jsonify({"error": message}), status
