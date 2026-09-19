"""Read-only email capture and durable semantic order interpretation."""

import base64
import copy
import hashlib
import time
from datetime import datetime
from email.utils import parseaddr
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re

from pypdf import PdfReader
from photo_intake import write_json


class OrderEmailError(ValueError):
    """Fixed diagnostic codes; never include email content or credentials."""


def normalize(value):
    return re.sub(r'[^A-Z0-9]', '', (value or '').upper())


def parts(payload):
    yield payload
    for child in payload.get('parts', []):
        yield from parts(child)


def decode(data):
    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div', 'tr', 'td', 'li'):
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def message_text(payload, fetch_attachment=None):
    plain, html = [], []
    for part in parts(payload):
        if part.get('filename') or part.get('mimeType') not in ('text/plain', 'text/html'):
            continue
        body = part.get('body', {})
        if body.get('size', 0) > 500_000:
            raise OrderEmailError('email_body_too_large')
        if body.get('data'):
            raw = decode(body['data'])
        elif body.get('attachmentId') and fetch_attachment:
            raw = fetch_attachment(body['attachmentId'])
        else:
            continue
        text = raw.decode('utf-8', 'replace')
        (plain if part['mimeType'] == 'text/plain' else html).append(text)
    if plain:
        return '\n'.join(plain)
    parser = HTMLText()
    parser.feed('\n'.join(html))
    return ''.join(parser.text)


def date_value(value):
    return datetime.strptime(value, '%m/%d/%Y').date().isoformat()


def require(pattern, text, flags=0):
    match = re.search(pattern, text, flags)
    if not match:
        raise OrderEmailError('unsupported_email_layout')
    return match


