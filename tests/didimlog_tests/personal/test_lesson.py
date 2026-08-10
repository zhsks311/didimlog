import tempfile
import unittest
from pathlib import Path

from didimlog.personal.lesson import (
    parse_frontmatter_text,
    parse_inline_list,
    parse_lesson,
    parse_lesson_text,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"
REQUIRED = ("topic", "title", "summary", "date")


def lesson_text(
    *,
    topic="testing",
    title="검증된 교훈",
    summary="검증된 내용만 저장한다",
    date="2026-08-07",
    extra_fields=(),
):
    fields = [
        "topic: {}".format(topic),
        "title: {}".format(title),
        "summary: {}".format(summary),
        "date: {}".format(date),
    ]
    fields.extend(extra_fields)
    return "---\n{}\n---\n## 상황\n본문\n".format("\n".join(fields))


class ParseInlineListTests(unittest.TestCase):
    def test_empty_and_quoted_scalar_lists_are_parsed_without_reordering(self):
        self.assertEqual(parse_inline_list("[]"), [])
        self.assertEqual(
            parse_inline_list(" [ alpha, '한글', \"quoted\" ] "),
            ["alpha", "한글", "quoted"],
        )

    def test_canonical_order_is_utf8_byte_order(self):
        self.assertEqual(
            parse_inline_list("[alpha, zeta, 가]", canonical=True),
            ["alpha", "zeta", "가"],
        )
        self.assertIsNone(parse_inline_list("[가, alpha, zeta]", canonical=True))
        self.assertEqual(
            parse_inline_list("[가, alpha, zeta]", canonical=False),
            ["가", "alpha", "zeta"],
        )

    def test_duplicate_policy_is_explicit(self):
        self.assertIsNone(parse_inline_list("[cache, cache]"))
        self.assertEqual(
            parse_inline_list("[cache, cache]", unique=False),
            ["cache", "cache"],
        )

    def test_index_term_scalar_and_utf8_byte_boundaries_are_inclusive(self):
        accepted = (
            "a" * 32,
            "가" * 32,
            "😀" * 24,
        )
        rejected = (
            "a" * 33,
            "가" * 33,
            "😀" * 25,
        )

        for value in accepted:
            with self.subTest(kind="accepted", scalars=len(value)):
                self.assertEqual(parse_inline_list("[{}]".format(value)), [value])
        for value in rejected:
            with self.subTest(kind="rejected", scalars=len(value)):
                self.assertIsNone(parse_inline_list("[{}]".format(value)))

    def test_invalid_shape_control_characters_and_item_limit_are_rejected(self):
        invalid_values = (
            None,
            "",
            "alpha",
            "[alpha",
            "alpha]",
            "[alpha,,beta]",
            "[alpha\tbeta]",
            "[alpha\nbeta]",
            "[alpha[beta]",
            "[{}]".format(", ".join("t{}".format(index) for index in range(21))),
        )

        for raw in invalid_values:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_inline_list(raw))


