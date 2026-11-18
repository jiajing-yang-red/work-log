"""Input preservation, content screening, and evidence-backed exports."""
import copy
import datetime as dt
import hashlib
import io
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

STATUSES = {'unresolved': 0, 'partial': 1, 'content_obtained': 2}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def host(url):
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')


def valid_url(url):
    if not isinstance(url, str) or not url or re.search(r'\s', url) or len(url) > 8192:
        return False
    try:
        p = urlsplit(url)
        return p.scheme in ('http', 'https') and bool(p.hostname) and p.username is None and p.password is None
    except ValueError:
        return False


def public_url(url):
    if not valid_url(url):
        return False
    h = host(url)
    if h == 'localhost' or h.endswith(('.localhost', '.local', '.internal')):
        return False
    try:
        return ipaddress.ip_address(h).is_global
    except ValueError:
        return True


def at(document, path):
    for part in path:
        document = document[part]
    return document


def load_input(args):
    if args.input:
        path = Path(args.input)
        raw = path.read_bytes()
        if path.suffix.lower() == '.jsonl':
            data = [json.loads(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip()]
        else:
            data = json.loads(raw.decode('utf-8-sig'))
        source_hash = hashlib.sha256(raw).hexdigest()
    else:
        data = [{'url': u} for u in args.url]
        source_hash = digest(json.dumps(data, ensure_ascii=False))
    prefix = args.rows_key.split('.') if args.rows_key else []
    container = at(data, prefix)
    if isinstance(container, dict) and args.url_key in container and not args.results_key:
        paths = [prefix]
    elif isinstance(container, list):
        paths = [prefix + [i] for i in range(len(container))]
    else:
        raise ValueError('Input must be an object with URL or a list; use --rows-key for a wrapped list.')
    if args.results_key:
        expanded = []
        suffix = args.results_key.split('.')
        for path in paths:
            values = at(data, path + suffix)
            if not isinstance(values, list):
                raise ValueError('Each --results-key value must be a list.')
            expanded.extend(path + suffix + [i] for i in range(min(len(values), args.top_k or len(values))))
        paths = expanded
    elif args.top_k:
        raise ValueError('--top-k requires --results-key; it selects the first K in each original array.')
    urls = {}
    for path in paths:
        row = at(data, path)
        if not isinstance(row, dict):
            raise ValueError('Selected rows must be objects; normalize non-object records explicitly.')
        if args.field in row:
            raise ValueError('Output field already exists; choose a different --field, never overwrite it.')
        url = row.get(args.url_key)
        uid = digest(url if isinstance(url, str) else json.dumps(url, sort_keys=True))
        entry = urls.setdefault(uid, {'url_id': uid, 'url': url, 'occurrences': []})
        entry['occurrences'].append(path)
    spec = {'source_sha256': source_hash, 'rows_key': args.rows_key, 'results_key': args.results_key,
            'url_key': args.url_key, 'top_k': args.top_k, 'field': args.field}
    return data, urls, spec


def kind(url):
    h, p = host(url), urlsplit(url).path
    if h in ('youtube.com', 'm.youtube.com', 'youtu.be'):
        return 'video' if h == 'youtu.be' or '/watch' in p or '/shorts/' in p else 'channel'
    if h in ('facebook.com', 'm.facebook.com', 'instagram.com', 'linkedin.com', 'x.com', 'twitter.com'):
        return 'social_post' if any(x in p for x in ('/posts/', '/post/', '/p/', '/reel/', '/status/')) else 'profile'
    return 'pdf' if p.lower().endswith('.pdf') or '/doi/pdf/' in p else 'page'


def extract_bytes(body, content_type, url):
    """Return readable text; never mistake binary PDF contents for document text."""
    if body.startswith(b'%PDF-') or 'application/pdf' in content_type:
        from pypdf import PdfReader
        pdf = PdfReader(io.BytesIO(body))
        text = '\n\n'.join(page.extract_text() or '' for page in pdf.pages)
        return str((pdf.metadata or {}).get('/Title', '')), text, 'pdf_text', [] if text.strip() else ['pdf_needs_ocr']
    from lxml import html
    import trafilatura
    decoded = body.decode('utf-8', errors='replace')
    if 'html' not in content_type and not re.search(r'<(?:html|body|article)\b', decoded[:2000], re.I):
        if any(x in content_type for x in ('text/', 'json', 'xml')):
            return '', decoded, 'text', []
        return '', '', 'unknown', ['unsupported_content_type']
    tree = html.fromstring(body)
    title = ' '.join(tree.xpath('//title/text()')).strip()
    text = trafilatura.extract(body, url=url, include_comments=True, include_tables=True) or ''
    if text.strip():
        return title, text, 'html_main', []
    # Visible text is retained only as a review candidate, not automatic success.
    for node in tree.xpath('//script|//style|//nav|//header|//footer|//svg|//noscript'):
        node.drop_tree()
    nodes = tree.xpath('//article|//main|//*[@role="main"]') or [tree]
    text = max(('\n'.join(s.strip() for s in n.itertext() if s.strip()) for n in nodes), key=len, default='')
    return title, text, 'visible_text_fallback', ['main_content_not_isolated']


def assess(url, text, title='', target_status=None, error=None, final_url=None,
           flags=None, structured=False, primary_text=False):
    flags = list(flags or [])
    if error:
        return 'unresolved', error_reason(str(error)), [str(error)[:700]]
    if target_status is not None and int(target_status) >= 400:
        return 'unresolved', 'target_http_' + str(target_status), flags
    envelope = text or ''
    target_warning = re.search(r'^Warning: Target URL returned error (\d{3})', envelope, re.M)
    if target_warning:
        return 'unresolved', 'target_http_' + target_warning[1], flags
    text = envelope.split('Markdown Content:', 1)[-1]
    text = re.sub(r'!\[[^\]]*\]\([^\n]*?\)', '', text)
    text = re.sub(r'\[([^\]]*)\]\([^\n]*?\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text).strip()
    low, heading = text.lower(), (title or '').strip().lower()
    if not text:
        return 'unresolved', 'empty_content', flags
    if text.startswith('%PDF-'):
        return 'unresolved', 'unparsed_pdf_stream', flags
    if low.startswith('something went wrong. wait a moment') and len(text) < 1000:
        return 'unresolved', 'site_error_message', flags
    # Error words inside a question/post are not evidence of a failed page.
    error_title = re.match(r'^(?:page not found\b|access denied\b|just a moment[.!]*$|403\s*[-:]|404(?:\s*[-:]?\s*(?:not found|page not found))?$)', heading)
    if error_title and not (structured and primary_text):
        return 'unresolved', 'error_or_challenge_page', flags
    if not (structured and primary_text):
        if re.search(r'^(?:403 forbidden|404 not found|access denied|verify you are human|checking your browser)', low) and len(text) < 1500:
            return 'unresolved', 'error_or_challenge_page', flags
        if any(s in low[:1500] for s in ('verify you are human', 'checking your browser', 'enable javascript and cookies')):
            flags.append('possible_access_challenge')
        if ('page not found' in low or '404' in low[:1000]) and len(text) < 1500:
            flags.append('error_text_requires_context_review')
    if 'maybe requiring CAPTCHA' in envelope or 'maybe not yet fully loaded' in envelope:
        flags.append('provider_warns_incomplete_or_captcha')
    if final_url and valid_url(final_url):
        original, final = urlsplit(url), urlsplit(final_url)
        if host(url) != host(final_url):
            flags.append('changed_destination_host_requires_identity_review')
        if any(x in final.path.lower() for x in ('/login', '/signin', '/authwall', '/checkpoint')) and final.path != original.path:
            return 'unresolved', 'login_redirect', flags
        if original.path.strip('/') and final.path in ('', '/', '/home', '/home/') and host(url) == host(final_url):
            flags.append('deep_url_redirected_to_homepage')
        elif original.path.rstrip('/') != final.path.rstrip('/'):
            flags.append('changed_destination_path_requires_identity_review')
    paywall = ('subscribe to continue reading', 'subscribe to read the full', 'unlock this article',
               'create a free account to read the full', 'member-only story', 'register to view this lesson')
    if any(s in low for s in paywall):
        flags.append('preview_or_paywall')
    k = kind(url)
    if k in ('video', 'channel', 'profile'):
        flags.append('media_or_profile_text_only')
    if host(url) == 'instagram.com':
        flags.append('caption_not_image_or_video_body')
    if structured and not primary_text:
        flags.append('metadata_or_title_only')
    if len(text) < (80 if structured else 350):
        flags.append('short_content_requires_review')
    if flags:
        return 'partial', flags[0], list(dict.fromkeys(flags))
    return 'content_obtained', 'substantive_text_candidate', ['Automatic screening; full completeness and factual accuracy unverified.']


def error_reason(error):
    low = error.lower()
    if 'customer is not active' in low: return 'provider_account_state_error'
    if 'blocked by bright data' in low: return 'provider_domain_block'
    if 'parse error' in low or 'parse_error' in low: return 'provider_parse_error'
    if 'could not resolve' in low or 'name resolution' in low: return 'dns_error'
    if 'certificate' in low: return 'tls_error'
    if 'captcha' in low or 'protection page' in low: return 'access_challenge'
    if 'waiting for selector' in low: return 'render_timeout'
    if 'dead page (404' in low: return 'provider_reports_target_404'
    if 'status 410' in low: return 'provider_reports_target_410'
    return 'provider_or_transport_error'


def structured_text(item):
    body_fields = ('content', 'post_text', 'post_content', 'body', 'text', 'article_body', 'description', 'caption')
    parts = []; primary = False
    for key in ('title', 'name', 'profile_name', 'full_name', *body_fields, 'Description', 'biography', 'about', 'summary', 'transcript'):
        value = item.get(key)
        if key == 'transcript' and isinstance(value, list):
            value = '\n'.join(str(v.get('text', '')) if isinstance(v, dict) else str(v) for v in value)
        if isinstance(value, str) and value.strip():
            parts.append(key + ':\n' + value.strip())
            primary |= key in body_fields or key == 'transcript'
    for key in ('comments', 'answers', 'replies', 'latest_comments', 'top_comments', 'top_visible_comments'):
        for value in item.get(key) or []:
            if isinstance(value, dict):
                value = dict(value)
                if 'comment' in value and 'text' not in value: value['text'] = value['comment']
                result, _ = structured_text(value)
                if result: parts.append(key + ':\n' + result)
    return '\n\n'.join(dict.fromkeys(parts)), primary


def selected(record):
    attempts = record.get('attempts', [])
    if not attempts: return None
    return max(attempts, key=lambda a: (STATUSES[a['status']], bool(a.get('manual_review')),
                                       bool(a.get('primary_text')), a.get('content_chars', 0)))


def export(out, source, urls, field, extra_summary=None):
    import html
    out = Path(out); result = copy.deepcopy(source); rows = []
    jobs = [json.loads(p.read_text()) for p in (out/'jobs').glob('*.json')]
    pending = {u for j in jobs if j['state'] not in ('processed', 'rejected', 'failed') for u in j['url_ids']}
    for uid, entry in urls.items():
        path = out/'records'/f'{uid}.json'
        record = json.loads(path.read_text()) if path.exists() else {**entry, 'attempts': []}
        a = selected(record)
        rec = {'url_id': uid, 'requested_url': entry['url'], 'status': a['status'] if a else 'pending',
               'pending_job': uid in pending, 'provider': a['provider'] if a else None,
               'reason': a['reason'] if a else 'not_fetched', 'content': '',
               'validation_level': 'manual_context_review' if a and a.get('manual_review') else 'automatic_screen',
               'completeness': 'unverified', 'attempts_file': f'records/{uid}.json' if path.exists() else None}
        if a:
            for key in ('title', 'final_url', 'checked_at', 'notes', 'content_file', 'raw_file', 'retrieval_scope', 'content_format'):
                rec[key] = a.get(key)
            if a.get('content_file') and a['status'] != 'unresolved':
                rec['content'] = (out/a['content_file']).read_text(encoding='utf-8')
        for pointer in entry['occurrences']:
            at(result, pointer)[field] = rec
        rows.append({**rec, 'occurrences': entry['occurrences'], 'attempts': record['attempts']})
    save(out/'enriched.json', result)
    with (out/'results.jsonl').open('w', encoding='utf-8') as f:
        for row in rows:
            line = json.dumps(row, ensure_ascii=False)
            for c in ('\u0085', '\u2028', '\u2029'): line = line.replace(c, '\\u'+format(ord(c), '04x'))
            f.write(line+'\n')
    save(out/'needs_followup.json', [x for x in rows if x['status'] != 'content_obtained' or x['pending_job']])
    status_counts = {s: sum(x['status'] == s for x in rows) for s in (*STATUSES, 'pending')}
    summary = {'updated_at': now(), 'unique_urls': len(urls), 'occurrences': sum(len(x['occurrences']) for x in urls.values()),
               'status_counts': status_counts, 'pending_jobs': sum(j['state'] not in ('processed','rejected','failed') for j in jobs),
               'unprocessed_urls': sum(not x['attempts'] for x in rows),
               'manually_reviewed_selected_urls': sum(x['validation_level']=='manual_context_review' for x in rows),
               'automatic_screen_only': not any(x['validation_level']=='manual_context_review' for x in rows)}
    summary.update(extra_summary or {})
    # Verify every original field, including fields outside the selected top K.
    stripped = copy.deepcopy(result)
    for entry in urls.values():
        for pointer in entry['occurrences']: del at(stripped, pointer)[field]
    assert stripped == source
    summary['all_original_fields_preserved'] = True
    save(out/'summary.json', summary)
    e = html.escape
    tr = []
    for x in rows:
        link = f'<a href="{e(x["attempts_file"])}">尝试记录</a>' if x['attempts_file'] else ''
        tr.append(f'<tr><td>{e(str(x["requested_url"]))}</td><td>{x["status"]}</td><td>{e(x["reason"])}</td><td>{link}<details><summary>文字预览</summary><pre>{e(x["content"][:1000])}</pre></details></td></tr>')
    (out/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>URL 正文抓取</title><style>body{font:15px/1.6 system-ui;margin:32px;color:#182a3f}table{border-collapse:collapse;width:100%}td,th{padding:12px;border:1px solid #dbe3ec;text-align:left;vertical-align:top;overflow-wrap:anywhere}td:first-child{max-width:480px}pre{white-space:pre-wrap}a{color:#2467b1}</style><h1>URL 正文抓取</h1><p>content_obtained 表示通过正文初筛，不保证全文完整；partial 保留预览、简介和待复核内容；unresolved 未取得可用正文；pending 尚未完成。</p><pre>'+e(json.dumps(summary, ensure_ascii=False, indent=2))+'</pre><p><a href="enriched.json">原字段＋正文</a> · <a href="results.jsonl">逐 URL 记录</a> · <a href="needs_followup.json">待处理清单</a></p><table><tr><th>URL</th><th>状态</th><th>原因</th><th>证据</th></tr>'+''.join(tr)+'</table>', encoding='utf-8')
    return summary
