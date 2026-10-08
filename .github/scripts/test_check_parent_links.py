import os
import unittest
from unittest import mock

import check_parent_links
from check_parent_links import links_own_issue, parent_closes


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


class LinksOwnIssueTest(unittest.TestCase):
    def test_closing_or_part_of_an_issue_in_this_repository_counts(self):
        for text in [
            "Closes #14",
            "fixes: #14",
            "Part of #14",
            "Resolves crf04/statsplus-backend#14",
            "Closes https://github.com/crf04/statsplus-backend/issues/14",
        ]:
            with self.subTest(text=text):
                self.assertTrue(links_own_issue(text, "crf04/statsplus-backend"))

    def test_parent_other_repositories_and_bare_mentions_do_not_count(self):
        for text in [
            "Part of crf04/statsplus#107",
            "Closes crf04/statsplus-mcp#14",
            "Merge after #14",
            "See #14",
        ]:
            with self.subTest(text=text):
                self.assertFalse(links_own_issue(text, "crf04/statsplus-backend"))


class MainTest(unittest.TestCase):
    def run_main(self, texts):
        original = check_parent_links.pull_request_texts
        check_parent_links.pull_request_texts = lambda: texts
        try:
            with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "crf04/statsplus-backend"}):
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

    def test_a_parent_link_without_a_child_issue_fails_the_check(self):
        texts = [
            ("title", "Add X"),
            ("body", "Part of crf04/statsplus#107"),
            ("commits", "Add X"),
        ]
        self.assertEqual(self.run_main(texts), 1)

    def test_partial_progress_on_a_child_issue_passes_the_check(self):
        texts = [
            ("title", "Add X"),
            ("body", "Part of #14\nPart of crf04/statsplus#107"),
            ("commits", "Add X"),
        ]
        self.assertEqual(self.run_main(texts), 0)

    def test_a_pull_request_without_a_parent_needs_no_child_issue(self):
        texts = [("title", "Bump a dependency"), ("body", ""), ("commits", "Bump")]
        self.assertEqual(self.run_main(texts), 0)


if __name__ == "__main__":
    unittest.main()
