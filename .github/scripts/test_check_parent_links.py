import unittest

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


if __name__ == "__main__":
    unittest.main()
