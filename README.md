# Practice-Exam-Scraper

Python app created by myself and Claude Code.

`scrape_results.py` turns a saved practice assessment results page into a JSON file in `practice-test/results/` under the folder you run it from. The JSON includes your score, the per-domain breakdown and every question with your answer, the correct answer, the explanation and the reference links.

It reads a page you've saved, rather than fetching the URL itself. That's because the results page is built by JavaScript and tied to your browser session.

## Web app

`app.py` puts the same parser behind an upload page. You choose the saved page, say whether it was a first attempt, and the JSON downloads through the browser to your usual downloads folder. The page also shows the score, domains and missed questions. Uploads are parsed in memory and never stored.

**Missing answers.** Some answers aren't in a saved page: your pick on a dropdown question you got wrong, and the right match on a drag-to-match row you got wrong. When that happens, the app lists those questions with a dropdown for each gap, filled with that question's own choices. Pick each one from a screenshot of the question, or from its explanation, then download. Filled answers are marked `"filled_in": {"your_answer": "by hand"}` (or `correct_answer`). Gaps you leave as **Not filled** stay `null`.

Run it locally:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/flask --app app run
```

Then open http://127.0.0.1:5000.

### Deploying to Azure App Service

The app runs on a Linux App Service web app with a Python runtime stack, deployed as code. App Service installs `requirements.txt` during the deployment and starts `app.py` with gunicorn on its own, so no startup command is needed.

Deployment is set up from the web app's **Deployment Center** in the Azure portal, with GitHub as the source. Deployment Center adds its own workflow to `.github/workflows/`, and every push to `main` deploys.

Separately, [.github/workflows/tests.yml](.github/workflows/tests.yml) runs the tests on every push and pull request. It doesn't block a deploy, so check the **Actions** tab if a deploy goes out with failing tests.

### Tests

```bash
.venv/bin/python -m unittest -v
```

## Requirements

- Python 3.9 or later. The command-line tool only uses the standard library, so it needs no `pip install`. The web app needs Flask and gunicorn, listed in `requirements.txt`.
- Works on Windows and macOS. On macOS the command is `python3`, because there's no `python` by default.

## Saving the results page

1. Finish the practice assessment.
2. Open the **Answer Summary** view, where every question is listed with your answer and the correct one.
3. Press **Ctrl+S** (Windows) or **Cmd+S** (macOS) and save it as "Webpage, Complete" or "Webpage, HTML Only".

The score and domain bars are saved with the page even though they're hidden behind the Answer Summary, so there's no need to save the report view separately.

## Command-line usage

Run it from the study repo where the results should go, for example `az104`. Results are written to `practice-test/results/` under the current folder; use `--results-dir` to pick another folder.

Windows (adjust the path to wherever this repo is cloned):

```bash
python C:\path\to\exam-results-scraper\scrape_results.py "C:\Users\nalli\Downloads\Your practice assessment results _ Microsoft Learn.html" --retake
```

macOS:

```bash
python3 ~/exam-results-scraper/scrape_results.py ~/Downloads/"Your practice assessment results _ Microsoft Learn.html" --retake
```

On macOS, leave `~/Downloads/` outside the quotes so the shell expands it.

Output:

```
45/50 (90.0%) -> ...\practice-test\results\ms-21-2026-10-02.json
missed: Q27, Q32, Q34, Q43, Q46
```

### Options

| Option | What it does |
|---|---|
| `--first-attempt` | Records `"first_attempt": true`, meaning the first time you've seen this question set. |
| `--retake` | Records `"first_attempt": false`. Without either flag the field is `null` and the script prints a reminder. |
| `--date YYYY-MM-DD` | Sets the attempt date. The default is today. |
| `--provider TAG` | Overrides the provider tag used in the file name. |
| `--out PATH` | Writes to a specific path rather than the default name. |
| `--results-dir DIR` | Folder for results files, also searched by the duplicate check. The default is `practice-test/results` under the current folder. |
| `--force` | Overwrites an existing file, including one that already holds this attempt. |

## Output files

Files are named `<results-dir>/<provider>-<assessment>-<date>.json`, for example `ms-21-2026-10-02.json`.

- **Provider** comes from the page URL: `ms` for Microsoft Learn, `whizlabs`, or `tutorialsdojo`. For other sites it's the first part of the domain name.
- **Assessment** is the `assessmentId` from the URL. It's left out when the URL doesn't have one.
- **A second attempt on the same day** gets `-2`, then `-3`, and so on. Existing files are never overwritten unless you pass `--force`.
- **The same attempt twice:** every attempt has a unique `snapshot_id` in its URL. If a file with that ID already exists, the script stops and names the file. With `--force`, it replaces that file.

## JSON layout

```jsonc
{
  "attempt_date": "2026-10-02",
  "source_file": "Your practice assessment results _ Microsoft Learn.html",
  "source": {
    "site": "Microsoft Learn",
    "url": "https://learn.microsoft.com/...results?assessmentId=21&...&snapshotId=...",
    "canonical_url": "https://learn.microsoft.com/.../practice/results",
    "page_title": "Your practice assessment results | Microsoft Learn",
    "certification": "azure-administrator",
    "assessment_id": "21",
    "snapshot_id": "95842057-a654-414b-bce2-7a568c9fe8b1"
  },
  "first_attempt": false,
  "parsed_at": "2026-10-02T10:10:35",
  "score": { "correct": 45, "total": 50, "percent": 90.0, "page_reported": "90%" },
  "domains": [
    { "name": "Implement and manage virtual networking", "percent": 80, "target": 80, "meets_target": true }
  ],
  "missed": [27, 32, 34, 43, 46],
  "questions": [
    {
      "number": 1,
      "question": "You have an Azure subscription. ...",
      "your_answer": ["The user is a guest in the tenant."],
      "correct_answer": ["The user is a guest in the tenant."],
      "is_correct": true,
      "objective": null,
      "explanation": "For guest users, the user principal name (UPN) ...",
      "references": [{ "title": "B2B collaboration overview ...", "url": "https://learn.microsoft.com/..." }]
    }
  ]
}
```

**Where the values come from:**
- **`score`:** `correct`, `total` and `percent` are counted from the questions. `page_reported` is the score the page itself shows.
- **`domains`:** read from the score report's bars. Each has your percentage and the target. A score equal to the target counts as meeting it.
- **`your_answer` and `correct_answer`:** these are lists, because some questions have several correct answers. A question counts as correct only when every answer you chose is right and you chose all of them.
- **`objective`:** filled only when the explanation includes an "Objective:" line, which some newer questions have.
- **`source`:** read from the "saved from" note that Chrome and Edge add to saved pages. If that's missing, the script falls back to the page's canonical link and title.

## Plain-text input

A `.txt` copy of the Answer Summary also works:

```bash
python3 ~/exam-results-scraper/scrape_results.py practice-test/az-104-practice-1.txt --date 2026-09-28 --retake
```

On Windows, use `python` and the Windows path to the script.

Text copies have no URL or page data, so `source` and `domains` are `null`, reference links have no `url`, and the file is named `unknown-<date>.json`. Use `--provider` and `--out` to name it yourself.

## Tutorials Dojo

Tutorials Dojo review pages are detected automatically, so the command is the same. Save the page in **Review Mode** after finishing the set, so every question shows your answer and the explanation.

The JSON layout matches the Microsoft Learn one, with these differences:

- **`score.points`:** Tutorials Dojo scores by points, for example `{"earned": 59, "possible": 81}`. A Yes/No grid or a drag-to-match question is worth one point per row, so `page_reported` (the points percentage) differs from `percent`, which counts fully correct questions.
- **`domains`:** the category percentages from the results panel. Tutorials Dojo has a single pass mark for the whole set (70%), which is used as every domain's `target`.
- **Each question** also has `type` (`single`, `multiple`, `laq_hotspot_question`, `laq_jumbled_sentence` or `matrix_sort_answer`) and `domain`, the question's category.
- **Grid, dropdown and matching answers** are listed as `"prompt: answer"`, one entry per row or blank.
- **`source`** gains `assessment_name` (for example "Review Mode Set 1 - AZ-104 Azure Administrator"). `assessment_id` is the quiz ID, and `certification` is the course slug from the URL.

Some answers aren't in the saved page, and those entries are `null`. The script names the affected questions when it finishes.

- **Dropdown questions:** the page keeps the correct choice but not yours, so a blank you got wrong has `null` in `your_answer`.
- **Drag-to-match questions:** the page keeps where you placed each item but not the right match, so a row you got wrong has `null` in `correct_answer`. The explanation usually gives the right order.

You can fill these gaps by hand afterwards, from a screenshot of the question or from its explanation. Add `"filled_in": {"your_answer": "screenshot"}` (or `"correct_answer": "explanation"`) to that question so it's clear which values didn't come from the page. Re-running the script with `--force` replaces the file and loses those edits.

## Limitations

- **Microsoft Learn and Tutorials Dojo are supported.** WhizLabs gets the right provider tag in the file name, but its question layout isn't parsed yet. A saved WhizLabs results page is needed to add support.
- **Save the Answer Summary view** on Microsoft Learn. A page saved before the assessment finished, or from a different view, has no "Question N of M" headings, and the script exits with an error.
- **Tutorials Dojo pages have no attempt ID,** so the duplicate-snapshot check doesn't apply to them. Saving the same attempt twice on one day gives a `-2` file instead.
