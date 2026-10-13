#!/usr/bin/env python3
"""Bounded, dependency-free privacy checks. Diagnostics never include matched values."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import zipfile

MAX_BYTES = 32 * 1024 * 1024
SAFE_EMAIL = re.compile(r"@(?:users\.noreply\.github\.com|(?:[a-z0-9-]+\.)*example\.(?:com|org|net|invalid)|[^@]*\.(?:invalid|test)|localhost)$", re.I)
EMAIL = re.compile(r"(?<![\w/])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w])", re.I)
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "provider_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{35}|xox[baprs]-[A-Za-z0-9-]{15,})\b"),
    "personal_home": re.compile(r"(?:/(?:home|Users)/(?!user(?:/|\b)|example(?:/|\b)|runner(?:/|\b)|<)[A-Za-z0-9_.-]+|[A-Z]:[\\/]+Users[\\/]+(?!user\b|example\b|Public\b)[A-Za-z0-9_.-]+)", re.I),
    "private_host": re.compile(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|[a-z0-9][a-z0-9.-]*\.(?:local|internal|lan))\b(?!\.[A-Za-z0-9])", re.I),
    "private_chat": re.compile(r"https?://(?:chatgpt\.com/(?:c/|g/[^\s]+/c/)|chat\.openai\.com/c/|claude\.ai/chat/)[A-Za-z0-9_-]+", re.I),
    "credential_url": re.compile(r"(?:https?|ssh|ftp)://[^\s/:]+:[^\s/@]+@", re.I),
    "literal_credential": re.compile(r'''(?i)["']?(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret|auth[_-]?token)["']?\s*[:=]\s*["']([^"'\n]{4,})["']'''),
    "personal_record": re.compile(r'''(?i)["'](?:student[_-]?name|pupil[_-]?name|full[_-]?name|date[_-]?of[_-]?birth|home[_-]?address|medical[_-]?history|diary[_-]?entry|personal[_-]?notes|partner[_-]?name)["']\s*:\s*["'][^"'\n]+["']'''),
}
PLACEHOLDER = re.compile(r"^(?:example|sample|synthetic|dummy|test(?:ing)?|fake|placeholder|change[-_]?me|replace[-_]?me|redacted|your[-_])(?:\b|[-_])", re.I)


def git(root, *args):
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if p.returncode:
        raise ValueError("Git read failed (details suppressed)")
    return p.stdout


def safe_path(path):
    # Names are also untrusted; do not emit credential-shaped names or controls.
    if EMAIL.search(path.replace("/", " ")) or any(ord(c) < 32 for c in path) or any(PATTERNS[k].search(path) for k in ("provider_token", "private_chat", "personal_home")):
        return "redacted-path-" + hashlib.sha256(path.encode()).hexdigest()[:12]
    return path


def text_findings(text):
    out = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for m in EMAIL.finditer(line):
            if not SAFE_EMAIL.search(m[0]): out.append(("personal_email", line_no))
        for rule, pattern in PATTERNS.items():
            for m in pattern.finditer(line):
                if rule == "literal_credential" and (PLACEHOLDER.search(m[1]) or m[1].startswith(("${", "<", "os.environ", "process.env"))):
                    continue
                if rule == "private_host" and not re.match(r"\d", m[0]):
                    if line.lstrip().startswith(("import ", "package ")) or m[0] in {"threading.local", "tz.local"}:
                        continue
                out.append((rule, line_no))
    return sorted(set(out))


def metadata_findings(path, data):
    out = []
    suffix = Path(path).suffix.lower()
    if data.startswith(b"\x89PNG\r\n\x1a\n") or suffix == ".png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"): return [("invalid_image", 0)]
        p = 8
        while p < len(data):
            if p + 12 > len(data): return out + [("invalid_image", 0)]
            size, kind = struct.unpack(">I4s", data[p:p + 8]); end = p + size + 12
            if end > len(data): return out + [("invalid_image", 0)]
            if kind in {b"tEXt", b"zTXt", b"iTXt", b"eXIf"}: out.append(("png_private_metadata", 0))
            p = end
            if kind == b"IEND":
                if p != len(data): out.append(("image_trailing_data", 0))
                break
        return sorted(set(out))
    if data.startswith(b"\xff\xd8") or suffix in {".jpg", ".jpeg"}:
        if not data.startswith(b"\xff\xd8"): return [("invalid_image", 0)]
        p = 2
        while p < len(data):
            if data[p] != 255: return out + [("invalid_image", 0)]
            while p < len(data) and data[p] == 255: p += 1
            if p == len(data): return out + [("invalid_image", 0)]
            marker = data[p]; p += 1
            if marker in {0xDA, 0xD9}: break
            if marker in range(0xD0, 0xD8) or marker == 1: continue
            if p + 2 > len(data): return out + [("invalid_image", 0)]
            size = int.from_bytes(data[p:p + 2], "big")
            if size < 2 or p + size > len(data): return out + [("invalid_image", 0)]
            if marker in {0xE1, 0xED, 0xFE}: out.append(("jpeg_private_metadata", 0))
            p += size
        return sorted(set(out))
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                members=z.infolist()
                if sum(i.file_size for i in members) > MAX_BYTES: return [("document_too_large", 0)]
                for info in members:
                    if info.filename.startswith("customXml/"): out.append(("document_custom_xml", 0))
                    if info.filename.startswith(("docProps/", "word/")) and info.filename.endswith(".xml"):
                        value=z.read(info).decode("utf-8", errors="replace")
                        out.extend(text_findings(value))
                        if re.search(r"<(?:dc:creator|cp:lastModifiedBy|vt:lpwstr)[^>]*>[^<]+",value):out.append(("document_identity_metadata",0))
        except (ValueError, OSError, zipfile.BadZipFile): out.append(("invalid_document", 0))
    if suffix == ".pdf":
        # A PDF needs review even if no literal metadata is visible: streams may be compressed.
        out.append(("pdf_requires_privacy_review", 0))
    return sorted(set(out))


def scan_blob(path, data):
    findings = []
    name=Path(path).name.lower();suffix=Path(path).suffix.lower()
    template=name.endswith((".example", ".sample", ".template"))
    if ((name.startswith(".env") or name == "secrets.env" or name.endswith(".env")) and not template) or suffix in {".key", ".pem", ".p12", ".p8", ".jks", ".keystore", ".sqlite", ".sqlite3", ".db"} or name in {"id_rsa", "id_ed25519", "key.properties"}:
        findings.append(("sensitive_file", 0))
    if re.search(r"(?:^|[/_.-])(?:exports?|backups?|attachments|diary|students?|pupils?)(?:[/_.-]|$)",path,re.I) and suffix in {".json", ".csv", ".jsonl", ".txt", ".zip", ".sql"}:
        findings.append(("personal_data_file", 0))
    if data.startswith(b"SQLite format 3\0"): findings.append(("database_content", 0))
    if len(data)>MAX_BYTES: return findings+[("oversized_uninspected_file",0)]
    findings.extend(metadata_findings(path,data))
    if b"\0" not in data:
        try: findings.extend(text_findings(data.decode("utf-8")))
        except UnicodeError: pass
    return sorted(set(findings))


def scan(root, exceptions=()):
    findings=[];seen=set();suppressed=set();blob_count=0
    allowed={}
    for row in exceptions:
        if set(row)!={"path","rule","line","blob_sha256","reason"} or not row["reason"].strip() or not re.fullmatch(r"[a-f0-9]{64}",row["blob_sha256"]):raise ValueError("Invalid reviewed exception")
        allowed[(row["path"],row["rule"],row["line"],row["blob_sha256"])]=row["reason"]
    def record(path,data,where):
        nonlocal blob_count
        digest=hashlib.sha256(data).hexdigest();key=(path,digest)
        if key in seen:return
        seen.add(key);blob_count+=1
        for rule,line in scan_blob(path,data):
            fp=(path,rule,line,digest)
            if fp in allowed:suppressed.add(fp);continue
            findings.append({"rule":rule,"path":safe_path(path),"line":line,"scope":where,"blob_sha256":digest})
    # Working tree plus ALL locally reachable branch/tag/PR-ref histories.
    for raw in git(root,"ls-files","-z").split(b"\0"):
        if not raw:continue
        name=raw.decode("utf-8",errors="surrogateescape");p=root/name
        if p.is_symlink():record(name,str(p.readlink()).encode(),"symlink")
        elif p.is_file():record(name,p.read_bytes(),"tracked")
        else:findings.append({"rule":"tracked_file_missing","path":safe_path(name),"line":0,"scope":"tracked"})
    commits=git(root,"rev-list","--all").decode().splitlines()
    objects={}
    for sha in commits:
        raw=git(root,"cat-file","commit",sha);headers,message=raw.split(b"\n\n",1)
        for line in headers.splitlines():
            if line.startswith((b"author ",b"committer ")):
                match=re.search(rb"<([^<>]+)>",line)
                if not match or not SAFE_EMAIL.search(match[1].decode(errors="replace")):
                    findings.append({"rule":"commit_identity","path":"commit:"+sha+":"+line.split()[0].decode(),"line":0,"scope":"history"})
        record("commit-message:"+sha,message,"history")
        for row in git(root,"ls-tree","-r","-z",sha).split(b"\0"):
            if not row:continue
            meta,name=row.split(b"\t",1);mode,kind,oid=meta.split()
            if kind==b"blob":objects[(name.decode(errors="surrogateescape"),oid.decode())]=True
    for name,oid in objects:record(name,git(root,"cat-file","blob",oid),"history")
    tags=git(root,"for-each-ref","--format=%(objectname) %(objecttype)","refs/tags").decode().splitlines()
    for row in tags:
        oid,kind=row.split()
        if kind!="tag":continue
        raw=git(root,"cat-file","tag",oid);headers,message=raw.split(b"\n\n",1)
        match=re.search(rb"^tagger .* <([^<>]+)>",headers,re.M)
        if not match or not SAFE_EMAIL.search(match[1].decode(errors="replace")):findings.append({"rule":"tagger_identity","path":"tag-object:"+oid,"line":0,"scope":"history"})
        record("tag-message:"+oid,message,"history")
    return {"ok":not findings,"findings":findings,"commits_checked":len(commits),"unique_path_contents_checked":blob_count,"reviewed_exceptions_used":len(suppressed),"scope":"Tracked files and locally reachable Git refs; no claim about remote caches, unadvertised refs, external clones, image pixels or arbitrary encoded data."}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--root",type=Path,default=Path.cwd());parser.add_argument("--json",action="store_true");args=parser.parse_args()
    try:
        p=args.root/"tools/privacy_allowlist.json";exceptions=json.loads(p.read_text()) if p.exists() else []
        result=scan(args.root,exceptions)
    except Exception:
        print("Privacy check could not complete; details suppressed.",file=sys.stderr);return 2
    if args.json:print(json.dumps(result,indent=2,ensure_ascii=True))
    else:
        for row in result["findings"]:print(f"{row['rule']}: {row['path']}:{row['line']} (value redacted)")
        print(f"Privacy check: {'PASS' if result['ok'] else 'FAIL'}; {len(result['findings'])} findings; {result['commits_checked']} commits; {result['reviewed_exceptions_used']} exact reviewed exceptions.")
    return 0 if result["ok"] else 1
if __name__=="__main__":raise SystemExit(main())