def parse_fisher(source):
    """Parse only layouts with unambiguous order, catalog, unit and quantity fields.

    A different supplier or changed template goes to review instead of guessing.
    No links are followed and no source content is executed.
    """
    body, subject = source['text'], source['subject']
    if not re.search(r'FisherCustomerService\.US@thermofisher\.com', body, re.I):
        raise OrderEmailError('unsupported_supplier')
    order_id = require(r'\bOrder(?:#|:)\s*([A-Z0-9-]+)', subject, re.I)[1]
    po = require(r'\bPO(?:#|:)\s*([A-Z0-9-]+)', subject, re.I)[1]
    document = {'supplier': 'Fisher Scientific', 'order_id': order_id, 'purchase_order': po,
                'message_id': source['id'], 'thread_id': source['thread_id'], 'items': []}
    if 'Your order has been shipped' in subject:
        text = re.sub(r'<https?://[^>]*>', '', body)
        text = re.sub(r'\[https?://[^\]]*\]', '', text)
        if require(r'Order Number:\s*([A-Z0-9-]+)', text)[1] != order_id or require(r'P\.O\. Number:\s*([A-Z0-9-]+)', text)[1] != po:
            raise OrderEmailError('shipping_order_mismatch')
        document.update(kind='shipping_confirmation', order_date=date_value(require(r'Order Date:\s*(\d{2}/\d{2}/\d{4})', text)[1]),
                        requester=require(r'Order Placed By:\s*(?:Attn\s*)?([^\r\n]+)', text)[1].strip())
        chunks = list(re.finditer(r'Shipment number:\s*(\d+)', text))
        for index, shipment in enumerate(chunks):
            chunk = text[shipment.end():chunks[index + 1].start() if index + 1 < len(chunks) else len(text)]
            pattern = (r'([^\r\n]+)\s+Catalog number:\s*([A-Za-z0-9-]+)\s+'
                       r'Shipment date:\s*(\d{2}/\d{2}/\d{4})\s+'
                       r'(Case of \d+|Pack of \d+|Each)\s+(\d+) of (\d+)')
            matches = list(re.finditer(pattern, chunk))
            for item in matches:
                product, catalog, shipped_date, pack, shipped, ordered = item.groups()
                document['items'].append({'catalog': catalog, 'product': product.strip(), 'specifications': None,
                    'quantity_ordered': int(ordered), 'unit': 'case' if pack.startswith('Case') else 'pack' if pack.startswith('Pack') else 'each',
                    'pack_size': pack, 'shipment': shipment[1].lstrip('0') or '0',
                    'quantity_shipped': int(shipped), 'shipment_date': date_value(shipped_date),
                    'tracking': [], 'invoice_notification': None, 'unit_price': None, 'extended_price': None})
        # Reject partial extraction: every catalog block must have been understood.
        if len(document['items']) != len(re.findall(r'Catalog number:', text)):
            raise OrderEmailError('incomplete_shipping_items')
        # A tracking-bearing template needs an explicit parser before auto-import.
        if 'Tracking information is currently unavailable' not in text:
            raise OrderEmailError('tracking_layout_requires_review')
    elif 'INVOICE NOTIFICATION' in subject.upper():
        if not source.get('pdf_texts'):
            raise OrderEmailError('invoice_pdf_required')
        document.update(kind='invoice_notification')
        for pdf in source['pdf_texts']:
            text = pdf['text']
            if 'This is not an Invoice - Do not Remit Payment' not in text or 'FISHER SCIENTIFIC' not in text:
                raise OrderEmailError('unsupported_invoice_document')
            header = require(r'\b' + re.escape(order_id) + r'\s+(\d{2}/\d{2}/\d{4})\s+(\d+)\s+(\d{2}/\d{2}/\d{4})', text)
            if po not in text:
                raise OrderEmailError('invoice_order_mismatch')
            document['order_date'] = date_value(header[1])
            # Keep placement attention distinct from the mail forwarder.
            requester = re.search(r'\bATTN\s+([A-Z][A-Z ]+?)\s*\n', text)
            document['requester'] = requester[1].strip().title() if requester else None
            ship_dates = {str(int(m[1])): date_value(m[2]) for m in re.finditer(r'^\s*(\d{3})\s+(\d{2}/\d{2}/\d{4})', text, re.M)}
            pattern = r'^\s*(\d+)\s+(\d+)\s+(CS|PK|EA)\s+([A-Z0-9-]+)\s+(.+?)\s{2,}([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$'
            items = list(re.finditer(pattern, text, re.M))
            if not items:
                raise OrderEmailError('invoice_items_unreadable')
            if len(items) != len(re.findall(r'^\s*\d+\s+\d+\s+(?:CS|PK|EA)\s+', text, re.M)):
                raise OrderEmailError('incomplete_invoice_items')
            for item in items:
                shipment, qty, unit, catalog, description, price, extended = item.groups()
                spec = re.search(r'\b(\d+(?:\.\d+)?)\s*(ML|UL|L)\b', description)
                pack = re.search(r'\b(\d+)CS\b', description)
                document['items'].append({'catalog': catalog, 'product': description,
                    'specifications': (spec[1] + ' ' + {'ML': 'mL', 'UL': 'uL', 'L': 'L'}[spec[2]]) if spec else None,
                    'quantity_ordered': None, 'unit': {'CS': 'case', 'PK': 'pack', 'EA': 'each'}[unit],
                    'pack_size': 'Case of ' + pack[1] if pack else None, 'shipment': str(int(shipment)),
                    'quantity_shipped': int(qty), 'shipment_date': ship_dates.get(str(int(shipment))),
                    'tracking': [], 'invoice_notification': header[2],
                    'unit_price': price.replace(',', ''), 'extended_price': extended.replace(',', '')})
    else:
        raise OrderEmailError('unsupported_order_document')
    if not document['items']:
        raise OrderEmailError('no_order_items')
    return document


