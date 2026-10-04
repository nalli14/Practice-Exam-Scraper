#!/usr/bin/env python3
"""Parse a saved Microsoft Learn or Tutorials Dojo practice results page into JSON.

Usage:
    python scrape_results.py path/to/saved-page.html
    python scrape_results.py results.txt --date 2026-10-01 --out somewhere.json

Save the results page with Ctrl+S ("Webpage, Complete" or "HTML Only") after
finishing a practice assessment, then point this script at the .html file.
A plain-text copy of the "Answer Summary" (.txt) also works, minus link URLs.

Standard library only; no pip installs needed.
"""

import argparse
import datetime as dt
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Relative to the folder the script is run from, e.g. a certification study repo.
DEFAULT_RESULTS_DIR = Path("practice-test") / "results"

QUESTION_RE = re.compile(r"^Question (\d+) of (\d+)$")
MARKER_RE = re.compile(r"^This answer is (correct|incorrect)\.$")
PERCENT_RE = re.compile(r"\b(\d{1,3})\s?%")

# Links are carried through the text as LINK_OPEN url LINK_SEP text LINK_CLOSE.
LINK_OPEN, LINK_SEP, LINK_CLOSE = "\x01", "\x02", "\x03"
LINK_RE = re.compile(f"{LINK_OPEN}(.*?){LINK_SEP}(.*?){LINK_CLOSE}", re.S)

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "source", "track", "wbr"}
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "head"}
BLOCK_TAGS = {"address", "article", "aside", "blockquote", "br", "dd", "details",
              "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
              "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "label",
              "legend", "li", "main", "nav", "ol", "p", "pre", "section",
              "summary", "table", "td", "th", "tr", "ul", "button"}


# ---------------------------------------------------------------- HTML -> lines

class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.children = []
        self.parent = parent


