import unittest

import check_parent_links
from check_parent_links import parent_closes


class ParentClosesTest(unittest.TestCase):
    def test_closing_keywords_aimed_at_the_parent_are_found(self):
        text = (
            "Fixes crf04/statsplus#111\n"
            "closed: https://github.com/crf04/statsplus/issues/40\n"
            "RESOLVES crf04/statsplus#7"
        )
        self.assertEqual(
            parent_closes(text),
            [
                "Fixes crf04/statsplus#111",
                "closed: https://github.com/crf04/statsplus/issues/40",
                "RESOLVES crf04/statsplus#7",
            ],
        )

    def test_child_links_and_parent_mentions_pass(self):
        text = (
            "Closes #160\n"
            "Part of crf04/statsplus#111\n"
            "Closes crf04/statsplus-backend#12\n"
            "Merge after: crf04/statsplus-frontend#3\n"
            "fixed the close button for crf04/statsplus#9 users"
        )
        self.assertEqual(parent_closes(text), [])


class MainTest(unittest.TestCase):
    def run_main(self, texts):
        original = check_parent_links.pull_request_texts
        check_parent_links.pull_request_texts = lambda: texts
        try:
            return check_parent_links.main()
        finally:
            check_parent_links.pull_request_texts = original

    def test_a_parent_close_in_any_commit_fails_the_check(self):
        texts = [
            ("title", "Add X"),
            ("body", "Part of crf04/statsplus#5"),
            ("commits", "Fixes crf04/statsplus#5"),
        ]
        self.assertEqual(self.run_main(texts), 1)

    def test_child_links_only_pass_the_check(self):
        texts = [
            ("title", "Add X"),
            ("body", "Closes #9\nPart of crf04/statsplus#5"),
            ("commits", "Add X"),
        ]
        self.assertEqual(self.run_main(texts), 0)


if __name__ == "__main__":
    unittest.main()
