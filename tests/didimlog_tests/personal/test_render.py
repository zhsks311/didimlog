import base64
import hashlib
import os
import stat
import tempfile
import unittest
from unittest import mock
from importlib import resources
from pathlib import Path

from didimlog.personal import render as render_module
from didimlog.personal.render import render_book


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
    "x8AAusB9Y9ZlJ8AAAAASUVORK5CYII="
)
PNG_BASE64 = base64.b64encode(PNG).decode("ascii")
PROJECT = "demo-api"
MERMAID_SHA256 = "70137e77bb273bb2ef972b86e8b0400cca8be53cb25bfc45911a186dc98665de"


def book_markdown(body, *, title="테스트 책", find_when="[render, test]"):
    return """---
title: {title}
find_when: {find_when}
---
{body}""".format(title=title, find_when=find_when, body=body)


class RenderBookTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data_root = self.root / "knowledge"
        self.project_book = self.data_root / "book" / PROJECT
        self.assets = self.project_book / "assets"
        self.assets.mkdir(parents=True)
        self.source = self.project_book / "test.md"
        self.output = self.project_book / "html" / "test.html"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_source(self, body, *, title="테스트 책", find_when="[render, test]"):
        self.source.write_text(
            book_markdown(body, title=title, find_when=find_when),
            encoding="utf-8",
            newline="\n",
        )

    def render(self):
        return render_book(
            self.source,
            self.output,
            project=PROJECT,
            data_root=self.data_root,
        )

    def make_symlink(self, link, target, *, target_is_directory=False):
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except (NotImplementedError, OSError) as error:
            self.skipTest("symlinks unavailable: {}".format(error))

    def test_renders_linked_book_project_and_returns_logical_output(self):
        external = self.root / "external-book"
        external_assets = external / "assets"
        external_assets.mkdir(parents=True)
        (external_assets / "pixel.png").write_bytes(PNG)
        self.assets.rmdir()
        self.project_book.rmdir()
        self.make_symlink(
            self.project_book,
            external,
            target_is_directory=True,
        )
        (external / "test.md").write_text(
            book_markdown("# linked\n\n![점](assets/pixel.png)"),
            encoding="utf-8",
            newline="\n",
        )

        rendered = self.render()

        external_output = external / "html" / "test.html"
        self.assertEqual(rendered, self.output)
        self.assertTrue(external_output.is_file())
        self.assertIn(
            "data:image/png;base64",
            external_output.read_text(encoding="utf-8"),
        )

    def test_link_change_after_output_prepare_leaves_no_rendered_output(self):
        external = self.root / "external-book"
        (external / "assets").mkdir(parents=True)
        replacement = self.root / "replacement-book"
        replacement.mkdir()
        self.assets.rmdir()
        self.project_book.rmdir()
        self.make_symlink(
            self.project_book,
            external,
            target_is_directory=True,
        )
        (external / "test.md").write_text(
            book_markdown("# linked\n"),
            encoding="utf-8",
            newline="\n",
        )
        real_prepare = render_module._prepare_output_directory

        def prepare_and_retarget(*args, **kwargs):
            descriptor = real_prepare(*args, **kwargs)
            self.project_book.unlink()
            os.symlink(
                replacement,
                self.project_book,
                target_is_directory=True,
            )
            return descriptor

        with mock.patch.object(
            render_module,
            "_prepare_output_directory",
            side_effect=prepare_and_retarget,
        ), self.assertRaisesRegex(
            ValueError,
            "project book link changed during render",
        ):
            self.render()

        self.assertFalse((external / "html" / "test.html").exists())
        self.assertFalse((replacement / "html" / "test.html").exists())
        self.assertEqual(list((external / "html").glob(".render-*")), [])

    def test_link_change_before_publish_leaves_no_rendered_output(self):
        external = self.root / "external-book"
        (external / "assets").mkdir(parents=True)
        replacement = self.root / "replacement-book"
        replacement.mkdir()
        self.assets.rmdir()
        self.project_book.rmdir()
        self.make_symlink(
            self.project_book,
            external,
            target_is_directory=True,
        )
        (external / "test.md").write_text(
            book_markdown("# linked\n"),
            encoding="utf-8",
            newline="\n",
        )
        real_replace = render_module._replace_output

        def retarget_and_publish(*args, **kwargs):
            self.project_book.unlink()
            os.symlink(
                replacement,
                self.project_book,
                target_is_directory=True,
            )
            return real_replace(*args, **kwargs)

        with mock.patch.object(
            render_module,
            "_replace_output",
            side_effect=retarget_and_publish,
        ), self.assertRaisesRegex(
            ValueError,
            "project book link changed during render",
        ):
            self.render()

        self.assertFalse((external / "html" / "test.html").exists())
        self.assertFalse((replacement / "html" / "test.html").exists())
        self.assertEqual(list((external / "html").glob(".render-*")), [])

    def test_link_change_before_publish_restores_existing_output_and_cleans_artifacts(
        self,
    ):
        external = self.root / "external-book"
        external_html = external / "html"
        external_html.mkdir(parents=True)
        replacement = self.root / "replacement-book"
        replacement.mkdir()
        existing_output = external_html / "test.html"
        existing_output.write_bytes(b"original rendered entry\n")
        existing_output.chmod(0o640)
        original_info = existing_output.lstat()
        self.assets.rmdir()
        self.project_book.rmdir()
        self.make_symlink(
            self.project_book,
            external,
            target_is_directory=True,
        )
        (external / "test.md").write_text(
            book_markdown("# linked\n"),
            encoding="utf-8",
            newline="\n",
        )
        real_replace = render_module._replace_output

        def retarget_and_publish(*args, **kwargs):
            self.project_book.unlink()
            os.symlink(
                replacement,
                self.project_book,
                target_is_directory=True,
            )
            return real_replace(*args, **kwargs)

        with mock.patch.object(
            render_module,
            "_replace_output",
            side_effect=retarget_and_publish,
        ), self.assertRaisesRegex(
            ValueError,
            "project book link changed during render",
        ):
            self.render()

        restored_info = existing_output.lstat()
        self.assertEqual(existing_output.read_bytes(), b"original rendered entry\n")
        self.assertEqual(
            (restored_info.st_dev, restored_info.st_ino, restored_info.st_mode),
            (original_info.st_dev, original_info.st_ino, original_info.st_mode),
        )
        self.assertEqual(stat.S_IMODE(restored_info.st_mode), 0o640)
        self.assertFalse((replacement / "html" / "test.html").exists())
        self.assertEqual(list(external_html.glob(".render-*")), [])

    def test_link_change_after_publish_preserves_concurrent_output(self):
        external = self.root / "external-book"
        external_html = external / "html"
        external_html.mkdir(parents=True)
        replacement = self.root / "replacement-book"
        replacement.mkdir()
        existing_output = external_html / "test.html"
        existing_output.write_bytes(b"original rendered entry\n")
        concurrent = b"user concurrent output\n"
        self.assets.rmdir()
        self.project_book.rmdir()
        self.make_symlink(
            self.project_book,
            external,
            target_is_directory=True,
        )
        (external / "test.md").write_text(
            book_markdown("# linked\n"),
            encoding="utf-8",
            newline="\n",
        )
        real_replace = render_module._replace_output

        def publish_retarget_and_save(*args, **kwargs):
            publication = real_replace(*args, **kwargs)
            existing_output.write_bytes(concurrent)
            self.project_book.unlink()
            os.symlink(
                replacement,
                self.project_book,
                target_is_directory=True,
            )
            return publication

        with mock.patch.object(
            render_module,
            "_replace_output",
            side_effect=publish_retarget_and_save,
        ), self.assertRaisesRegex(
            ValueError,
            "project book link changed during render",
        ):
            self.render()

        self.assertEqual(existing_output.read_bytes(), concurrent)
        self.assertFalse((replacement / "html" / "test.html").exists())
        self.assertEqual(list(external_html.glob(".render-*")), [])

    def test_publish_preserves_output_created_after_absent_check(self):
        external = self.root / "external-book"
        (external / "assets").mkdir(parents=True)
        self.assets.rmdir()
        self.project_book.rmdir()
        self.make_symlink(
            self.project_book,
            external,
            target_is_directory=True,
        )
        (external / "test.md").write_text(
            book_markdown("# linked\n"),
            encoding="utf-8",
            newline="\n",
        )
        concurrent_output = external / "html" / "test.html"
        concurrent = b"user created output\n"
        real_write_all = render_module._write_all

        def write_and_create_output(descriptor, data):
            real_write_all(descriptor, data)
            concurrent_output.write_bytes(concurrent)

        with mock.patch.object(
            render_module,
            "_write_all",
            side_effect=write_and_create_output,
        ), self.assertRaisesRegex(ValueError, "book output"):
            self.render()

        self.assertEqual(concurrent_output.read_bytes(), concurrent)
        self.assertEqual(list((external / "html").glob(".render-*")), [])

    def test_publish_preserves_output_created_after_existing_entry_is_backed_up(self):
        external = self.root / "external-book"
        external_html = external / "html"
        external_html.mkdir(parents=True)
        existing_output = external_html / "test.html"
        existing_output.write_bytes(b"original rendered entry\n")
        concurrent = b"user replacement output\n"
        self.assets.rmdir()
        self.project_book.rmdir()
        self.make_symlink(
            self.project_book,
            external,
            target_is_directory=True,
        )
        (external / "test.md").write_text(
            book_markdown("# linked\n"),
            encoding="utf-8",
            newline="\n",
        )
        real_rename = render_module.os.rename

        def backup_and_create_output(source, destination, *args, **kwargs):
            result = real_rename(source, destination, *args, **kwargs)
            if source == "test.html" and str(destination).endswith(".bak"):
                existing_output.write_bytes(concurrent)
            return result

        with mock.patch.object(
            render_module.os,
            "rename",
            side_effect=backup_and_create_output,
        ), self.assertRaisesRegex(ValueError, "book output"):
            self.render()

        self.assertEqual(existing_output.read_bytes(), concurrent)
        self.assertEqual(list(external_html.glob(".render-*")), [])

    def test_rollback_preserves_output_created_after_current_entry_check(self):
        external = self.root / "external-book"
        (external / "assets").mkdir(parents=True)
        replacement = self.root / "replacement-book"
        replacement.mkdir()
        self.assets.rmdir()
        self.project_book.rmdir()
        self.make_symlink(
            self.project_book,
            external,
            target_is_directory=True,
        )
        (external / "test.md").write_text(
            book_markdown("# linked\n"),
            encoding="utf-8",
            newline="\n",
        )
        concurrent_output = external / "html" / "test.html"
        concurrent = b"user created during rollback\n"
        real_replace = render_module._replace_output
        real_stat = render_module.os.stat
        retargeted = False
        replaced = False

        def publish_and_retarget(*args, **kwargs):
            nonlocal retargeted
            publication = real_replace(*args, **kwargs)
            self.project_book.unlink()
            os.symlink(
                replacement,
                self.project_book,
                target_is_directory=True,
            )
            retargeted = True
            return publication

        def stat_and_replace_output(path, *args, **kwargs):
            nonlocal replaced
            info = real_stat(path, *args, **kwargs)
            if (
                retargeted
                and not replaced
                and path == "test.html"
                and kwargs.get("dir_fd") is not None
            ):
                concurrent_output.unlink()
                concurrent_output.write_bytes(concurrent)
                replaced = True
            return info

        with mock.patch.object(
            render_module,
            "_replace_output",
            side_effect=publish_and_retarget,
        ), mock.patch.object(
            render_module.os,
            "stat",
            side_effect=stat_and_replace_output,
        ), self.assertRaisesRegex(
            ValueError,
            "project book link changed during render",
        ):
            self.render()

        self.assertEqual(concurrent_output.read_bytes(), concurrent)
        self.assertFalse((replacement / "html" / "test.html").exists())
        self.assertEqual(list((external / "html").glob(".render-*")), [])

    def test_rollback_preserves_output_replaced_after_current_entry_check(self):
        external = self.root / "external-book"
        external_html = external / "html"
        external_html.mkdir(parents=True)
        existing_output = external_html / "test.html"
        existing_output.write_bytes(b"original rendered entry\n")
        replacement = self.root / "replacement-book"
        replacement.mkdir()
        concurrent = b"user replaced during rollback\n"
        self.assets.rmdir()
        self.project_book.rmdir()
        self.make_symlink(
            self.project_book,
            external,
            target_is_directory=True,
        )
        (external / "test.md").write_text(
            book_markdown("# linked\n"),
            encoding="utf-8",
            newline="\n",
        )
        real_replace = render_module._replace_output
        real_stat = render_module.os.stat
        retargeted = False
        replaced = False

        def publish_and_retarget(*args, **kwargs):
            nonlocal retargeted
            publication = real_replace(*args, **kwargs)
            self.project_book.unlink()
            os.symlink(
                replacement,
                self.project_book,
                target_is_directory=True,
            )
            retargeted = True
            return publication

        def stat_and_replace_output(path, *args, **kwargs):
            nonlocal replaced
            info = real_stat(path, *args, **kwargs)
            if (
                retargeted
                and not replaced
                and path == "test.html"
                and kwargs.get("dir_fd") is not None
            ):
                existing_output.unlink()
                existing_output.write_bytes(concurrent)
                replaced = True
            return info

        with mock.patch.object(
            render_module,
            "_replace_output",
            side_effect=publish_and_retarget,
        ), mock.patch.object(
            render_module.os,
            "stat",
            side_effect=stat_and_replace_output,
        ), self.assertRaisesRegex(
            ValueError,
            "project book link changed during render",
        ):
            self.render()

        self.assertEqual(existing_output.read_bytes(), concurrent)
        self.assertFalse((replacement / "html" / "test.html").exists())
        self.assertEqual(list(external_html.glob(".render-*")), [])

    def test_backup_move_failure_after_concurrent_delete_does_not_restore_placeholder(
        self,
    ):
        external = self.root / "external-book"
        external_html = external / "html"
        external_html.mkdir(parents=True)
        existing_output = external_html / "test.html"
        existing_output.write_bytes(b"original rendered entry\n")
        self.assets.rmdir()
        self.project_book.rmdir()
        self.make_symlink(
            self.project_book,
            external,
            target_is_directory=True,
        )
        (external / "test.md").write_text(
            book_markdown("# linked\n"),
            encoding="utf-8",
            newline="\n",
        )
        real_rename = render_module.os.rename

        def delete_before_backup_move(source, destination, *args, **kwargs):
            if source == "test.html" and str(destination).endswith(".bak"):
                os.unlink(source, dir_fd=kwargs["src_dir_fd"])
            return real_rename(source, destination, *args, **kwargs)

        with mock.patch.object(
            render_module.os,
            "rename",
            side_effect=delete_before_backup_move,
        ), self.assertRaisesRegex(ValueError, "book output"):
            self.render()

        self.assertFalse(existing_output.exists())
        self.assertEqual(list(external_html.glob(".render-*")), [])

    def test_renders_self_contained_html_with_markdown_table_mermaid_and_image(self):
        (self.assets / "pixel.png").write_bytes(PNG)
        self.write_source(
            """# 테스트 책

| 항목 | 값 |
|---|---:|
| 테스트 | 1 |

```mermaid
graph LR
  A --> B
```

![점](assets/pixel.png)
"""
        )

        rendered = self.render()

        self.assertEqual(rendered, self.output)
        document = rendered.read_text(encoding="utf-8")
        self.assertTrue(document.startswith("<!doctype html>"))
        self.assertIn("<style>", document)
        self.assertNotIn('<link rel="stylesheet"', document)
        self.assertNotIn("<script src=", document)
        self.assertIn("<table>", document)
        self.assertIn('<pre class="mermaid">graph LR', document)
        self.assertIn("mermaid.initialize({startOnLoad:true,securityLevel:'strict'", document)
        self.assertIn(
            'src="data:image/png;base64,{}"'.format(PNG_BASE64),
            document,
        )
        self.assertNotIn('src="assets/pixel.png"', document)

    def test_markdown_code_and_mermaid_text_are_escaped(self):
        self.write_source(
            """# escaping

Inline code: `<unsafe>& value`.

```html
<script>alert(1)</script>
```

```mermaid
graph LR
  A["</pre><script>alert(2)</script>"] --> B
```
"""
        )

        document = self.render().read_text(encoding="utf-8")

        self.assertIn("<code>&lt;unsafe&gt;&amp; value</code>", document)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", document)
        self.assertIn("&lt;/pre&gt;&lt;script&gt;alert(2)&lt;/script&gt;", document)
        self.assertNotIn("<script>alert(1)</script>", document)
        self.assertNotIn("<script>alert(2)</script>", document)

    def test_raw_html_outside_a_fence_is_rejected(self):
        self.write_source("# unsafe\n\n<script>alert(1)</script>\n")

        with self.assertRaisesRegex(ValueError, "raw HTML"):
            self.render()

        self.assertFalse(self.output.exists())

    def test_block_raw_html_with_or_without_markdown_attribute_is_rejected(self):
        for raw_html in (
            "<div>\nunsafe\n</div>",
            '<div markdown="1">\n**unsafe**\n</div>',
        ):
            with self.subTest(raw_html=raw_html):
                self.write_source("# unsafe\n\n{}\n".format(raw_html))

                with self.assertRaisesRegex(ValueError, "raw HTML"):
                    self.render()

                self.assertFalse(self.output.exists())

    def test_unsafe_markdown_link_schemes_are_rejected(self):
        for destination in (
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox(1)",
        ):
            with self.subTest(destination=destination):
                self.write_source("# unsafe\n\n[실행]({})\n".format(destination))

                with self.assertRaisesRegex(ValueError, "unsafe link"):
                    self.render()

                self.assertFalse(self.output.exists())

    def test_safe_relative_web_and_mail_links_are_preserved(self):
        self.write_source(
            "# links\n\n[내부](chapter.md) [웹](https://example.com) "
            "[메일](mailto:reader@example.invalid)\n"
        )

        document = self.render().read_text(encoding="utf-8")

        self.assertIn('href="chapter.md"', document)
        self.assertIn('href="https://example.com"', document)
        self.assertIn('href="mailto:reader@example.invalid"', document)

    def test_frontmatter_is_removed_title_is_escaped_and_project_source_is_shown(self):
        self.write_source(
            "본문만 렌더합니다.\n",
            title="프로젝트 <책> & 렌더",
            find_when="[book, project]",
        )

        document = self.render().read_text(encoding="utf-8")

        self.assertIn(
            "<title>프로젝트 &lt;책&gt; &amp; 렌더</title>",
            document,
        )
        self.assertIn("원천: book/demo-api/test.md", document)
        self.assertNotIn("find_when:", document)
        self.assertNotIn("title: 프로젝트", document)
        self.assertNotIn(str(self.root), document)

    def test_missing_local_image_is_rejected(self):
        self.write_source("# missing\n\n![없음](assets/missing.png)\n")

        with self.assertRaisesRegex(ValueError, "image missing"):
            self.render()

        self.assertFalse(self.output.exists())

    def test_image_outside_the_selected_project_is_rejected(self):
        other_assets = self.data_root / "book" / "other-project" / "assets"
        other_assets.mkdir(parents=True)
        (other_assets / "secret.png").write_bytes(PNG)
        self.write_source("# outside\n\n![다른 프로젝트](../other-project/assets/secret.png)\n")

        with self.assertRaisesRegex(ValueError, "escapes book"):
            self.render()

        self.assertFalse(self.output.exists())

    def test_symlink_image_is_rejected_even_when_its_target_is_in_the_project(self):
        real_image = self.assets / "real.png"
        real_image.write_bytes(PNG)
        linked_image = self.assets / "linked.png"
        self.make_symlink(linked_image, real_image)
        self.write_source("# linked\n\n![링크](assets/linked.png)\n")

        with self.assertRaisesRegex(ValueError, "symlink|regular file"):
            self.render()

        self.assertFalse(self.output.exists())

    def test_external_image_url_is_rejected(self):
        self.write_source("# external\n\n![외부](https://example.com/pixel.png)\n")

        with self.assertRaisesRegex(ValueError, "external image"):
            self.render()

        self.assertFalse(self.output.exists())

    def test_source_symlink_escape_is_rejected(self):
        outside_source = self.root / "outside.md"
        outside_source.write_text(
            book_markdown("# outside\n"),
            encoding="utf-8",
            newline="\n",
        )
        self.make_symlink(self.source, outside_source)

        with self.assertRaisesRegex(ValueError, "regular file|symlink|escapes"):
            self.render()

        self.assertFalse(self.output.exists())

    def test_output_directory_symlink_escape_is_rejected_without_writing_outside(self):
        self.write_source("# safe\n")
        outside = self.root / "outside-output"
        outside.mkdir()
        self.make_symlink(
            self.output.parent,
            outside,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(ValueError, "output directory|symlink|escapes"):
            self.render()

        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_output_symlink_is_replaced_without_touching_its_target(self):
        self.write_source("# safe\n")
        self.output.parent.mkdir()
        outside = self.root / "user-data.html"
        outside.write_text("user data", encoding="utf-8", newline="\n")
        self.make_symlink(self.output, outside)

        rendered = self.render()

        self.assertEqual(rendered, self.output)
        self.assertFalse(rendered.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "user data")
        self.assertTrue(rendered.read_text(encoding="utf-8").startswith("<!doctype html>"))

    def test_invalid_project_and_source_topic_are_rejected(self):
        self.write_source("# safe\n")
        with self.assertRaises(ValueError):
            render_book(
                self.source,
                self.output,
                project="../escape",
                data_root=self.data_root,
            )

        invalid_source = self.project_book / "has space.md"
        invalid_source.write_text(
            book_markdown("# safe\n"),
            encoding="utf-8",
            newline="\n",
        )
        invalid_output = self.project_book / "html" / "has space.html"
        with self.assertRaises(ValueError):
            render_book(
                invalid_source,
                invalid_output,
                project=PROJECT,
                data_root=self.data_root,
            )


class MermaidResourceTests(unittest.TestCase):
    def test_packaged_mermaid_runtime_matches_pinned_bytes_version_hash_and_license(self):
        package = resources.files("didimlog.resources.personal")
        runtime = (package / "mermaid.min.js").read_bytes()
        checksum = (package / "mermaid.min.js.sha256").read_text(encoding="utf-8")
        version = (package / "MERMAID-VERSION").read_text(encoding="utf-8")
        license_text = (package / "MERMAID-LICENSE").read_text(encoding="utf-8")

        self.assertEqual(hashlib.sha256(runtime).hexdigest(), MERMAID_SHA256)
        self.assertEqual(
            checksum,
            "{}  mermaid.min.js\n".format(MERMAID_SHA256),
        )
        self.assertEqual(
            version,
            "mermaid 11.15.0\nsource: local installed package\nlicense: MIT\n",
        )
        self.assertTrue(license_text.startswith("The MIT License (MIT)\n\n"))
        self.assertIn("Copyright (c) 2014 - 2022 Knut Sveidqvist", license_text)
        self.assertIn(
            "Permission is hereby granted, free of charge, to any person obtaining a copy",
            license_text,
        )
        self.assertIn(
            'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND',
            license_text,
        )

    def test_mermaid_loader_rejects_runtime_with_wrong_digest(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            package = Path(temporary_directory)
            (package / "mermaid.min.js").write_text(
                "console.log('tampered')",
                encoding="utf-8",
            )

            with mock.patch.object(
                render_module.resources,
                "files",
                return_value=package,
            ):
                with self.assertRaisesRegex(ValueError, "integrity"):
                    render_module._load_mermaid()


if __name__ == "__main__":
    unittest.main()