class GmailOrderIntake:
    def __init__(self, root, config, transport):
        self.root, self.config, self.transport = Path(root), config, transport
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, path, **params):
        return self.transport.request('GET', 'https://gmail.googleapis.com/gmail/v1/users/me/' + path, params=params)

    def capture(self):
        cursor_path = self.root / 'scan.json'
        cursor = json.loads(cursor_path.read_text()) if cursor_path.exists() else {}
        after = max(self.config['start_epoch'], int(cursor.get('last_scan', 0)) - 172800)
        query = f"after:{after} from:{self.config['forwarder']} -in:sent"
        token = None
        import time
        started = int(time.time())
        for _ in range(20):
            params = {'q': query, 'maxResults': 100}
            if token:
                params['pageToken'] = token
            page = self.get('messages', **params)
            for item in page.get('messages', []):
                mid = item['id']
                if not re.fullmatch(r'[a-f0-9]+', mid):
                    raise OrderEmailError('invalid_message_id')
                folder = self.root / mid
                folder.mkdir(exist_ok=True)
                if (folder / 'source.json').exists():
                    continue
                raw = self.get('messages/' + mid, format='full')
                write_json(folder / 'gmail.json', raw)
                headers = {h['name'].lower(): h['value'] for h in raw['payload'].get('headers', [])}
                if parseaddr(headers.get('from', ''))[1].lower() != self.config['forwarder'].lower():
                    raise OrderEmailError('unexpected_forwarder')
                def attachment(aid):
                    return decode(self.get('messages/' + mid + '/attachments/' + aid)['data'])
                source = {'id': mid, 'thread_id': raw['threadId'], 'subject': headers.get('subject', ''),
                          'received_ms': raw['internalDate'], 'text': message_text(raw['payload'], attachment),
                          'pdf_texts': [], 'image_attachments': [], 'attachment_warnings': []}
                attachments = [p for p in parts(raw['payload']) if p.get('filename') or p.get('mimeType', '').startswith('image/') or p.get('mimeType') == 'application/pdf']
                for index, part in enumerate(attachments):
                    name, body = part.get('filename', ''), part.get('body', {})
                    if part.get('mimeType', '').startswith('image/'):
                        if index >= 20 or body.get('size', 0) > 20_000_000:
                            source['attachment_warnings'].append('attachment_limit');continue
                        data = decode(body['data']) if body.get('data') else attachment(body['attachmentId'])
                        try:
                            from photo_intake import image_details
                            from PIL import Image
                            if len(data)>20_000_000:raise OrderEmailError('image_too_large')
                            image_details(data)
                            local_file = f'image-{index + 1}.png'
                            with Image.open(io.BytesIO(data)) as picture:
                                picture.convert('RGB').save(folder / local_file)
                            source['image_attachments'].append({'local_file':local_file,
                                'sha256':hashlib.sha256((folder / local_file).read_bytes()).hexdigest()})
                        except Exception:
                            source['attachment_warnings'].append('image_unreadable')
                        continue
                    if not name.lower().endswith('.pdf') and part.get('mimeType')!='application/pdf':
                        source['attachment_warnings'].append('unsupported_attachment')
                        continue
                    if index >= 10 or body.get('size', 0) > 10_000_000:
                        source['attachment_warnings'].append('attachment_limit')
                        continue
                    data = decode(body['data']) if body.get('data') else attachment(body['attachmentId'])
                    if len(data) > 10_000_000 or not data.startswith(b'%PDF-'):
                        source['attachment_warnings'].append('invalid_pdf_attachment')
                        continue
                    (folder / f'attachment-{index + 1}.pdf').write_bytes(data)
                    try:
                        reader = PdfReader(io.BytesIO(data))
                        if len(reader.pages) > 30:
                            raise OrderEmailError('pdf_page_limit')
                        text = '\n'.join(p.extract_text(extraction_mode='layout') or '' for p in reader.pages)
                    except Exception:
                        source['attachment_warnings'].append('pdf_needs_review')
                        continue
                    if len(text) < 30 or len(text) > 200_000:
                        source['attachment_warnings'].append('pdf_needs_review')
                        continue
                    source['pdf_texts'].append({'filename': name, 'text': text})
                write_json(folder / 'source.json', source)
            token = page.get('nextPageToken')
            if not token:
                write_json(cursor_path, {'last_scan': started})
                return
        raise OrderEmailError('email_scan_page_limit')


def parsed_documents(result):
    return result.get('documents', [result['document']] if result.get('document') else [])


