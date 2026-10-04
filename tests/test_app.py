"""Smoke tests for the parser and the web app. Run with: python -m unittest"""

import io
import json
import unittest

from app import app
from scrape_results import ParseError, file_stem, parse_results

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


class ParserTests(unittest.TestCase):
    def test_text_answer_summary(self):
        result, warnings, unrecorded = parse_results(SAMPLE_TXT, "sample.txt", "2026-10-04", True)
        self.assertEqual(result["score"]["correct"], 1)
        self.assertEqual(result["score"]["total"], 2)
        self.assertEqual(result["missed"], [1])
        self.assertTrue(result["first_attempt"])
        self.assertEqual(result["questions"][0]["correct_answer"], ["Azure Application Gateway"])
        self.assertEqual(warnings, [])
        self.assertEqual(unrecorded, [])
        self.assertEqual(file_stem(result), "unknown-2026-10-04")

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

    def test_rejects_bad_input(self):
        self.assertEqual(self.client.post("/api/parse").status_code, 400)
        self.assertEqual(self.post(SAMPLE_TXT, name="notes.pdf").status_code, 400)
        self.assertEqual(self.post(SAMPLE_TXT, date="04/10/2026").status_code, 400)
        self.assertEqual(self.post("<p>hi</p>", name="page.html").status_code, 422)


if __name__ == "__main__":
    unittest.main()
