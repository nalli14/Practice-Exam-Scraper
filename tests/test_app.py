"""Smoke tests for the parser and the web app. Run with: python -m unittest"""

import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import screenshot_reader
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

    def test_signed_in_user(self):
        self.assertEqual(self.client.get("/api/me").get_json(), {"name": None})
        res = self.client.get("/api/me", headers={"X-MS-CLIENT-PRINCIPAL-NAME": "me@example.com"})
        self.assertEqual(res.get_json(), {"name": "me@example.com"})

    @mock.patch.dict(os.environ, {"ALLOWED_USERS": "Nalli14, other@example.com"})
    def test_allow_list(self):
        signed_in = lambda name: {"X-MS-CLIENT-PRINCIPAL-NAME": name, "X-MS-CLIENT-PRINCIPAL-IDP": "github"}
        with self.client.get("/", headers=signed_in("nalli14")) as res:
            self.assertEqual(res.status_code, 200)
        res = self.client.get("/", headers=signed_in("<someone-else>"))
        self.assertEqual(res.status_code, 403)
        self.assertIn(b"(&lt;someone-else&gt;)", res.data)
        self.assertEqual(self.client.get("/api/me", headers=signed_in("someone-else")).status_code, 403)
        # Not signed in at all, or a name without App Service's provider header.
        self.assertEqual(self.client.get("/").status_code, 403)
        self.assertEqual(self.client.get("/", headers={"X-MS-CLIENT-PRINCIPAL-NAME": "nalli14"}).status_code, 403)
        self.assertEqual(self.client.get("/healthz").status_code, 200)

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


GAP = {"number": 1, "type": "laq_jumbled_sentence", "question": "Which methods?", "explanation": "",
       "slots": [{"field": "your_answer", "index": 0, "prompt": "Blob storage",
                  "choices": ["API key", "Shared access signature"]},
                 {"field": "your_answer", "index": 1, "prompt": "File storage",
                  "choices": ["API key", "Shared access signature"]}]}
PNG = b"\x89PNG\r\n\x1a\n fake image"


class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def post(self, gap=GAP, images=((PNG, "q1.png"),)):
        data = {"gap": json.dumps(gap),
                "screenshots": [(io.BytesIO(b), name) for b, name in images]}
        return self.client.post("/api/read-screenshot", data=data, content_type="multipart/form-data")

    @mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @mock.patch("screenshot_reader.anthropic.Anthropic")
    def test_reads_picks_from_claude(self, client_cls):
        create = client_cls.return_value.beta.messages.create
        create.return_value = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(
                {"slot_0": "API key", "slot_1": screenshot_reader.NOT_VISIBLE}))])
        res = self.post()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"picks": {"0": "API key"}})
        # The answer is constrained to the question's own choices.
        schema = create.call_args.kwargs["output_config"]["format"]["schema"]
        self.assertEqual(schema["properties"]["slot_0"]["enum"],
                         ["API key", "Shared access signature", screenshot_reader.NOT_VISIBLE])
        image = create.call_args.kwargs["messages"][0]["content"][0]
        self.assertEqual(image["source"]["media_type"], "image/png")

    @mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    @mock.patch("screenshot_reader.anthropic.Anthropic")
    def test_refusal_is_reported(self, client_cls):
        client_cls.return_value.beta.messages.create.return_value = SimpleNamespace(
            stop_reason="refusal", content=[])
        self.assertEqual(self.post().status_code, 422)

    @mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""})
    def test_not_configured(self):
        self.assertEqual(self.post().status_code, 503)
        parsed = self.client.post("/api/parse", data={"page": (io.BytesIO(SAMPLE_LEARNDASH.encode()), "td.html")},
                                  content_type="multipart/form-data").get_json()
        self.assertIs(parsed["screenshots"], False)

    def test_rejects_bad_uploads(self):
        self.assertEqual(self.post(gap={"slots": []}).status_code, 400)
        self.assertEqual(self.post(images=()).status_code, 400)
        self.assertEqual(self.post(images=((b"%PDF", "q1.pdf"),)).status_code, 400)
        self.assertEqual(self.post(images=[(PNG, f"q{i}.png") for i in range(5)]).status_code, 400)


if __name__ == "__main__":
    unittest.main()