class TreeBuilder(HTMLParser):
    """Builds a minimal, forgiving DOM tree from saved HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]
        self.saved_from = None

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs, self.stack[-1]))

    def handle_endtag(self, tag):
        # Pop back to the matching open tag; ignore stray end tags.
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_comment(self, data):
        # Chrome/Edge "Save as" stamps <!-- saved from url=(0200)https://... -->
        m = re.match(r"\s*saved from url=\(\d+\)(\S+)", data)
        if m and self.saved_from is None:
            self.saved_from = m.group(1)

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def render(node, out):
    """Append the visible text of node to out, with newlines at block edges."""
    if isinstance(node, str):
        out.append(re.sub(r"\s+", " ", node))
        return
    if node.tag in SKIP_TAGS or "hidden" in node.attrs \
            or node.attrs.get("aria-hidden") == "true":
        return
    block = node.tag in BLOCK_TAGS
    if block:
        out.append("\n")
    if node.tag == "a" and node.attrs.get("href"):
        inner = []
        for child in node.children:
            render(child, inner)
        text = " ".join("".join(inner).split())
        if text:
            out.append(f"{LINK_OPEN}{node.attrs['href']}{LINK_SEP}{text}{LINK_CLOSE}")
    else:
        for child in node.children:
            render(child, out)
    if block:
        out.append("\n")


def to_lines(node):
    out = []
    render(node, out)
    return [ln.strip() for ln in "".join(out).split("\n") if ln.strip()]


def own_text(node):
    return " ".join("".join(c for c in node.children if isinstance(c, str)).split())


def question_container(root):
    """Smallest element that contains every 'Question N of M' heading."""
    paths = []

    def walk(node):
        for child in node.children:
            if isinstance(child, Node):
                if QUESTION_RE.match(own_text(child)):
                    path, n = [], child
                    while n is not None:
                        path.append(n)
                        n = n.parent
                    paths.append(path[::-1])
                walk(child)

    walk(root)
    if not paths:
        return None
    common = paths[0]
    for path in paths[1:]:
        i = 0
        while i < min(len(common), len(path)) and common[i] is path[i]:
            i += 1
        common = common[:i]
    # With one question, the "common ancestor" is the heading itself; widen it.
    container = common[-1]
    if len(paths) == 1 and container.parent is not None:
        container = container.parent
    return container


def score_report(root):
    """Read the score report's <meter> bars, paired with their <label for=...>.

    The report view is hidden while the answer summary is open, so this reads
    attributes rather than rendered text. Returns (overall %, [domains]).
    """
    labels, meters = {}, []

    def walk(node):
        for child in node.children:
            if isinstance(child, Node):
                if child.tag == "label" and child.attrs.get("for"):
                    inner = []
                    render(child, inner)
                    labels[child.attrs["for"]] = " ".join("".join(inner).split())
                elif child.tag == "meter":
                    meters.append(child)
                walk(child)

    walk(root)
    overall, domains = None, []
    for m in meters:
        name = labels.get(m.attrs.get("id"))
        try:
            value = float(m.attrs["value"])
        except (KeyError, TypeError, ValueError):
            continue
        target = m.attrs.get("optimum") or m.attrs.get("low")
        value = int(value) if value.is_integer() else value
        if name is None:
            continue
        if name.lower().startswith("score"):
            overall = value
        else:
            domains.append({
                "name": name,
                "percent": value,
                "target": int(float(target)) if target else None,
                "meets_target": float(value) >= float(target) if target else None,
            })
    return overall, domains


def page_source(root, saved_from):
    """Where the page came from: site name, URL and assessment identifiers."""
    title = canonical = og_url = site_name = None

    def walk(node):
        nonlocal title, canonical, og_url, site_name
        for child in node.children:
            if not isinstance(child, Node):
                continue
            a = child.attrs
            if child.tag == "title" and title is None:
                title = " ".join("".join(c for c in child.children if isinstance(c, str)).split())
            elif child.tag == "link" and "canonical" in (a.get("rel") or "").split():
                canonical = canonical or a.get("href")
            elif child.tag == "meta" and a.get("property") == "og:url":
                og_url = og_url or a.get("content")
            elif child.tag == "meta" and a.get("property") == "og:site_name":
                site_name = site_name or a.get("content")
            walk(child)

    walk(root)
    url = saved_from or canonical or og_url
    parsed = urlparse(url) if url else None
    query = parse_qs(parsed.query) if parsed else {}
    cert = re.search(r"/certifications/([^/?#]+)", parsed.path) if parsed else None

    site = site_name
    if not site and title and "|" in title:
        site = title.rsplit("|", 1)[1].strip() or None
    if not site and parsed:
        site = parsed.hostname

    return {
        "site": site,
        "url": url,
        "canonical_url": canonical or og_url,
        "page_title": title,
        "certification": cert.group(1) if cert else None,
        "assessment_id": query.get("assessmentId", [None])[0],
        "snapshot_id": query.get("snapshotId", [None])[0],
    }


def load_html(text):
    builder = TreeBuilder()
    builder.feed(text)
    builder.close()
    root = builder.root
    page_lines = to_lines(root)
    container = question_container(root)
    question_lines = to_lines(container) if container else page_lines
    return page_lines, question_lines, score_report(root), page_source(root, builder.saved_from), root


# ------------------------------------------------------------ lines -> results

def split_links(line):
    """Return (plain text, [links]) for one line."""
    links = [{"title": t.strip(), "url": u} for u, t in LINK_RE.findall(line)]
    plain = LINK_RE.sub(lambda m: m.group(2), line).strip()
    return plain, links


def is_reference_line(plain, links):
    # A line that is nothing but a link, or a bare "... | Microsoft Learn" title (.txt input).
    if links and plain == " ".join(l["title"] for l in links):
        return True
    return not links and plain.endswith("| Microsoft Learn")


def parse_questions(lines):
    questions = []
    q = None
    section = None      # "question" | "yours" | "correct"
    pending = []        # option text waiting for its correct/incorrect marker

    def finish():
        if q is None:
            return
        build_explanation(q, pending)
        questions.append(q)

    for raw in lines:
        plain, _ = split_links(raw)
        m = QUESTION_RE.match(plain)
        if m:
            finish()
            q = {"number": int(m.group(1)), "of": int(m.group(2)),
                 "question": [], "your_answer": [], "correct_answer": [],
                 "_your_marks": []}
            section, pending = "question", []
            continue
        if q is None:
            continue
        if plain == "Your Answer":
            section, pending = "yours", []
            continue
        if plain == "Correct Answer":
            section, pending = "correct", []
            continue
        mk = MARKER_RE.match(plain)
        if mk and section in ("yours", "correct"):
            option = " ".join(split_links(p)[0] for p in pending).strip()
            if section == "yours":
                q["your_answer"].append(option)
                q["_your_marks"].append(mk.group(1) == "correct")
            else:
                q["correct_answer"].append(option)
            pending = []
            continue
        if section == "question":
            q["question"].append(plain)
        else:
            pending.append(raw)
    finish()
    return questions


def build_explanation(q, tail):
    """Everything after the last Correct Answer marker is explanation + references."""
    text, refs = [], []
    for raw in tail:
        plain, links = split_links(raw)
        if is_reference_line(plain, links):
            refs.extend(links or [{"title": plain, "url": None}])
        else:
            text.append(plain)
            refs.extend(links)

    objective = None
    for i, line in enumerate(text):
        if line.rstrip(":") == "Objective" and i + 1 < len(text):
            objective = text[i + 1]
            break

    marks = q.pop("_your_marks")
    q["question"] = "\n".join(q["question"])
    q["is_correct"] = bool(q["your_answer"]) and all(marks) \
        and sorted(q["your_answer"]) == sorted(q["correct_answer"])
    q["objective"] = objective
    q["explanation"] = "\n\n".join(text)
    q["references"] = refs


def page_reported_score(page_lines, question_lines):
    """Best-effort: a percentage shown on the page before the answer summary."""
    first_q = question_lines[0] if question_lines else None
    for raw in page_lines:
        plain, _ = split_links(raw)
        if plain == first_q or QUESTION_RE.match(plain):
            break
        m = PERCENT_RE.search(plain)
        if m and int(m.group(1)) <= 100:
            return f"{m.group(1)}%"
    return None


# ------------------------------------------------- LearnDash (Tutorials Dojo)
#
# Tutorials Dojo's review page is a LearnDash "wpProQuiz". Answers are read from
# the markup rather than the text: the option you picked has label.is-selected,
# correct options carry wpProQuiz_answerCorrect(Incomplete), and the visible
# wpProQuiz_correct / wpProQuiz_incorrect box says how the question was graded.

def classes(node):
    return set((node.attrs.get("class") or "").split())


def find_all(node, pred):
    found = []

    def walk(n):
        for child in n.children:
            if isinstance(child, Node):
                if pred(child):
                    found.append(child)
                walk(child)

    walk(node)
    return found


def find_class(node, cls):
    return find_all(node, lambda n: cls in classes(n))


def first_class(node, cls):
    hits = find_class(node, cls)
    return hits[0] if hits else None


def text_of(node):
    return " ".join(" ".join(to_lines(node)).split()) if node is not None else ""


def is_displayed(node):
    return "display:none" not in (node.attrs.get("style") or "").replace(" ", "")


def is_learndash(root):
    return bool(find_class(root, "wpProQuiz_listItem"))


def learndash_answers(item, qtype):
    """(your_answer, correct_answer, gaps) for one wpProQuiz_listItem.

    An answer the page doesn't record comes back as None in the list, and gaps
    describes each one so it can be filled in by hand: which list and index, the
    prompt it belongs to, and the choices the question offered.
    """
    yours, correct, gaps = [], [], []

    def gap(field, prompt, choices):
        index = len(yours if field == "your_answer" else correct)
        gaps.append({"field": field, "index": index, "prompt": prompt, "choices": choices})
        return None

    if qtype in ("single", "multiple"):
        for opt in find_class(item, "wpProQuiz_questionListItem"):
            label = next((n for n in find_all(opt, lambda n: n.tag == "label")), None)
            text = text_of(label)
            if label is not None and "is-selected" in classes(label):
                yours.append(text)
            if classes(opt) & {"wpProQuiz_answerCorrect", "wpProQuiz_answerCorrectIncomplete"}:
                correct.append(text)
    elif qtype == "laq_hotspot_question":
        # Yes/No grid: the hidden input holds the right answer, span.checked your pick.
        for row in find_class(item, "hotspot_question_row"):
            stmt_cell = first_class(row, "hotspot_question")
            stmt = text_of(stmt_cell)
            right = first_class(row, "hotspot_question_correct")
            picked = [n for n in find_class(row, "checked")]
            picked_input = find_all(picked[0], lambda n: n.tag == "input") if picked else []
            yours.append(f"{stmt}: {picked_input[0].attrs.get('value')}" if picked_input
                         else gap("your_answer", stmt, ["Yes", "No"]))
            correct.append(f"{stmt}: {right.attrs.get('value')}" if right is not None
                           else gap("correct_answer", stmt, ["Yes", "No"]))
    elif qtype == "laq_jumbled_sentence":
        # Dropdown blanks. A saved page keeps the correct value but not your choice,
        # so a blank you got wrong has no recorded answer.
        for sel in find_class(item, "laq_jumbled_sentence_dropdown"):
            siblings = sel.parent.children
            i = siblings.index(sel)
            # The blank's prompt is the text since the previous line break or blank.
            parts = []
            for prev in reversed(siblings[:i]):
                if isinstance(prev, str):
                    parts.append(prev)
                elif prev.tag in ("br", "select") or \
                        "laq_jumbled_sentence_correct_ans" in classes(prev):
                    break
                elif prev.tag != "input":
                    parts.append(text_of(prev))
            prompt = " ".join(" ".join(reversed(parts)).split()).rstrip(" :")
            right = next((n.attrs.get("value") for n in siblings[i + 1:]
                          if isinstance(n, Node) and "laq_jumbled_sentence_correct" in classes(n)), None)
            got_it = "wpProQuiz_answerCorrect" in classes(sel)
            choices = [o.attrs["value"].strip() for o in find_all(sel, lambda n: n.tag == "option")
                       if (o.attrs.get("value") or "").strip()]
            correct.append(f"{prompt}: {right}")
            yours.append(f"{prompt}: {right}" if got_it else gap("your_answer", prompt, choices))
    elif qtype == "matrix_sort_answer":
        # Drag-to-match rows. Only your placement is saved; for a row you got
        # wrong, the right match isn't in the page.
        choices = list(dict.fromkeys(text_of(n) for n in find_class(item, "wpProQuiz_sortStringItem")))
        for row in find_class(item, "wpProQuiz_questionListItem"):
            criterion = text_of(first_class(row, "wpProQuiz_maxtrixSortText"))
            placed = text_of(first_class(row, "wpProQuiz_maxtrixSortCriterion"))
            yours.append(f"{criterion}: {placed}")
            correct.append(f"{criterion}: {placed}" if "wpProQuiz_answerCorrect" in classes(row)
                           else gap("correct_answer", criterion, choices))
    return yours, correct, gaps


def learndash_explanation(item):
    """(explanation text, references) from the visible response box."""
    msgs = [m for m in find_class(item, "wpProQuiz_AnswerMessage") if to_lines(m)]
    lines = to_lines(msgs[0]) if msgs else []
    text, refs, in_refs = [], [], False
    for raw in lines:
        plain, links = split_links(raw)
        if plain.rstrip(":").lower() == "references":
            in_refs = True
            continue
        if in_refs or (links and plain == " ".join(l["title"] for l in links)):
            # Lead-ins such as "Check out this ... Cheat Sheet:" carry no link; drop them.
            refs.extend(links)
        else:
            text.append(plain)
    return "\n\n".join(text), refs


def parse_learndash(root):
    questions = []
    items = find_class(root, "wpProQuiz_listItem")
    for item in items:
        qtype = item.attrs.get("data-type")
        header = first_class(item, "wpProQuiz_question_page")
        m = re.search(r"Question (\d+) of (\d+)", text_of(header)) if header is not None else None
        number = int(m.group(1)) if m else len(questions) + 1
        domain = None
        for div in find_all(item, lambda n: n.tag == "div" and own_text(n).startswith("Category:")):
            domain = text_of(div).split(":", 1)[1].strip()
            break
        response = first_class(item, "wpProQuiz_response")
        graded_right = first_class(response, "wpProQuiz_correct") if response is not None else None
        yours, correct, gaps = learndash_answers(item, qtype)
        explanation, refs = learndash_explanation(item)
        questions.append({
            "number": number,
            "of": int(m.group(2)) if m else len(items),
            "type": qtype,
            "domain": domain,
            "question": "\n".join(to_lines(first_class(item, "wpProQuiz_question_text"))),
            "your_answer": yours,
            "correct_answer": correct,
            "is_correct": graded_right is not None and is_displayed(graded_right),
            "objective": None,
            "explanation": explanation,
            "references": refs,
            "_gaps": gaps,
        })
    return questions


def learndash_results(root):
    """(points dict, page-reported %, [domains]) from the quiz results panel."""
    results = first_class(root, "wpProQuiz_results")
    text = text_of(results)
    points = page_pct = None
    m = re.search(r"You have reached (\d+) of (\d+) point\(s\), \(\s*([\d.]+)%", text)
    if m:
        points = {"earned": int(m.group(1)), "possible": int(m.group(2))}
        page_pct = f"{m.group(3)}%"

    passing = None
    for script in find_all(root, lambda n: n.tag == "script"):
        pm = re.search(r"passingpercentage:\s*([\d.]+)", "".join(c for c in script.children if isinstance(c, str)))
        if pm:
            passing = float(pm.group(1))
            break

    domains = []
    for name_node in find_class(root, "wpProQuiz_catName"):
        pct_node = first_class(name_node.parent, "wpProQuiz_catPercent")
        pct =re.search(r"([\d.]+)\s?%", text_of(pct_node)) if pct_node is not None else None
        if not pct:
            continue
        value = float(pct.group(1))
        value = int(value) if value.is_integer() else value
        domains.append({
            "name": text_of(name_node),
            "percent": value,
            # Tutorials Dojo has one pass mark for the whole test, used here for every domain.
            "target": int(passing) if passing is not None and passing.is_integer() else passing,
            "meets_target": value >= passing if passing is not None else None,
        })
    return points, page_pct, domains


def learndash_source(root, source):
    """Fill in Tutorials Dojo identifiers: course slug and quiz ID."""
    url = source.get("url") or ""
    course = re.search(r"/courses/([^/?#]+)", url)
    quiz = re.search(r"/quizzes/([^/?#]+)", url)
    quiz_id = None
    for script in find_all(root, lambda n: n.tag == "script"):
        qm = re.search(r"\bquizId:\s*(\d+)", "".join(c for c in script.children if isinstance(c, str)))
        if qm:
            quiz_id = qm.group(1)
            break
    title = source.get("page_title") or ""
    if not source.get("site") or source["site"] == urlparse(url).hostname:
        source["site"] = "Tutorials Dojo"
    source["certification"] = source.get("certification") or (course.group(1) if course else None)
    source["assessment_id"] = source.get("assessment_id") or quiz_id
    source["assessment_name"] = re.sub(r"\s+-\s+Tutorials Dojo$", "", title) or \
        (quiz.group(1) if quiz else None)
    return source


# ---------------------------------------------------------------- output path

# Hostname suffix -> short provider tag used in file names.
PROVIDERS = {
    "learn.microsoft.com": "ms",
    "whizlabs.com": "whizlabs",
    "tutorialsdojo.com": "tutorialsdojo",
}


def provider_tag(source):
    host = urlparse(source["url"]).hostname if source and source.get("url") else None
    for suffix, tag in PROVIDERS.items():
        if host and (host == suffix or host.endswith("." + suffix)):
            return tag
    if host:
        return re.sub(r"^www\.", "", host).split(".")[0]
    return "unknown"


def find_snapshot(snapshot_id, results_dir):
    """Existing results file for this exact attempt, if any."""
    if not snapshot_id or not results_dir.is_dir():
        return None
    for f in sorted(results_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (data.get("source") or {}).get("snapshot_id") == snapshot_id:
            return f
    return None


def default_out_path(stem, results_dir):
    """<results_dir>/<stem>[-N].json, never clobbering."""
    out, n = results_dir / f"{stem}.json", 2
    while out.exists():
        out, n = results_dir / f"{stem}-{n}.json", n + 1
    return out


# --------------------------------------------------------------- parse a page

class ParseError(ValueError):
    """The input isn't a results page this script can read."""


