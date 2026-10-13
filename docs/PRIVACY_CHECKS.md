# Repository privacy checks

Run `python3 tools/privacy_check.py` and
`python3 -m unittest discover -s tools -p test_privacy_check.py` from the checkout.
The checker uses only Python's standard library and local Git. CI checks pushes and
pull requests with read-only permissions, complete fetched history, and no retained
checkout credentials. It does not call external scanning services or use secrets.

Checks cover tracked source, locally reachable branch/tag/PR-ref history, commit
messages, author/committer and annotated tagger email addresses, common credential
formats, literal passwords, private paths/hosts/chat links, environment/signing
files, databases, data-export filenames and some personal-record fields. PNG
text/EXIF chunks, JPEG EXIF/XMP/IPTC/comments, DOCX identity/custom XML and PDF
review requirements are included. Findings show only class, path and line; values
are never printed. Credential-shaped filenames are redacted too.

`tools/privacy_allowlist.json` records individually reviewed synthetic fixtures and
required upstream attribution. Each exception binds to a rule, line and exact file
SHA-256. It cannot suppress a changed file. Do not add exceptions for live secrets
or personal operator data. Changes to the checker and its exceptions need careful
maintainer review; this workflow is a detection check, not a repository protection
rule that prevents administrators from pushing.

Reserved example/test/invalid domains and GitHub noreply identities are permitted.
Known programming namespaces are distinguished from private hostnames. An exception
for a synthetic test credential does not exempt that file from other categories.
Do not remove upstream copyright notices merely because they contain contact data.

Pattern matching cannot prove the absence of personal data. Ordinary names, prose,
image pixels, compressed/encoded data, unknown credential formats and proprietary
rights need human review. GitHub may retain unreachable commits, pull-request refs,
release attachments or cached SHA views outside the refs fetched by Actions. This
check does not inspect GitHub settings, external clones, forks, search indexes,
remote release assets or account-private data. Release binaries need a separate
review; the gate does not change or rebuild application/release content.

Before adding screenshots or fixtures, use fictional data, inspect the pixels and
metadata, and review the exact bytes. Keep local exports and credentials ignored.
Use the repository owner's verified GitHub noreply identity for both author and
committer. In GitHub Settings > Emails, verify both **Keep my email addresses
private** and **Block command line pushes that expose my email**. Enabling these
settings does not repair previously published commit objects.
