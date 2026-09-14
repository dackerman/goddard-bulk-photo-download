"""Document export tests; no network or real account data."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import goddard_documents as docs


class DocumentsTest(unittest.TestCase):
    def test_lesson_text_keeps_paragraphs_and_removes_hidden_content(self):
        parser = docs.LessonText()
        parser.feed('<html><head><title>Duplicate</title><style>bad css</style></head>'
                    '<body><h1>Counting &amp; Shapes</h1><p>Count <b>three</b> blocks.</p>'
                    '<div hidden><p>Hidden</p></div><script>bad()</script>'
                    '<p>Line one<br/>Line two</p><ul><li>Circle</li><li>Square</li></ul>'
                    '<div><span>Math</span><span>Art</span></div></body></html>')
        text = parser.text()
        self.assertIn('Counting & Shapes\n\nCount three blocks.', text)
        self.assertIn('Line one\nLine two', text)
        self.assertIn('- Circle\n- Square', text)
        self.assertIn('Math Art', text)
        for unwanted in ('Duplicate', 'bad css', 'bad()', 'Hidden', '<'):
            self.assertNotIn(unwanted, text)

    def test_lesson_text_backfill_updates_without_duplicate_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Lesson Plans').mkdir()
            lesson = root / 'Lesson Plans/lesson.html'
            lesson.write_text('<html><body><h1>Lessons</h1><p>Count to three.</p></body></html>')
            state = {'one': {'date': '2026-01-01', 'files': ['Lesson Plans/lesson.html']}}
            self.assertEqual(docs.export_lesson_text(root, state), 1)
            self.assertEqual(docs.export_lesson_text(root, state), 1)
            self.assertEqual(state['one']['files'].count('Lesson Plans/lesson.txt'), 1)
            lesson.write_text('<html><body><h1>Lessons</h1><p>Count to four.</p></body></html>')
            docs.export_lesson_text(root, state)
            self.assertIn('Count to four.', lesson.with_suffix('.txt').read_text())
            self.assertIn('Count to four.', (root / 'all-lesson-text.txt').read_text())

    def test_daily_detail_uses_composite_id(self):
        row = {'type': 'dailysheet', 'dailysheet': {
            'classroom': 'room', 'student': 'child',
            'fromDateISOString': '2026-01-12T05:00:00.000Z',
            'timezoneOffset': -300, 'language': 'en'}}
        with patch.object(docs.gs, '_http', return_value={'dailysheetUrl': 'https://example.com/doc'}) as call:
            self.assertEqual(docs.document_url(row, 'token'), 'https://example.com/doc')
            self.assertIn('room%7Cchild%7C2026-01-12T05%3A00%3A00.000Z%7C-300%7Cen', call.call_args.args[0])
            self.assertEqual(call.call_args.kwargs['token'], 'token')

    def test_offline_assets_and_lessons(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(docs, 'fetch', return_value=b'asset'):
            parser = docs.OfflineHTML('https://example.com/doc', Path(directory))
            parser.feed('<html><script>bad()</script><body onload="bad()"><img src="/image.jpg">'
                        '<table><tr><td class="lessons-container"><table><tr><td>Counting &amp; shapes'
                        '<br/>One</td></tr></table></td></tr></table>'
                        '<a href="/lesson%20one.pdf">PDF</a></body></html>')
            result = ''.join(parser.parts)
            self.assertNotIn('bad()', result)
            self.assertIn('../_assets/', result)
            self.assertIn('../Attachments/', result)
            self.assertIn('Counting &amp; shapes', ''.join(parser.lessons))
            self.assertEqual(parser.capture, 0)
            self.assertEqual(len(parser.attachments), 1)
            for filename in parser.assets:
                self.assertEqual((Path(directory) / filename).read_bytes(), b'asset')

    def test_resume_and_retry_missing_asset(self):
        row = {'_id': 'sheet1', 'date': '2020-01-01', 'type': 'dailysheet'}
        source = b'<html><body><div class="heading-date">Date</div><img src="/a.jpg"></body></html>'
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(docs, 'document_url', return_value='https://example.com/doc'), \
             patch.object(docs, 'fetch', side_effect=lambda u: source if u.endswith('/doc') else b'image') as get:
            self.assertEqual(docs.sync_folder([row], 'token', directory), 0)
            count = get.call_count
            self.assertEqual(docs.sync_folder([row], 'token', directory), 0)
            self.assertEqual(get.call_count, count)
            root = Path(directory) / 'Documents'
            # ubs:ignore — malformed exporter output must fail this test.
            state = json.loads((root / '.documents-state.json').read_text())
            (root / state['sheet1']['assets'][0]).unlink()
            self.assertEqual(docs.sync_folder([row], 'token', directory), 0)
            self.assertGreater(get.call_count, count)

    def test_failure_is_not_marked_complete(self):
        row = {'_id': 'sheet1', 'date': '2020-01-01', 'type': 'dailysheet'}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(docs, 'document_url', side_effect=ValueError('bad')):
            self.assertEqual(docs.sync_folder([row], 'token', directory), 1)
            self.assertFalse((Path(directory) / 'Documents/.documents-state.json').exists())

    def test_shared_url_uses_encrypted_pair(self):
        row = {'type': 'storyboard', 'storyboardId': 'story'}
        with patch.object(docs.gs, '_http', return_value={'encrypted': {'id': 'abc', 'v': 'def'}}):
            self.assertEqual(docs.document_url(row, 'token'),
                             'https://my.kaymbu.com/storyboards/shared?id=abc&v=def&excludeBanner=1&source=parentapp')


if __name__ == '__main__':
    unittest.main()