class ParseFrontmatterTextTests(unittest.TestCase):
    def test_fields_preserve_source_order(self):
        text = """---
topic: parser
title: 단일 행 파서
summary: 필드 순서를 그대로 보존한다
tags: [parser]
date: 2026-08-07
review_by: 2026-12-31
---
본문
"""

        parsed = parse_frontmatter_text("lesson.md", text, REQUIRED)

        self.assertIsNotNone(parsed)
        fields, lines, closing = parsed
        self.assertEqual(
            list(fields),
            ["topic", "title", "summary", "tags", "date", "review_by"],
        )
        self.assertEqual(lines[closing], "---")
        self.assertEqual(closing, 7)

    def test_required_fields_must_be_present_and_nonempty(self):
        missing = lesson_text().replace("summary: 검증된 내용만 저장한다\n", "")
        empty = lesson_text().replace(
            "summary: 검증된 내용만 저장한다",
            "summary: ",
        )

        self.assertIsNone(parse_frontmatter_text("lesson.md", missing, REQUIRED))
        self.assertIsNone(parse_frontmatter_text("lesson.md", empty, REQUIRED))

    def test_duplicate_invalid_and_noncanonical_key_lines_are_rejected(self):
        invalid_lines = (
            "topic: duplicate",
            "1topic: invalid",
            "topic.name: invalid",
            "topic : invalid",
            "topic:invalid",
            " topic: invalid",
            "",
        )

        for line in invalid_lines:
            with self.subTest(line=line):
                text = lesson_text().replace(
                    "title: 검증된 교훈",
                    "{}\ntitle: 검증된 교훈".format(line),
                )
                self.assertIsNone(parse_frontmatter_text("lesson.md", text, REQUIRED))

    def test_frontmatter_requires_bare_markdown_filename_and_exact_delimiters(self):
        valid = lesson_text()
        invalid_names = (
            "nested/lesson.md",
            "lesson.txt",
            "lesson.md/child.md",
        )
        invalid_texts = (
            valid.removeprefix("---\n"),
            valid.replace("---\n", "--\n", 1),
            valid.replace("\n---\n## 상황", "\n## 상황"),
        )

        for name in invalid_names:
            with self.subTest(name=name):
                self.assertIsNone(parse_frontmatter_text(name, valid, REQUIRED))
        for text in invalid_texts:
            with self.subTest(text=text):
                self.assertIsNone(parse_frontmatter_text("lesson.md", text, REQUIRED))

    def test_crlf_and_indented_multiline_blocks_are_rejected(self):
        valid = lesson_text()
        multiline = (FIXTURES / "multiline-attempt.md").read_text(encoding="utf-8")

        self.assertIsNone(
            parse_frontmatter_text(
                "lesson.md",
                valid.replace("\n", "\r\n"),
                REQUIRED,
            )
        )
        self.assertIsNone(
            parse_frontmatter_text("multiline-attempt.md", multiline, REQUIRED)
        )