def parse_results(text, filename, date, first_attempt=None):
    """Turn a saved results page into the results dict.

    Returns (result, warnings, unrecorded): warnings are problems worth showing, and
    unrecorded maps each question number whose answers the page doesn't keep to its
    gaps (see learndash_answers), for filling in with fill_gaps.
    """
    learndash = False
    if Path(filename).suffix.lower() in (".html", ".htm"):
        page_lines, question_lines, (meter_score, domains), source, root = load_html(text)
        learndash = is_learndash(root)
    else:
        page_lines = question_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        meter_score, domains = None, []
        source = None  # plain text carries no URL or title

    if learndash:
        questions = parse_learndash(root)
        points, learndash_pct, domains = learndash_results(root)
        source = learndash_source(root, source)
    else:
        questions = parse_questions(question_lines)
    if not questions:
        raise ParseError("No 'Question N of M' headings found. Is this the results page, "
                         "saved after the assessment finished?")

    warnings = []
    total = questions[0]["of"]
    correct = sum(q["is_correct"] for q in questions)
    numbers = [q["number"] for q in questions]
    if numbers != list(range(1, total + 1)):
        warnings.append(f"expected questions 1-{total}, parsed {len(numbers)}: "
                        f"missing {sorted(set(range(1, total + 1)) - set(numbers))}")
    unrecorded = {}
    for q in questions:
        del q["of"]
        gaps = q.pop("_gaps", [])
        if None in q["your_answer"] or None in q["correct_answer"]:
            unrecorded[q["number"]] = gaps
        if not q["your_answer"] or not q["correct_answer"]:
            warnings.append(f"question {q['number']} has no "
                            f"{'answer' if not q['your_answer'] else 'correct answer'} parsed")

    result = {
        "attempt_date": date,
        "source_file": Path(filename).name,
        "source": source,
        # Set by hand (--first-attempt / --retake); the page can't know. null = not recorded.
        "first_attempt": first_attempt,
        "parsed_at": dt.datetime.now().isoformat(timespec="seconds"),
        "score": {
            "correct": correct,
            "total": total,
            "percent": round(100 * correct / total, 1),
            "page_reported": learndash_pct if learndash
                             else f"{meter_score}%" if meter_score is not None
                             else page_reported_score(page_lines, question_lines),
        },
        # Per-domain bars from the score report (HTML input only).
        "domains": domains or None,
        "missed": [q["number"] for q in questions if not q["is_correct"]],
        "questions": questions,
    }
    if learndash:
        # Tutorials Dojo scores by points (a Yes/No grid is worth one per row), so the
        # page's percentage differs from the count of fully correct questions.
        result["score"]["points"] = points
    return result, warnings, unrecorded


