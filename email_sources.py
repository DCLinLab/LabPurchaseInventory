"""Prepare bounded email text, images, and rendered PDF pages for visual reading."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from pypdf import PdfReader
from label_reader import ReaderError, CodexLabelReader, clean_environment


def renderer():
    found=shutil.which('pdftoppm')
    if found:return found
    bundled=Path(os.environ.get('USERPROFILE',''))/'.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin/pdftoppm.exe'
    if bundled.is_file():return str(bundled)
    raise ReaderError('pdf_renderer_unavailable')


def prepare(source, folder, temporary):
    folder,temporary=Path(folder).resolve(),Path(temporary)
    chunks={'email': 'Subject: '+source.get('subject','')+'\n'+source.get('text','')}
    for i,pdf in enumerate(source.get('pdf_texts',[]),1):
        chunks[f'pdf-text:{i}']=pdf['text']
    images=[];image_ids=[]
    for file in sorted(folder.glob('attachment-*.pdf')):
        if file.resolve().parent != folder or file.stat().st_size > 10_000_000:
            raise ReaderError('invalid_email_attachment')
        try:pages=len(PdfReader(file).pages)
        except Exception as error:raise ReaderError('unreadable_pdf') from error
        if not 1 <= pages <= 20 or len(images)+pages>20:
            raise ReaderError('email_page_limit')
        prefix=temporary/file.stem
        command=[renderer(),'-png','-scale-to','2000',str(file),str(prefix)]
        result=subprocess.run(command,capture_output=True,timeout=120,env=clean_environment(),**CodexLabelReader.process_options())
        if result.returncode:raise ReaderError('pdf_render_failed')
        rendered=sorted(temporary.glob(file.stem+'-*.png'),key=lambda p:int(p.stem.rsplit('-',1)[1]))
        if len(rendered)!=pages:raise ReaderError('incomplete_pdf_render')
        images.extend(rendered);image_ids.extend(f'{file.name}:page:{i}' for i in range(1,pages+1))
    for item in source.get('image_attachments',[]):
        file=(folder/item['local_file']).resolve()
        if file.parent!=folder or file.stat().st_size>20_000_000 or hashlib.sha256(file.read_bytes()).hexdigest()!=item['sha256']:
            raise ReaderError('email_image_integrity_failed')
        images.append(file);image_ids.append(item['local_file'])
    if len(images)>20:raise ReaderError('email_page_limit')
    if sum(len(s) for s in chunks.values())>250000:raise ReaderError('email_text_limit')
    return chunks,images,image_ids