class ParseLessonTextTests(unittest.TestCase):
    def test_valid_fixture_matrix_is_accepted(self):
        valid_fixtures = (
            "demo-api-cache.md",
            "expired-boundary.md",
            "jpa-n-plus-one.md",
            "kafka-idempotence.md",
            "korean-lesson.md",
        )

        for name in valid_fixtures:
            with self.subTest(name=name):
                text = (FIXTURES / name).read_text(encoding="utf-8")
                self.assertIsNotNone(parse_lesson_text(name, text))

    def test_malformed_fixture_matrix_is_rejected(self):
        invalid_fixtures = (
            "malformed-no-close.md",
            "malformed-no-colon.md",
            "multiline-attempt.md",
            "other-project.md",
        )

        for name in invalid_fixtures:
            with self.subTest(name=name):
                text = (FIXTURES / name).read_text(encoding="utf-8")
                self.assertIsNone(parse_lesson_text(name, text))

    def test_project_and_unknown_fields_are_rejected(self):
        for field in ("project: old-project", "unexpected: value"):
            with self.subTest(field=field):
                parsed = parse_lesson_text(
                    "lesson.md",
                    lesson_text(extra_fields=(field,)),
                )

                self.assertIsNone(parsed)

    def test_title_scalar_boundary_is_inclusive_and_not_a_byte_limit(self):
        self.assertIsNotNone(
            parse_lesson_text("valid-title.md", lesson_text(title="가" * 120))
        )
        self.assertIsNone(
            parse_lesson_text("long-title.md", lesson_text(title="가" * 121))
        )

    def test_topic_uses_slug_and_index_term_limits(self):
        self.assertIsNotNone(
            parse_lesson_text("valid-topic.md", lesson_text(topic="a" * 32))
        )

        invalid_topics = (
            "a" * 33,
            "has space",
            "has_underscore",
            "-leading",
            "trailing-",
            "double--dash",
            "한글",
        )
        for topic in invalid_topics:
            with self.subTest(topic=topic):
                self.assertIsNone(
                    parse_lesson_text("invalid-topic.md", lesson_text(topic=topic))
                )

    def test_date_and_review_by_must_be_real_iso_calendar_dates(self):
        self.assertIsNotNone(
            parse_lesson_text(
                "leap-day.md",
                lesson_text(date="2024-02-29", extra_fields=("review_by: 2024-03-01",)),
            )
        )

        invalid_dates = (
            "2023-02-29",
            "2026-13-01",
            "2026-01-32",
            "2026-8-07",
            "07-08-2026",
        )
        for date in invalid_dates:
            with self.subTest(field="date", date=date):
                self.assertIsNone(
                    parse_lesson_text("invalid-date.md", lesson_text(date=date))
                )
            with self.subTest(field="review_by", date=date):
                self.assertIsNone(
                    parse_lesson_text(
                        "invalid-review.md",
                        lesson_text(extra_fields=("review_by: {}".format(date),)),
                    )
                )

    def test_title_and_summary_reject_block_markers_and_control_characters(self):
        block_markers = ("|", "|-", "|+", ">", ">-", ">+")
        for marker in block_markers:
            with self.subTest(field="title", marker=marker):
                self.assertIsNone(
                    parse_lesson_text("block-title.md", lesson_text(title=marker))
                )
            with self.subTest(field="summary", marker=marker):
                self.assertIsNone(
                    parse_lesson_text("block-summary.md", lesson_text(summary=marker))
                )

        self.assertIsNone(
            parse_lesson_text("tab-title.md", lesson_text(title="제목\t삽입"))
        )
        self.assertIsNone(
            parse_lesson_text("delete-summary.md", lesson_text(summary="요약\x7f삽입"))
        )

    def test_tags_must_be_canonical_and_unique(self):
        invalid_tags = (
            "tags: [zeta, alpha]",
            "tags: [alpha, zeta, alpha]",
        )

        for field in invalid_tags:
            with self.subTest(field=field):
                self.assertIsNone(
                    parse_lesson_text(
                        "invalid-tags.md",
                        lesson_text(extra_fields=(field,)),
                    )
                )

    def test_invalid_tags_are_rejected(self):
        invalid_tags = (
            "tags: zeta",
            "tags: [bad\tvalue]",
            "tags: [{}]".format("😀" * 25),
            "tags: [{}]".format(", ".join("t{}".format(index) for index in range(21))),
        )

        for field in invalid_tags:
            with self.subTest(field=field):
                self.assertIsNone(
                    parse_lesson_text(
                        "invalid-tags.md",
                        lesson_text(extra_fields=(field,)),
                    )
                )

    def test_booked_accepts_unsorted_unique_slugs(self):
        parsed = parse_lesson_text(
            "booked.md",
            lesson_text(extra_fields=("booked: [zeta, alpha]",)),
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0]["booked"], "[zeta, alpha]")

    def test_booked_rejects_duplicates_and_invalid_slugs(self):
        invalid_booked = (
            "booked: [same, same]",
            "booked: [valid, has_space]",
            "booked: [valid, has_underscore]",
            "booked: [한글]",
        )

        for field in invalid_booked:
            with self.subTest(field=field):
                self.assertIsNone(
                    parse_lesson_text(
                        "invalid-booked.md",
                        lesson_text(extra_fields=(field,)),
                    )
                )

    def test_crlf_is_rejected_even_when_all_values_are_valid(self):
        self.assertIsNone(
            parse_lesson_text(
                "crlf.md",
                lesson_text().replace("\n", "\r\n"),
            )
        )

    def test_filename_must_be_a_bare_slug_markdown_name(self):
        invalid_names = (
            "nested/lesson.md",
            "lesson.txt",
            "lesson.md.md",
            ".md",
            "has space.md",
            "has_underscore.md",
            "-leading.md",
            "trailing-.md",
            "double--dash.md",
            "한글.md",
        )

        for name in invalid_names:
            with self.subTest(name=name):
                self.assertIsNone(parse_lesson_text(name, lesson_text()))


class ParseLessonPathTests(unittest.TestCase):
    def test_path_parser_matches_text_parser_for_every_existing_fixture(self):
        for path in sorted(FIXTURES.glob("*.md")):
            with self.subTest(name=path.name):
                text = path.read_bytes().decode("utf-8")
                self.assertEqual(
                    parse_lesson(path),
                    parse_lesson_text(path.name, text),
                )

    def test_missing_and_non_utf8_files_return_none(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            invalid_utf8 = root / "invalid-utf8.md"
            invalid_utf8.write_bytes(b"\xff\xfe")

            self.assertIsNone(parse_lesson(root / "missing.md"))
            self.assertIsNone(parse_lesson(invalid_utf8))

    def test_path_parser_applies_filename_contract_to_basename(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "invalid_name.md"
            path.write_text(lesson_text(), encoding="utf-8")

            self.assertIsNone(parse_lesson(path))


if __name__ == "__main__":
    unittest.main()