FILLED_BY_HAND = "by hand"


def fill_gaps(result, fills):
    """Write hand-picked answers into the null entries of a parsed result.

    fills is a list of {"number", "field", "index", "prompt", "value"}. Only entries
    that are still null are filled; each filled question gets a "filled_in" note
    naming the lists that didn't come from the page. Returns how many were filled.
    """
    questions = {q["number"]: q for q in result["questions"]}
    filled = 0
    for f in fills:
        q = questions.get(f.get("number"))
        field, index, value = f.get("field"), f.get("index"), f.get("value")
        if q is None or field not in ("your_answer", "correct_answer") or not value \
                or not isinstance(index, int) or not 0 <= index < len(q[field]) \
                or q[field][index] is not None:
            raise ValueError(f"can't fill {field} {index} of question {f.get('number')}")
        q[field][index] = f"{f['prompt']}: {value}" if f.get("prompt") else value
        q.setdefault("filled_in", {})[field] = FILLED_BY_HAND
        filled += 1
    return filled


def file_stem(result, provider=None):
    """<provider>-<assessment>-<date>, the default results file name without .json."""
    source = result.get("source")
    assessment = (source or {}).get("assessment_id")
    return "-".join(x for x in (provider or provider_tag(source), assessment,
                                result["attempt_date"]) if x)


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", type=Path, help="saved results page (.html/.htm) or text copy (.txt)")
    ap.add_argument("--date", default=dt.date.today().isoformat(),
                    help="attempt date, YYYY-MM-DD (default: today)")
    ap.add_argument("--out", type=Path,
                    help="output JSON path (default: RESULTS_DIR/PROVIDER-ASSESSMENT-DATE.json)")
    ap.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR,
                    help="folder for results files and the duplicate check "
                         "(default: practice-test/results under the current folder)")
    ap.add_argument("--provider",
                    help="short provider tag for the file name (default: from the page URL, e.g. ms)")
    first = ap.add_mutually_exclusive_group()
    first.add_argument("--first-attempt", dest="first_attempt", action="store_true", default=None,
                       help="mark this as your first attempt at this question set")
    first.add_argument("--retake", dest="first_attempt", action="store_false",
                       help="mark this as a retake of a question set you've seen before")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing file (including one already holding this attempt)")
    args = ap.parse_args()

    try:
        dt.date.fromisoformat(args.date)
    except ValueError:
        ap.error(f"--date must be YYYY-MM-DD, got {args.date!r}")

    text = args.input.read_text(encoding="utf-8", errors="replace")
    try:
        result, warnings, unrecorded = parse_results(text, args.input.name, args.date,
                                                     args.first_attempt)
    except ParseError as e:
        sys.exit(str(e))
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    source, points = result["source"], result["score"].get("points")
    correct, total = result["score"]["correct"], result["score"]["total"]

    existing = find_snapshot((source or {}).get("snapshot_id"), args.results_dir)
    if existing and not args.force:
        sys.exit(f"This attempt (snapshot {source['snapshot_id']}) is already saved in "
                 f"{existing}; use --force to replace it.")
    out = args.out or existing or default_out_path(file_stem(result, args.provider), args.results_dir)
    if out.exists() and not args.force:
        sys.exit(f"{out} already exists; use --force to overwrite or --out to pick another path.")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"{correct}/{total} ({result['score']['percent']}%) -> {out}")
    if points:
        print(f"page score: {points['earned']}/{points['possible']} points "
              f"({result['score']['page_reported']})")
    if result["missed"]:
        print("missed: " + ", ".join(f"Q{n}" for n in result["missed"]))
    if unrecorded:
        print("note: the saved page doesn't record every answer for "
              + ", ".join(f"Q{n}" for n in unrecorded) + "; those entries are null.")
    if args.first_attempt is None:
        print("note: first_attempt not recorded; pass --first-attempt or --retake to set it.")


if __name__ == "__main__":
    main()
