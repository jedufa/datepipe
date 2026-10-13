"""Synthetic negative controls: no real credentials or personal records are used."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import zipfile
import zlib

import privacy_check as check


class PrivacyTests(unittest.TestCase):
    def rules(self, path, data):
        return {rule for rule, _ in check.scan_blob(path, data)}

    def test_safe_examples(self):
        self.assertEqual(self.rules('README.md', b'Contact demo@example.invalid; /home/user/project'), set())
        self.assertEqual(self.rules('test.py', b'"a@example.test"'), set())
        self.assertEqual(self.rules('.env.example', b'API_KEY="replace-me"'), set())

    def test_personal_email(self):
        value=('private.person'+'@'+'mail-provider.com').encode()
        self.assertIn('personal_email', self.rules('note.txt',value))

    def test_token_formats(self):
        for value in ['ghp_'+'a'*36, 'sk-'+'b'*32, 'AKIA'+'Z'*16]:
            self.assertIn('provider_token',self.rules('config.txt',value.encode()))

    def test_private_key(self):
        value=('-----BEGIN '+'PRIVATE KEY-----').encode()
        self.assertIn('private_key',self.rules('notes.txt',value))

    def test_literal_credentials(self):
        self.assertIn('literal_credential',self.rules('config.py',b'pass'+b'word = "arbitrary-value-1234"'))

    def test_personal_paths(self):
        for text in ['/home/'+'realperson/private', 'C:\\Users\\'+'realperson\\file']:
            self.assertIn('personal_home',self.rules('note.txt',text.encode()))

    def test_private_network_and_namespace(self):
        for text in ['http://'+'office'+'.local/', '"192.168.'+'4.5"']:
            self.assertIn('private_host',self.rules('config.txt',text.encode()))
        self.assertEqual(self.rules('source.py',b'x = threading.local()'),set())
        self.assertEqual(self.rules('Source.kt',b'import product.data.local.Store'),set())

    def test_private_chat_and_credential_url(self):
        self.assertIn('private_chat',self.rules('note.md',('https://chatgpt.com/'+'c/private-conversation').encode()))
        scheme='https://'
        self.assertIn('credential_url',self.rules('config',(scheme+'user:secret@host.example.invalid').encode()))

    def test_sensitive_files_and_records(self):
        for name in ['.env','secrets.env','signing.p12','secret.key','app.sqlite3']:
            self.assertIn('sensitive_file',self.rules(name,b'content'))
        self.assertIn('database_content',self.rules('renamed.bin',b'SQLite format 3\0payload'))
        self.assertIn('personal_record',self.rules('data.json',b'{"student_'+b'name":"Fictional Person"}'))
        self.assertIn('personal_data_file',self.rules('exports/records.json',b'{}'))

    @staticmethod
    def png(kind=None):
        def chunk(k,d):return struct.pack('>I',len(d))+k+d+struct.pack('>I',zlib.crc32(k+d)&0xffffffff)
        data=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))
        if kind:data+=chunk(kind,b'fixture metadata')
        return data+chunk(b'IDAT',zlib.compress(b'\0\0\0\0'))+chunk(b'IEND',b'')

    def test_png_metadata(self):
        self.assertEqual(self.rules('shot.png',self.png()),set())
        for kind in [b'tEXt',b'zTXt',b'iTXt',b'eXIf']:
            self.assertIn('png_private_metadata',self.rules('shot.png',self.png(kind)))
        self.assertIn('image_trailing_data',self.rules('shot.png',self.png()+b'extra'))

    def test_jpeg_metadata(self):
        clean=b'\xff\xd8\xff\xd9';self.assertEqual(self.rules('shot.jpg',clean),set())
        for marker in [b'\xe1',b'\xed',b'\xfe']:
            data=b'\xff\xd8\xff'+marker+b'\x00\x06Exif\xff\xd9'
            self.assertIn('jpeg_private_metadata',self.rules('shot.jpg',data))

    def test_docx_and_pdf(self):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            z.writestr('docProps/core.xml','<dc:creator>Fictional Creator</dc:creator>')
            z.writestr('customXml/item1.xml','<fixture/>')
        self.assertTrue({'document_identity_metadata','document_custom_xml'}<=self.rules('notes.docx',out.getvalue()))
        self.assertIn('pdf_requires_privacy_review',self.rules('notes.pdf',b'%PDF-1.7'))

    def test_bad_media_fails_closed(self):
        self.assertIn('invalid_image',self.rules('shot.png',b'invalid'))
        self.assertIn('invalid_image',self.rules('shot.jpg',b'invalid'))
        self.assertIn('invalid_document',self.rules('notes.docx',b'invalid'))

    def test_path_redaction(self):
        value='private'+'@'+'mail-provider.com'
        self.assertNotIn(value,check.safe_path('exports/'+value+'.txt'))

    def test_history_identity_deleted_file_and_exact_exception(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);env=os.environ.copy()
            for kind in ['AUTHOR','COMMITTER']:
                env['GIT_'+kind+'_NAME']='Synthetic Test'
                env['GIT_'+kind+'_EMAIL']='test@example.invalid'
            def git(*args, override=None):
                return subprocess.run(['git','-C',d,*args],check=True,capture_output=True,env=override or env)
            git('init','-b','main');content=('ghp_'+'x'*36).encode();(root/'old.txt').write_bytes(content)
            git('add','old.txt');git('commit','-m','Synthetic test')
            git('rm','old.txt');git('commit','-m','Remove fixture')
            result=check.scan(root);self.assertFalse(result['ok']);self.assertEqual(result['findings'][0]['scope'],'history')
            exception={'path':'old.txt','rule':'provider_token','line':1,'blob_sha256':hashlib.sha256(content).hexdigest(),'reason':'Synthetic regression fixture'}
            self.assertTrue(check.scan(root,[exception])['ok'])
            bad=dict(exception,blob_sha256='0'*64);self.assertFalse(check.scan(root,[bad])['ok'])
            private=env.copy()
            for kind in ['AUTHOR','COMMITTER']:private['GIT_'+kind+'_EMAIL']='fictional'+'@'+'mail-provider.com'
            git('commit','--allow-empty','-m','Identity fixture',override=private)
            git('tag','-a','fixture-tag','-m','Tag fixture',override=private)
            rules={x['rule'] for x in check.scan(root,[exception])['findings']}
            self.assertTrue({'commit_identity','tagger_identity'}<=rules)
            scanner=Path(check.__file__).resolve()
            p=subprocess.run(['python3',str(scanner),'--root',d],capture_output=True,text=True)
            self.assertEqual(p.returncode,1)
            self.assertNotIn(private['GIT_AUTHOR_EMAIL'],p.stdout+p.stderr)
            self.assertNotIn(content.decode(),p.stdout+p.stderr)

if __name__=='__main__':unittest.main()
