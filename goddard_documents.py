"""Offline daily sheets, lessons, and newsletters from the Family Hub feed.

Uses the same detail IDs and shared document URLs as the Android app. No
browser, emulator, or extra Python packages are needed.
"""
from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
import urllib.error
import urllib.request

import goddard_sync as gs

DOCUMENT_TYPES = {'dailysheet', 'dailynote', 'storyboard', 'lesson-planner'}
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'param', 'source', 'track', 'wbr'}


def document_url(row, token):
    kind = row['type']
    if kind == 'dailysheet':
        sheet = row['dailysheet']
        ident = '|'.join(str(sheet[k]) for k in (
            'classroom', 'student', 'fromDateISOString', 'timezoneOffset', 'language'))
    else:
        ident = row[{'storyboard': 'storyboardId', 'dailynote': 'dailynoteId',
                     'lesson-planner': 'lessonPlanMessageId'}[kind]]
    detail = gs._http(gs.API_BASE + '/feed/details/' + kind + '/' +
                      quote(ident, safe=''), token=token)
    if kind == 'dailysheet':
        return detail['dailysheetUrl']
    if kind == 'storyboard':
        return 'https://my.kaymbu.com/storyboards/shared?' + urlencode({
            'id': detail['encrypted']['id'], 'v': detail['encrypted']['v'],
            'excludeBanner': 1, 'source': 'parentapp'})
    if kind == 'lesson-planner':
        return urljoin('https://my.kaymbu.com', detail['lessonPlanUrl'])
    return ('https://dailynote-api-production.herokuapp.com/api/v1/viewNote/' +
            quote(ident, safe='') + '?excludeBanner=1&excludeFooter=1&source=parentapp')


def fetch(url):
    """Fetch public document/assets, without forwarding the account token."""
    url = quote(url, safe=":/?=&%+#@;,!$'()*[]~")
    if urlsplit(url).scheme != 'https':
        raise ValueError('Document assets must use HTTPS')
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': gs.USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as response:
                data = response.read()
                if not data:
                    raise ValueError('Empty document or asset')
                return data
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
        except (OSError, ValueError):
            if attempt == 3:
                raise
        time.sleep(attempt + 1)


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.part')
    temp.write_bytes(data)
    temp.chmod(0o600)
    temp.replace(path)


def safe_name(text):
    return re.sub(r'[^\w .()-]+', '-', str(text)).strip(' .-')[:110] or 'document'


