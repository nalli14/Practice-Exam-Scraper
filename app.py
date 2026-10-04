"""Web front end for scrape_results: upload a saved results page, get the JSON back.

Nothing is stored: the upload is parsed in memory and the JSON is returned to the
browser, which saves it to the user's downloads folder.

Run locally:  flask --app app run
On Azure App Service (Linux, Python), gunicorn finds `app` in app.py on its own.
"""

import datetime as dt
import html
import json
import os
from pathlib import Path

from flask import Flask, jsonify, request

import screenshot_reader
from scrape_results import ParseError, file_stem, fill_gaps, parse_results

ALLOWED_SUFFIXES = {".html", ".htm", ".txt"}

app = Flask(__name__, static_folder="static", static_url_path="/static")
# A saved results page is about 1 MB; leave plenty of room but refuse anything silly.
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.json.sort_keys = False


def allowed_users():
    """Accounts allowed in, from ALLOWED_USERS (comma-separated, any case).

    App Service authentication decides how people sign in, but an identity
    provider like GitHub lets any account sign in, so the allow-list is checked
    here. Unset means no check, for local development.
    """
    return {u.strip().lower() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()}


@app.before_request
def check_user():
    allowed = allowed_users()
    if not allowed or request.path == "/healthz":
        return None
    # App Service sets these headers after signing someone in and strips any copy
    # sent by the browser, so they can be trusted while authentication is on.
    name = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME") or ""
    if request.headers.get("X-MS-CLIENT-PRINCIPAL-IDP") and name.lower() in allowed:
        return None
    if request.path.startswith("/api/"):
        return error("This account doesn't have access to this site.", 403)
    who = f" ({html.escape(name)})" if name else ""
    return (f"<!doctype html><meta charset=utf-8><title>No access</title>"
            f"<p style='font-family:system-ui;margin:40px'>This account{who} doesn't have "
            f"access to this site. To allow it, add that name to the ALLOWED_USERS app "
            f"setting, or <a href='/.auth/logout'>sign out</a> and sign in with another "
            f"account.</p>", 403)


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/me")
def me():
    """Who's signed in, as App Service authentication reports it (null when it's off).

    App Service sets this header itself and strips any copy sent by the browser.
    """
    return {"name": request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")}


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
                    "unrecorded": list(unrecorded), "gaps": gaps, "result": result,
                    "screenshots": screenshot_reader.is_configured()})


@app.post("/api/read-screenshot")
def read_screenshot():
    """Pick one question's missing answers from its screenshots (see screenshot_reader)."""
    try:
        gap = json.loads(request.form.get("gap") or "")
        slots = gap["slots"]
        if not slots or not all(isinstance(s.get("choices"), list) and s["choices"] for s in slots):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        return error("Send the question's gaps along with the screenshot.")

    files = request.files.getlist("screenshots")
    if not files or not any(f.filename for f in files):
        return error("Choose a screenshot of the question.")
    if len(files) > screenshot_reader.MAX_IMAGES:
        return error(f"Send at most {screenshot_reader.MAX_IMAGES} screenshots per question.")
    images = []
    for f in files:
        if f.mimetype not in screenshot_reader.IMAGE_TYPES:
            return error(f"{f.filename} isn't a PNG, JPEG, WebP or GIF image.")
        data = f.read()
        if len(data) > 5 * 1024 * 1024:
            return error(f"{f.filename} is over 5 MB. Crop it to the question and try again.")
        images.append((data, f.mimetype))

    try:
        picks = screenshot_reader.read_gaps(images, gap)
    except screenshot_reader.ReaderUnavailable as e:
        return error(str(e), 503)
    except ValueError as e:
        return error(str(e), 422)
    return jsonify({"picks": picks})


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