def known_records(results):
    records={}
    for result in results:
        if result.get('status')!='parsed':continue
        for d in parsed_documents(result):
            for i in d['items']:
                identity='|'.join(normalize(v) for v in (d['supplier'],d['order_id'],i['catalog']))
                records[identity]={'source':'known:'+hashlib.sha256(identity.encode()).hexdigest()[:16],
                    'supplier':d['supplier'],'order_id':d['order_id'],'catalog':i['catalog'],
                    'product':i['product'],'specifications':i.get('specifications'),
                    'pack_size':i.get('pack_size')}
    return list(records.values())


def source_fingerprint(source,folder):
    content={key:source.get(key) for key in ('subject','text','pdf_texts','image_attachments','attachment_warnings')}
    content['pdf_files']=[(p.name,hashlib.sha256(p.read_bytes()).hexdigest())
                          for p in sorted(Path(folder).glob('attachment-*.pdf'))]
    return hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()


def parse_pending(root, reader=None, clock=time.time):
    from email_reader import SemanticEmailReader
    from label_reader import ReaderError
    root=Path(root);results=[]
    paths=sorted(root.glob('*/source.json'),key=lambda p:int(json.loads(p.read_text(encoding='utf-8')).get('received_ms','0')))
    cached=[json.loads(p.with_name('order.json').read_text(encoding='utf-8')) for p in paths if p.with_name('order.json').exists()]
    cooldown_path=root/'reader-cooldown.json'
    cooldown=json.loads(cooldown_path.read_text()) if cooldown_path.exists() else {}
    reuse={}
    for path in paths:
        output=path.with_name('order.json')
        if not output.exists():continue
        saved=json.loads(output.read_text(encoding='utf-8'))
        if saved.get('status') not in ('parsed','ignored'):continue
        original=json.loads(path.read_text(encoding='utf-8'))
        fingerprint=saved.get('source_fingerprint') or source_fingerprint(original,path.parent)
        reuse[fingerprint]=(saved,original.get('received_ms','0'))
    for path in paths:
        output=path.with_name('order.json')
        state=json.loads(output.read_text(encoding='utf-8')) if output.exists() else {'status':'pending','attempts':0}
        # Keep successful historical results stable. Only old template failures
        # are reconsidered automatically by the new semantic reader.
        terminal=state['status'] in ('parsed','ignored') or (state['status']=='needs_review' and state.get('reader_version')==2)
        if terminal or clock()<state.get('retry_at',0):
            results.append(state);continue
        source=json.loads(path.read_text(encoding='utf-8'))
        fingerprint=source_fingerprint(source,path.parent)
        if fingerprint in reuse:
            result,observed=reuse[fingerprint]
            result=copy.deepcopy(result)
            for doc in parsed_documents(result):
                doc.update(message_id=source['id'],thread_id=source['thread_id'])
                doc.setdefault('observed_ms',observed)
            if len(result.get('documents',[]))==1:result['document']=result['documents'][0]
            result.update(source_fingerprint=fingerprint,reused_identical_source=True)
            write_json(output,result);results.append(result);continue
        if clock()<cooldown.get('retry_at',0):
            state.update(status='waiting_usage',retry_at=cooldown['retry_at']);write_json(output,state)
            results.append(state);continue
        attempts=state.get('attempts',0)+1
        state.update(status='analyzing',attempts=attempts,reader_version=2);write_json(output,state)
        try:
            reader=reader or SemanticEmailReader()
            result=reader.read(source,path.parent,known_records([*cached,*results]))
            result['attempts']=attempts
            result['source_fingerprint']=fingerprint
            if result['status'] in ('parsed','ignored'):reuse[fingerprint]=(result,source.get('received_ms','0'))
        except Exception as error:
            code=str(error) if isinstance(error,ReaderError) else type(error).__name__
            quota=code=='codex_usage_limit'
            result={'status':'waiting_usage' if quota else 'needs_review' if attempts>=3 else 'retry_wait',
                    'error':code,'reader_version':2,'attempts':attempts-1 if quota else attempts,
                    'retry_at':clock()+(1800 if quota else 60)}
            if quota:
                cooldown={'retry_at':result['retry_at'],'error':code};write_json(cooldown_path,cooldown)
        write_json(output,result);results.append(result)
    return results
