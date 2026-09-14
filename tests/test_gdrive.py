import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import goddard_gdrive as gd


class FakeDrive:
    def __init__(self, lose_response=False):
        self.files = {}
        self.calls = []
        self.lose_response = lose_response
        self.counter = 0

    def generate_id(self):
        self.counter += 1
        return 'id' + str(self.counter)

    def get(self, ident):
        return self.files.get(ident)

    def api(self, method, path, body, content_type, upload=False):
        self.calls.append((method, path))
        if upload:
            sections = body.split(b'\r\n\r\n')
            metadata = json.loads(sections[1].split(b'\r\n--')[0])
            data = sections[2].rsplit(b'\r\n--', 1)[0]
        else:
            metadata = json.loads(body)
            data = None
        ident = metadata.get('id') or path.split('/files/')[1].split('?')[0]
        result = dict(self.files.get(ident, {}), **metadata)
        result['id'] = ident
        if data is not None:
            result['md5Checksum'] = hashlib.md5(data, usedforsecurity=False).hexdigest()
        self.files[ident] = result
        if self.lose_response:
            self.lose_response = False
            raise gd.DriveError('response lost')
        return result


class DriveTests(unittest.TestCase):
    def test_scheduled_sync_runs_documents_then_drive(self):
        from types import SimpleNamespace
        import goddard_documents
        args = SimpleNamespace(config="unused", output_dir=None, no_drive=False)
        cfg = {"token": "token", "per_student": True, "gdrive_sync_enabled": True}
        with patch.object(gd.gs, "load_config", return_value=cfg), \
             patch.object(gd.gs, "_run_sync", return_value=1), \
             patch.object(goddard_documents, "cmd_documents", return_value=0) as documents, \
             patch.object(gd, "command", return_value=0) as upload:
            self.assertEqual(gd.gs.cmd_sync(args), 1)
            documents.assert_called_once()
            upload.assert_called_once()

    def test_failed_document_download_does_not_upload_stale_files(self):
        from types import SimpleNamespace
        import goddard_documents
        args = SimpleNamespace(config="unused", output_dir=None, no_drive=False)
        cfg = {"token": "token", "per_student": True, "gdrive_sync_enabled": True}
        with patch.object(gd.gs, "load_config", return_value=cfg), \
             patch.object(gd.gs, "_run_sync", return_value=0), \
             patch.object(goddard_documents, "cmd_documents", return_value=1), \
             patch.object(gd, "command") as upload:
            self.assertEqual(gd.gs.cmd_sync(args), 1)
            upload.assert_not_called()

    def test_picker_only_selected_folders_and_scope(self):
        p = gd.authorization_params('client', 'http://127.0.0.1/', 'state', 'verifier', ['max', 'maya'])
        self.assertEqual(p['scope'], 'https://www.googleapis.com/auth/drive.file')
        self.assertEqual(p['file_ids'], 'max,maya')
        self.assertEqual(p['trigger_onepick'], 'true')
        self.assertEqual(p['include_granted_scopes'], 'false')
        self.assertEqual(p['code_challenge_method'], 'S256')

    def test_create_skip_update(self):
        drive, pending = FakeDrive(), {}
        checkpointed = []
        save = lambda: checkpointed.append(dict(pending))
        original, changed = gd.upsert(drive, 'parent', 'doc.pdf', 'source', b'one', [], pending, save)
        self.assertTrue(changed)
        self.assertEqual(checkpointed[0], {'parent:source': 'id1'})
        remote, changed = gd.upsert(drive, 'parent', 'doc.pdf', 'source', b'one', [original], pending, save)
        self.assertFalse(changed)
        self.assertEqual(len(drive.calls), 1)
        updated, changed = gd.upsert(drive, 'parent', 'doc.pdf', 'source', b'two', [remote], pending, save)
        self.assertTrue(changed)
        self.assertEqual(updated['id'], original['id'])
        self.assertEqual(drive.calls[-1][0], 'PATCH')

    def test_response_lost_does_not_duplicate(self):
        drive, pending = FakeDrive(lose_response=True), {}
        saved, changed = gd.upsert(drive, 'parent', 'doc.pdf', 'key', b'data', [], pending, lambda: None)
        self.assertTrue(changed)
        recovered, changed = gd.upsert(drive, 'parent', 'doc.pdf', 'key', b'data', [], pending, lambda: None)
        self.assertFalse(changed)
        self.assertEqual(saved['id'], recovered['id'])
        self.assertEqual(drive.counter, 1)

    def test_existing_unrelated_filename_not_overwritten(self):
        drive = FakeDrive()
        result, _ = gd.upsert(drive, 'parent', 'doc.pdf', 'key', b'data',
                              [{'id': 'manual', 'name': 'doc.pdf'}], {}, lambda: None)
        self.assertNotEqual(result['id'], 'manual')
        self.assertEqual(drive.calls[0][0], 'POST')

    def test_changed_target_folder_uses_new_id(self):
        drive, pending = FakeDrive(), {}
        a, _ = gd.upsert(drive, 'first', 'doc.pdf', 'key', b'data', [], pending, lambda: None)
        b, _ = gd.upsert(drive, 'second', 'doc.pdf', 'key', b'data', [], pending, lambda: None)
        self.assertNotEqual(a['id'], b['id'])

    def test_moved_file_is_not_overwritten(self):
        drive = FakeDrive()
        drive.files['existing'] = {'id': 'existing', 'parents': ['elsewhere'], 'appProperties': {'goddardSource': 'key'}}
        with self.assertRaises(gd.DriveError):
            gd.upsert(drive, 'parent', 'doc.pdf', 'key', b'data', [], {'parent:key': 'existing'}, lambda: None)
        self.assertEqual(drive.calls, [])

    def test_prepare_passes_through_attachments_and_renders_html(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'Documents'
            (root / 'Daily Sheets').mkdir(parents=True)
            (root / 'Attachments').mkdir()
            (root / 'Daily Sheets/test.html').write_text('<html>Daily sheet</html>')
            (root / 'Attachments/test.pdf').write_bytes(b'%PDF-attachment')
            (root / '.documents-state.json').write_text(json.dumps({'sheet': {'files': ['Daily Sheets/test.html'], 'assets': [], 'attachments': ['Attachments/test.pdf']}}))
            with patch.object(gd.subprocess, 'run') as run:
                _, outputs = gd.prepare({}, folder)
                self.assertEqual(len(outputs), 2)
                jobs = json.loads(run.call_args.kwargs['input'])
                self.assertEqual(len(jobs), 1)
                self.assertTrue(jobs[0]['destination'].endswith('Daily Sheets/test.pdf'))

    def test_drive_destinations_do_not_mix_children(self):
        cfg = {'per_student': True, 'output_dir': '/photos/{name}', 'students': {
            'a': {'name': 'Max', 'gdrive_folder_id': 'max-folder'},
            'b': {'name': 'Maya', 'gdrive_folder_id': 'maya-folder'}}}
        self.assertEqual(gd.destinations(cfg), [('Max', 'max-folder', '/photos/Max'), ('Maya', 'maya-folder', '/photos/Maya')])


if __name__ == '__main__':
    unittest.main()