class OfflineHTML(HTMLParser):
    """Keep server-rendered content, localize images/files, remove web app scripts.

    The source documents use inline layout. App navigation stylesheets and
    scripts are unnecessary in the archive and could require a live session.
    """
    def __init__(self, base_url, root):
        super().__init__(convert_charrefs=False)
        self.base_url, self.root = base_url, root
        self.parts, self.lessons, self.assets = [], [], set()
        self.capture = 0
        self.skip_script = False
        self.attachments = set()

    def emit(self, value):
        self.parts.append(value)
        if self.capture:
            self.lessons.append(value)

    def asset(self, value, attachment=False):
        if value.startswith('data:'):
            return value
        url = urljoin(self.base_url, html.unescape(value))
        digest = hashlib.sha256(url.encode()).hexdigest()[:20]
        name = safe_name(unquote(urlsplit(url).path.rsplit('/', 1)[-1]))
        folder = 'Attachments' if attachment else '_assets'
        relative = folder + '/' + digest + '-' + name
        path = self.root / relative
        if not path.exists() or not path.stat().st_size:
            atomic_write(path, fetch(url))
        self.assets.add(relative)
        if attachment:
            self.attachments.add(relative)
        return '../' + quote(relative)

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.skip_script = True
            return
        if self.skip_script or tag in ('link', 'base'):
            return
        attrs = dict(attrs)
        if 'lessons-container' in attrs.get('class', '').split():
            self.capture = 1
        elif self.capture and tag not in VOID:
            self.capture += 1
        for key in list(attrs):
            val = attrs[key]
            if key.startswith('on') or key in ('srcset', 'integrity', 'crossorigin'):
                del attrs[key]
            elif key in ('src', 'poster') and val:
                attrs[key] = self.asset(val)
            elif key == 'href' and val:
                if re.search(r'\.(pdf|docx?|xlsx?|pptx?|zip)(?:[?#]|$)', val, re.I):
                    attrs[key] = self.asset(val, attachment=True)
                else:
                    attrs[key] = urljoin(self.base_url, val) if not val.startswith('#') else val
            elif key == 'style' and val:
                attrs[key] = re.sub(r'url\([\'\"]?([^\)\'\"]+)[\'\"]?\)',
                                    lambda m: 'url("' + self.asset(m[1]) + '")', val)
        rendered = ''.join(' ' + k + ('="' + html.escape(v, quote=True) + '"'
                                     if v is not None else '') for k, v in attrs.items())
        self.emit('<' + tag + rendered + '>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.skip_script = False
            return
        if self.skip_script or tag in VOID:
            return
        self.emit('</' + tag + '>')
        if self.capture:
            self.capture -= 1

    def handle_data(self, data):
        if not self.skip_script:
            self.emit(data)

    def handle_entityref(self, name):
        self.handle_data('&' + name + ';')

    def handle_charref(self, name):
        self.handle_data('&#' + name + ';')

    def handle_decl(self, decl):
        self.emit('<!' + decl + '>')


def page(title, body):
    return ('<!doctype html><html><head><meta charset="utf-8"><title>' +
            html.escape(title) + '</title><style>body{font:16px/1.5 Arial,sans-serif;'
            'max-width:850px;margin:35px auto;padding:0 20px}li{margin:6px 0}'
            'table{max-width:100%}</style></head><body><h1>' + html.escape(title) +
            '</h1>' + body + '</body></html>').encode()



class LessonText(HTMLParser):
    """Extract readable paragraphs from lesson HTML without CSS or scripts."""
    BLOCKS = {'h1', 'h2', 'h3', 'h4', 'p', 'div', 'section', 'article', 'tr', 'ul', 'ol'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag not in VOID:
                self.skip += 1
            return
        attrs = dict(attrs)
        hidden = ('hidden' in attrs or attrs.get('aria-hidden') == 'true' or
                  re.search(r'display\s*:\s*none', attrs.get('style', ''), re.I))
        if tag in ('head', 'script', 'style') or hidden:
            if tag not in VOID:
                self.skip = 1
        elif tag in self.BLOCKS:
            self.parts.append('\n\n')
        elif tag == 'br':
            self.parts.append('\n')
        elif tag == 'li':
            self.parts.append('\n- ')

    def handle_endtag(self, tag):
        if self.skip:
            if tag not in VOID:
                self.skip -= 1
        elif tag in self.BLOCKS:
            self.parts.append('\n\n')
        elif tag == 'li':
            self.parts.append('\n')
        elif tag in ('td', 'th', 'span'):
            self.parts.append(' ')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self):
        text = ''.join(self.parts).replace('\xa0', ' ')
        lines = [re.sub(r'[^\S\n]+', ' ', line).strip() for line in text.splitlines()]
        text = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
        text = re.sub(r'(?m)^(- [^\n]+)\n{2,}(?=- )', r'\1\n', text)
        return text + '\n'


def export_lesson_text(root, state):
    """Backfill text from saved HTML and register it for ordinary Drive sync."""
    root = Path(root)
    count = 0
    combined = []
    for record in sorted(state.values(), key=lambda row: row.get('date', '')):
        for filename in list(record['files']):
            if not filename.startswith('Lesson Plans/') or not filename.endswith('.html'):
                continue
            source = (root / filename).resolve()
            if not source.is_relative_to(root.resolve()):
                raise ValueError('Lesson source is outside the archive')
            parser = LessonText()
            parser.feed(source.read_text(encoding='utf-8'))
            content = parser.text()
            if not content.strip():
                raise ValueError('Lesson page contains no text')
            relative = str(Path(filename).with_suffix('.txt'))
            target = root / relative
            data = content.encode('utf-8')
            if not target.exists() or target.read_bytes() != data:
                atomic_write(target, data)
            if relative not in record['files']:
                record['files'].append(relative)
            combined.append(content.rstrip())
            count += 1
    if combined:
        atomic_write(root / 'all-lesson-text.txt', ('\n\n' + '=' * 72 + '\n\n').join(combined).encode('utf-8') + b'\n')
    return count


def cmd_lesson_text(args):
    """Extract saved lesson pages locally; neither login nor network is needed."""
    cfg = gs.load_config(args.config)
    if args.output_dir:
        cfg['output_dir'] = args.output_dir
    folders = ([c['out_dir'] for c in gs._resolve_children(cfg, [])]
               if cfg.get('per_student') else [os.path.expanduser(cfg['output_dir'])])
    if not folders:
        print('No child folders configured; add student names or use single-folder mode.', file=gs.sys.stderr)
        return 1
    result = 0
    for folder in folders:
        root = Path(folder) / 'Documents'
        try:
            state_path = root / '.documents-state.json'
            state = json.loads(state_path.read_text())
            count = export_lesson_text(root, state)
            atomic_write(state_path, json.dumps(state, indent=2).encode())
            print(f'{root}: extracted {count} lesson text files.')
        except (OSError, ValueError) as exc:
            print(f'Lesson text export failed ({type(exc).__name__}) in {root}.', file=gs.sys.stderr)
            result = 1
    return result


def sync_folder(rows, token, output_dir, refresh=False):
    root = Path(output_dir).expanduser() / 'Documents'
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / '.documents-state.json'
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if not isinstance(state, dict):
            raise ValueError('Expected a document index object')
    except (ValueError, OSError):
        print(f'Cannot read {state_path}; preserve or repair it before retrying.', file=gs.sys.stderr)
        return 1
    downloaded = skipped = failed = 0
    for index, row in enumerate(rows, 1):
        ident = row['_id']
        old = state.get(ident)
        # Refresh today's daily sheet as the school may add entries during the day.
        date = row.get('dailysheet', {}).get('fromDateISOString', row.get('date', ''))[:10]
        today = gs.datetime.now().astimezone().date().isoformat()
        if (not refresh and old and date != today and
                all((root / f).is_file() and (root / f).stat().st_size
                    for f in old['files'] + old.get('assets', []))):
            skipped += 1
            continue
        kind = row['type']
        folder = {'dailysheet': 'Daily Sheets', 'dailynote': 'Daily Sheets',
                  'storyboard': 'Newsletters', 'lesson-planner': 'Lesson Plans'}[kind]
        title = row.get('title') or ('Daily Sheet' if kind in ('dailysheet', 'dailynote') else 'Lesson Plan')
        basename = safe_name(date + ' ' + title) + '_' + safe_name(ident)
        try:
            url = document_url(row, token)
            data = fetch(url)
            files = []
            parser = OfflineHTML(url, root)
            if data.startswith(b'%PDF-'):
                relative = folder + '/' + basename + '.pdf'
                atomic_write(root / relative, data)
            else:
                source = data.decode('utf-8')
                if '<html' not in source.lower() or 'Login to Kaymbu' in source:
                    raise ValueError('Expected a document, received an invalid page')
                if kind == 'dailysheet' and 'lessons-container' not in source and 'heading-date' not in source:
                    raise ValueError('Daily sheet content missing')
                parser.feed(source)
                relative = folder + '/' + basename + '.html'
                atomic_write(root / relative, ''.join(parser.parts).encode())
                if parser.lessons:
                    lesson = 'Lesson Plans/' + safe_name(date + ' Lessons') + '_' + safe_name(ident) + '.html'
                    # Captured content is a table cell; wrap it in a valid table.
                    atomic_write(root / lesson, page(date + ' Lessons',
                                 '<table><tr>' + ''.join(parser.lessons) + '</tr></table>'))
                    files.append(lesson)
            files.append(relative)
            state[ident] = {'date': date, 'title': title, 'type': kind, 'files': files,
                            'assets': sorted(parser.assets), 'attachments': sorted(parser.attachments)}
            atomic_write(state_path, json.dumps(state, indent=2).encode())
            downloaded += 1
        except Exception as exc:
            # Do not log signed document URLs or account tokens from exceptions.
            print(f"Document {ident} failed ({type(exc).__name__}); rerun to retry.", file=gs.sys.stderr)
            failed += 1
        if index % 20 == 0 or index == len(rows):
            print(f'Documents: {index}/{len(rows)}, saved {downloaded}, existing {skipped}, failed {failed}', flush=True)
    try:
        text_count = export_lesson_text(root, state)
        if text_count:
            atomic_write(state_path, json.dumps(state, indent=2).encode())
            print(f'Lesson text: {text_count} files available.')
    except (OSError, ValueError) as exc:
        print(f'Lesson text export failed ({type(exc).__name__}); rerun to retry.', file=gs.sys.stderr)
        failed += 1
    sections = []
    for folder in ('Daily Sheets', 'Lesson Plans', 'Newsletters', 'Attachments'):
        entries = {}
        for record in state.values():
            for filename in record['files'] + record.get('attachments', []):
                if filename.startswith(folder + '/'):
                    entries[filename] = record['date'] + ' — ' + (
                        ('Lessons (' + Path(filename).suffix[1:].upper() + ')')
                        if folder == 'Lesson Plans' else record['title'])
                    if folder == 'Attachments':
                        entries[filename] = Path(filename).name.split('-', 1)[-1]
        links = ''.join('<li><a href="' + quote(f) + '">' + html.escape(label) + '</a></li>'
                        for f, label in sorted(entries.items(), reverse=True))
        sections.append('<h2>' + folder + f' ({len(entries)})</h2><ul>' + links + '</ul>')
    atomic_write(root / 'index.html', page('Goddard Documents', ''.join(sections)))
    print(f'Document archive: {root / "index.html"}')
    return 1 if failed else 0


def cmd_documents(args):
    cfg = gs.load_config(args.config)
    if not cfg.get('token'):
        print('Run login first.', file=gs.sys.stderr)
        return 2
    results = gs.fetch_feed(cfg['token'])
    rows = [r for r in results if r.get('type') in DOCUMENT_TYPES]
    print(f"Found {sum(r['type'] in ('dailysheet', 'dailynote') for r in rows)} daily sheets, "
          f"{sum(r['type'] == 'lesson-planner' for r in rows)} standalone lesson plans, "
          f"{sum(r['type'] == 'storyboard' for r in rows)} newsletters.", flush=True)
    if args.output_dir:
        cfg['output_dir'] = args.output_dir
    if cfg.get('per_student'):
        children = gs._resolve_children(cfg, results)
        return max((sync_folder([r for r in rows if not r.get('studentIds') or
                                c['id'] in r['studentIds']], cfg['token'], c['out_dir'], args.refresh)
                    for c in children), default=0)
    return sync_folder(rows, cfg['token'], cfg['output_dir'], args.refresh)
