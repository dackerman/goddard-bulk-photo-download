"""Incremental PDF document uploads to explicitly selected Google Drive folders.

The native Google Picker authorizes only the configured folders (drive.file).
Uploads use stable, pre-generated IDs and app properties to survive retries.
No Drive files are deleted and unrelated files are never overwritten.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import mimetypes
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit, parse_qs, quote
import webbrowser

import goddard_sync as gs
from goddard_documents import atomic_write
from goddard_gphotos import request

API = 'https://www.googleapis.com/drive/v3'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3'
SCOPE = 'https://www.googleapis.com/auth/drive.file'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
FOLDER = 'application/vnd.google-apps.folder'
FIELDS = 'id,name,mimeType,parents,trashed,md5Checksum,appProperties,capabilities(canAddChildren,canEdit)'
CATEGORIES = ('Daily Sheets', 'Lesson Plans', 'Newsletters', 'Attachments')


class DriveError(Exception):
    pass


def credentials(cfg):
    client = cfg.get('gdrive_client_id') or cfg.get('gphotos_client_id')
    secret = cfg.get('gdrive_client_secret') or cfg.get('gphotos_client_secret')
    if not client or not secret:
        raise DriveError('Configure a Google Desktop OAuth client first (gdrive_client_id/secret).')
    return client, secret


def destinations(cfg):
    if cfg.get('per_student'):
        result = []
        for sid, settings in cfg.get('students', {}).items():
            if settings.get('gdrive_folder_id'):
                name = settings.get('name')
                if not name:
                    raise DriveError(f'Student {sid} needs a name before Drive syncing.')
                result.append((name, settings['gdrive_folder_id'], gs._student_output_dir(cfg, sid, name)))
        return result
    if cfg.get('gdrive_folder_id'):
        return [('Goddard', cfg['gdrive_folder_id'], os.path.expanduser(cfg['output_dir']))]
    return []


def authorization_params(client, redirect_uri, state, verifier, folder_ids):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return {'client_id': client, 'redirect_uri': redirect_uri, 'response_type': 'code',
            'scope': SCOPE, 'access_type': 'offline', 'prompt': 'consent',
            'include_granted_scopes': 'false', 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
            'trigger_onepick': 'true', 'allow_multiple': 'true',
            'allow_folder_selection': 'true', 'mimetypes': FOLDER,
            'file_ids': ','.join(folder_ids)}


def login(args):
    cfg = gs.load_config(args.config)
    client, secret = credentials(cfg)
    folder_ids = [fid for _, fid, _ in destinations(cfg)]
    if not folder_ids:
        raise DriveError('Configure destination folder IDs before drive-login.')
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlsplit(self.path).query)
            if not secrets.compare_digest(qs.get('state', [''])[0], state):
                self.send_error(400, 'Invalid sign-in state')
                return
            self.server.result = qs
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'<h1>Google sign-in received</h1><p>You can return to Codex. It will verify the selected folders.</p>')

        def log_message(self, *args):
            pass  # OAuth codes must not appear in logs.

    with http.server.HTTPServer(('127.0.0.1', 0), Handler) as server:
        server.timeout = 1
        server.result = None
        redirect = f'http://127.0.0.1:{server.server_port}/'
        url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(
            authorization_params(client, redirect, state, verifier, folder_ids))
        if args.url_file:
            atomic_write(Path(args.url_file), url.encode())
            print(f'Authorization URL saved to {args.url_file}', flush=True)
        else:
            print(url, flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        print('Select both configured folders in Google Picker. Waiting for authorization ...', flush=True)
        end = time.monotonic() + 1800
        while server.result is None and time.monotonic() < end:
            server.handle_request()
        qs = server.result
    if not qs or 'code' not in qs:
        raise DriveError('Drive sign-in was cancelled or timed out; run drive-login again.')
    selected = set(qs.get('picked_file_ids', [''])[0].split(','))
    if not set(folder_ids).issubset(selected):
        raise DriveError('Both configured folders must be selected; run drive-login again.')
    status, raw = request('POST', TOKEN_URL, {'Content-Type': 'application/x-www-form-urlencoded'},
                          urlencode({'client_id': client, 'client_secret': secret,
                                     'code': qs['code'][0], 'code_verifier': verifier,
                                     'redirect_uri': redirect, 'grant_type': 'authorization_code'}).encode())
    if status != 200:
        raise DriveError(f'Google token exchange failed (HTTP {status}).')
    token = json.loads(raw)
    if not token.get('refresh_token') or SCOPE not in token.get('scope', '').split():
        raise DriveError('Google did not return offline Drive access; run drive-login again.')
    # Reload to avoid replacing other config changes made while sign-in was open.
    cfg = gs.load_config(args.config)
    cfg.update(gdrive_client_id=client, gdrive_client_secret=secret,
               gdrive_refresh_token=token['refresh_token'])
    gs.save_config(args.config, cfg)
    print('Drive authorization saved. Google Photos credentials are unchanged.', flush=True)
    return 0


class Drive:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = None
        self.expires = 0

    def refresh(self):
        client, secret = credentials(self.cfg)
        if not self.cfg.get('gdrive_refresh_token'):
            raise DriveError('Drive authorization required: run goddard_sync.py drive-login.')
        status, raw = request('POST', TOKEN_URL, {'Content-Type': 'application/x-www-form-urlencoded'},
                              urlencode({'client_id': client, 'client_secret': secret,
                                         'refresh_token': self.cfg['gdrive_refresh_token'],
                                         'grant_type': 'refresh_token'}).encode())
        if status != 200:
            raise DriveError(f'Drive login needs renewal (HTTP {status}); run drive-login.')
        result = json.loads(raw)
        self.token = result['access_token']
        self.expires = time.monotonic() + int(result.get('expires_in', 3600)) - 60

    def api(self, method, path, body=None, content_type='application/json', upload=False, missing_ok=False):
        url = (UPLOAD if upload else API) + path
        for attempt in range(5):
            if not self.token or time.monotonic() >= self.expires:
                self.refresh()
            try:
                status, raw = request(method, url, {'Authorization': 'Bearer ' + self.token,
                                                   'Content-Type': content_type}, body)
            except OSError:
                if attempt == 4:
                    raise DriveError('Drive connection failed; rerun to safely resume.') from None
                time.sleep(2 ** attempt)
                continue
            if 200 <= status < 300:
                return json.loads(raw) if raw else {}
            if missing_ok and status == 404:
                return None
            if status == 401:
                self.token = None
            elif status not in (429, 500, 502, 503, 504):
                if status == 403:
                    raise DriveError('Drive access denied. Enable Google Drive API and Google Picker API '
                                     'in the OAuth project, and select writable destination folders with drive-login.')
                raise DriveError(f'Drive request failed (HTTP {status}); rerun to safely resume.')
            time.sleep(2 ** attempt)
        raise DriveError('Drive is temporarily unavailable; rerun to safely resume.')

    def get(self, ident):
        return self.api('GET', '/files/' + quote(ident, safe='') + '?' + urlencode({
            'fields': FIELDS, 'supportsAllDrives': 'true'}), missing_ok=True)

    def children(self, parent):
        found, page = [], None
        while True:
            query = {'q': "'" + parent.replace("'", "\\'") + "' in parents and trashed = false",
                     'fields': 'nextPageToken,files(' + FIELDS + ')', 'pageSize': 1000,
                     'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'}
            if page:
                query['pageToken'] = page
            result = self.api('GET', '/files?' + urlencode(query))
            found.extend(result.get('files', []))
            page = result.get('nextPageToken')
            if not page:
                return found

    def generate_id(self):
        return self.api('GET', '/files/generateIds?count=1&space=drive')['ids'][0]


def source_key(relative):
    return hashlib.sha256(relative.encode()).hexdigest()


def multipart(metadata, data, mime):
    boundary = 'goddard_' + secrets.token_hex(16)
    body = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode() +
            json.dumps(metadata).encode() + f'\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n'.encode() +
            data + f'\r\n--{boundary}--\r\n'.encode())
    return body, 'multipart/related; boundary=' + boundary


def upsert(drive, parent, name, key, data, known, pending, checkpoint):
    """Create/update one app-owned file. Persist an ID before the first write."""
    matches = [f for f in known if f.get('appProperties', {}).get('goddardSource') == key]
    if len(matches) > 1:
        raise DriveError('Duplicate managed Drive files found; resolve them before syncing.')
    state_key = parent + ':' + key
    remote = matches[0] if matches else None
    if remote is None and pending.get(state_key):
        remote = drive.get(pending[state_key])
        if remote and (remote.get('trashed') or parent not in remote.get('parents', []) or
                       remote.get('appProperties', {}).get('goddardSource') != key):
            raise DriveError('A managed file was moved or trashed in Drive; refusing to overwrite it.')
    # ubs:ignore — Drive exposes MD5 for byte equality, not authentication.
    digest = hashlib.md5(data, usedforsecurity=False).hexdigest() if data is not None else None
    if remote:
        if (data is None and remote.get('mimeType') != FOLDER) or (data is not None and remote.get('mimeType') == FOLDER):
            raise DriveError('Managed file type changed in Drive.')
        if data is None or remote.get('md5Checksum') == digest:
            return remote, False
        ident = remote['id']
        metadata = {'name': name}
        method, path = 'PATCH', '/files/' + quote(ident, safe='')
    else:
        ident = pending.get(state_key) or drive.generate_id()
        pending[state_key] = ident
        checkpoint()
        metadata = {'id': ident, 'name': name, 'parents': [parent],
                    'appProperties': {'goddardSource': key}}
        method, path = 'POST', '/files'
    options = {'fields': FIELDS, 'supportsAllDrives': 'true'}
    if data is None:
        metadata['mimeType'] = FOLDER
        body, content_type = json.dumps(metadata).encode(), 'application/json'
    else:
        body, content_type = multipart(metadata, data, mimetypes.guess_type(name)[0] or 'application/octet-stream')
        options['uploadType'] = 'multipart'
    try:
        result = drive.api(method, path + '?' + urlencode(options), body, content_type, upload=data is not None)
    except DriveError:
        # A response may have been lost after a successful create/update.
        result = drive.get(ident)
        if not result or parent not in result.get('parents', []) or result.get('trashed') or \
                result.get('appProperties', {}).get('goddardSource') != key or \
                (data is not None and result.get('md5Checksum') != digest):
            raise
    if data is not None and result.get('md5Checksum') != digest:
        raise DriveError('Drive checksum verification failed; rerun to retry.')
    pending[state_key] = result['id']
    checkpoint()
    return result, True


def prepare(cfg, output_dir):
    root = Path(output_dir) / 'Documents'
    state_path = root / '.documents-state.json'
    if not state_path.exists():
        raise DriveError(f'No document archive at {root}; run documents first.')
    records = json.loads(state_path.read_text())
    relative_files = sorted({f for r in records.values() for f in r['files'] + r.get('attachments', [])})
    pdf_root = root / 'Drive PDFs'
    jobs, outputs = [], []
    for relative in relative_files:
        source = (root / relative).resolve()
        if not source.is_relative_to(root.resolve()) or not source.is_file():
            raise DriveError('Missing or invalid source document; run documents to repair it.')
        if Path(relative).parts[0] not in CATEGORIES:
            raise DriveError('Unknown document category in archive.')
        if source.suffix == '.html':
            dest = pdf_root / Path(relative).with_suffix('.pdf')
            # Hash local assets too, so repairing/changing an image rebuilds the PDF.
            related = sorted({a for r in records.values() if relative in r['files'] for a in r.get('assets', [])})
            digest = hashlib.sha256(b'goddard-pdf-v1' + source.read_bytes())
            for asset in related:
                asset_path = (root / asset).resolve()
                if not asset_path.is_relative_to(root.resolve()) or not asset_path.is_file():
                    raise DriveError('Missing local document asset; run documents first.')
                digest.update(asset_path.read_bytes())
            fingerprint = digest.hexdigest()
            stamp = dest.with_suffix('.sha256')
            if not dest.exists() or not dest.read_bytes().startswith(b'%PDF-') or not stamp.exists() or stamp.read_text() != fingerprint:
                jobs.append({'source': str(source), 'destination': str(dest),
                             'fingerprint': fingerprint, 'stamp': str(stamp)})
            outputs.append((str(Path(relative).with_suffix('.pdf')), dest))
        else:
            outputs.append((relative, source))
    if jobs:
        env = os.environ.copy()
        if cfg.get('gdrive_node_modules'):
            env['NODE_PATH'] = cfg['gdrive_node_modules']
        if cfg.get('gdrive_chromium'):
            env['GODDARD_CHROMIUM'] = cfg['gdrive_chromium']
        renderer = Path(__file__).parent / 'tools' / 'render_documents.cjs'
        print(f'Rendering {len(jobs)} document PDFs ...', flush=True)
        subprocess.run([cfg.get('gdrive_node', 'node'), str(renderer)],
                       input=json.dumps(jobs), text=True, env=env, check=True,
                       timeout=max(300, len(jobs) * 10))
    return root, outputs


def sync_folder(drive, root, outputs, folder_id, dry_run=False):
    folder = drive.get(folder_id)
    if not folder or folder.get('trashed') or folder.get('mimeType') != FOLDER or not folder.get('capabilities', {}).get('canAddChildren'):
        raise DriveError('Destination is not an accessible, writable Drive folder; run drive-login.')
    state_path = root / '.gdrive-state.json'
    pending = json.loads(state_path.read_text()) if state_path.exists() else {}
    def checkpoint():
        atomic_write(state_path, json.dumps(pending, indent=2).encode())
    if dry_run:
        print(f"Drive destination '{folder['name']}': {len(outputs)} local files ready; no uploads performed.")
        return 0
    # Prevent two invocations from creating competing IDs before the first write.
    import fcntl
    with (root / '.gdrive-sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pending = json.loads(state_path.read_text()) if state_path.exists() else {}
        parent_files = drive.children(folder_id)
        uploaded = skipped = 0
        for category in CATEGORIES:
            files = [(rel, path) for rel, path in outputs if Path(rel).parts[0] == category]
            if not files:
                continue
            sub, _ = upsert(drive, folder_id, category, source_key('folder/' + category), None,
                            parent_files, pending, checkpoint)
            known = drive.children(sub['id'])
            for relative, path in files:
                _, changed = upsert(drive, sub['id'], path.name, source_key(relative), path.read_bytes(),
                                    known, pending, checkpoint)
                uploaded += bool(changed)
                skipped += not changed
                if (uploaded + skipped) % 25 == 0:
                    print(f'Drive: {uploaded} uploaded/updated, {skipped} unchanged', flush=True)
        print(f"Drive '{folder['name']}': {uploaded} uploaded/updated, {skipped} unchanged.", flush=True)
    return 0


def upload(args):
    cfg = gs.load_config(args.config)
    targets = destinations(cfg)
    if not targets:
        raise DriveError('No Drive destination folders configured.')
    if not args.prepare_only:
        credentials(cfg)
        if not cfg.get('gdrive_refresh_token'):
            raise DriveError('Google Drive is not authorized; run drive-login first.')
    drive = Drive(cfg)
    for name, folder, output_dir in targets:
        root, outputs = prepare(cfg, output_dir)
        print(f'{name}: {len(outputs)} files → https://drive.google.com/drive/folders/{folder}', flush=True)
        if not args.prepare_only:
            sync_folder(drive, root, outputs, folder, args.dry_run)
    return 0


def command(args):
    try:
        return login(args) if args.cmd == 'drive-login' else upload(args)
    except DriveError as exc:
        if getattr(args, "debug", False):
            raise
        print(f"Error: {exc}", file=sys.stderr)
        return 1
