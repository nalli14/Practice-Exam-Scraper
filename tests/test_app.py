"""Smoke tests for the parser and the web app. Run with: python -m unittest"""

import io
import json
import unittest

from app import app
from scrape_results import ParseError, file_stem, fill_gaps, parse_results

# A two-question Answer Summary in the plain-text layout Microsoft Learn copies out as.
SAMPLE_TXT = """\
Question 1 of 2
Which service balances HTTP traffic by URL path?
Your Answer
Azure Load Balancer
This answer is incorrect.
Correct Answer
Azure Application Gateway
This answer is correct.
Application Gateway is a layer 7 load balancer.
Question 2 of 2
Which record verifies a custom domain?
Your Answer
TXT
This answer is correct.
Correct Answer
TXT
This answer is correct.
Use a TXT or MX record.
"""

# One Tutorials Dojo (LearnDash) dropdown question, answered wrong. Like a real saved
# page, it records the correct choice but not the one picked.
SAMPLE_LEARNDASH = """\
<ol class="wpProQuiz_list"><li class="wpProQuiz_listItem" data-type="laq_jumbled_sentence">
<div class="wpProQuiz_question_page" style="display:none;">Question <span>1</span> of <span>1</span></div>
<div>Category: <span>AZ-104 – Implement and Manage Storage</span></div>
<div class="wpProQuiz_question"><div class="wpProQuiz_question_text">
<p>Which authentication methods can AzCopy use?</p></div>
<ul class="wpProQuiz_questionList" data-type="laq_jumbled_sentence"><div><p>
Blob storage <select class="laq_jumbled_sentence_dropdown wpProQuiz_answerIncorrect">
<option value=""></option><option value="API key">API key</option>
<option value="Shared access signature">Shared access signature</option></select>
<span class="laq_jumbled_sentence_correct_ans">(Shared access signature)</span>
<input type="hidden" class="laq_jumbled_sentence_correct" value="Shared access signature">
</p></div></ul></div>
<div class="wpProQuiz_response">
<div style="display: none;" class="wpProQuiz_correct"><p class="wpProQuiz_AnswerMessage"></p></div>
<div style="" class="wpProQuiz_incorrect"><p class="wpProQuiz_AnswerMessage">
<p>AzCopy supports SAS tokens and Entra ID.</p></p></div></div>
</li></ol>
"""


class ParserTests(unittest.TestCase):
    def test_text_answer_summary(self):
        result, warnings, unrecorded = parse_results(SAMPLE_TXT, "sample.txt", "2026-10-04", True)
        self.assertEqual(result["score"]["correct"], 1)
        self.assertEqual(result["score"]["total"], 2)
        self.assertEqual(result["missed"], [1])
        self.assertTrue(result["first_attempt"])
        self.assertEqual(result["questions"][0]["correct_answer"], ["Azure Application Gateway"])
        self.assertEqual(warnings, [])
        self.assertEqual(unrecorded, {})
        self.assertEqual(file_stem(result), "unknown-2026-10-04")

    def test_learndash_gap_and_fill(self):
        result, _, unrecorded = parse_results(SAMPLE_LEARNDASH, "td.html", "2026-10-04")
        q = result["questions"][0]
        self.assertEqual(q["correct_answer"], ["Blob storage: Shared access signature"])
        self.assertEqual(q["your_answer"], [None])
        self.assertEqual(unrecorded, {1: [{"field": "your_answer", "index": 0, "prompt": "Blob storage",
                                           "choices": ["API key", "Shared access signature"]}]})
        fill_gaps(result, [{"number": 1, "field": "your_answer", "index": 0,
                            "prompt": "Blob storage", "value": "API key"}])
        self.assertEqual(q["your_answer"], ["Blob storage: API key"])
        self.assertEqual(q["filled_in"], {"your_answer": "by hand"})
        # An entry the page did record can't be overwritten.
        with self.assertRaises(ValueError):
            fill_gaps(result, [{"number": 1, "field": "correct_answer", "index": 0, "value": "x"}])

    def test_not_a_results_page(self):
        with self.assertRaises(ParseError):
            parse_results("<html><body>hello</body></html>", "page.html", "2026-10-04")


class AppTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def post(self, data, name="sample.txt", **form):
        files = {"page": (io.BytesIO(data.encode()), name)}
        return self.client.post("/api/parse", data={**files, **form},
                                content_type="multipart/form-data")

    def test_index_and_health(self):
        with self.client.get("/") as res:
            self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get("/healthz").get_json(), {"status": "ok"})

    def test_parse_returns_file(self):
        res = self.post(SAMPLE_TXT, attempt="retake", date="2026-10-04")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["filename"], "unknown-2026-10-04.json")
        saved = json.loads(body["file_text"])
        self.assertIs(saved["first_attempt"], False)
        self.assertEqual(saved["score"]["correct"], 1)

    def test_fill_endpoint(self):
        parsed = self.post(SAMPLE_LEARNDASH, name="td.html").get_json()
        self.assertEqual(parsed["unrecorded"], [1])
        self.assertEqual(parsed["gaps"][0]["slots"][0]["choices"], ["API key", "Shared access signature"])
        res = self.client.post("/api/fill", json={"result": parsed["result"], "fills": [
            {"number": 1, "field": "your_answer", "index": 0, "prompt": "Blob storage", "value": "API key"}]})
        self.assertEqual(res.status_code, 200)
        saved = json.loads(res.get_json()["file_text"])
        self.assertEqual(saved["questions"][0]["your_answer"], ["Blob storage: API key"])
        self.assertEqual(self.client.post("/api/fill", json={}).status_code, 400)

    def test_rejects_bad_input(self):
        self.assertEqual(self.client.post("/api/parse").status_code, 400)
        self.assertEqual(self.post(SAMPLE_TXT, name="notes.pdf").status_code, 400)
        self.assertEqual(self.post(SAMPLE_TXT, date="04/10/2026").status_code, 400)
        self.assertEqual(self.post("<p>hi</p>", name="page.html").status_code, 422)


if __name__ == "__main__":
    unittest.main()
