# DatePipe

A phone-first local prototype for private surprise-date planning with a partner and trusted accomplices: calendar windows, an idea backlog, discreet plans, reveal hints, checklists, attachments and deterministic matching.

Experimental and unmaintained, looking for a maintainer; independent forks are welcome once licensing is established. There is no guaranteed support, PR review or security update. **This private candidate is not licensed or published: DatePipe-specific first-party distribution authority remains unresolved.**

## Scope and quick start

This snapshot is the standalone Python backend and mobile-sized browser UI. Native Android, original research/catalog data, browser-account research tooling, deployment/gateway integration and historical evidence are omitted. No mobile binary or hosted service is provided.

Requires Python 3.11+ on a Unix-like host; verified with Python 3.14.4 on Linux. Windows/macOS and native devices are unverified. Runtime uses only Python's standard library; no API key, pip dependency, external checkout or network service is required.

```sh
python3 server.py --host 127.0.0.1 --port 8812
```

Open **http://127.0.0.1:8812/**. Start with the fictional demo, pick a calendar week, find ideas, make one plan, inspect its reveal hint/backup/checklist, then restart to check persistence. All people, venues and ideas in `data/` are fictional. Links use `.invalid`; they are not real booking or event recommendations. Add your own plans and verify actual places yourself.

For tests in a new disposable environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m pytest -q -p no:cacheprovider tests/backend tests/web
node --check static/app.js
python3 -m py_compile server.py datepipe_backend/*.py
```

Node is needed only for JavaScript syntax checking. Exact results and limits are in [build and test](docs/BUILD_AND_TEST.md).

## Local and network behavior

The server refuses non-loopback binds and rejects foreign Host/Origin/browser fetch-site requests. It is a trusted single-machine prototype, **not a hosted or shared-machine multi-user service**. Local processes can access the default browser context; prototype account endpoints do not change that trust boundary.

Backend outbound HTTP is off unless explicitly enabled with `DATEPIPE_ALLOW_NETWORK=1`. Without opt-in, AI, source scouting, geocoding and image fetching fall back or report unavailable. Images and connections are limited to this local origin by default; clicking an external link is still a deliberate browser navigation. Optional network features are unverified in this source candidate and can send planning context or location to external services. No real API billing occurred in validation.

State, pipeline SQLite and reports are local under ignored `runtime/` by default; browser preferences/feedback use localStorage. The prototype stores ordinary JSON/files, not an encrypted vault. Fallback account hashing uses an automatically generated private per-state runtime key; preserve that key with state if prototype accounts are used. Do not put real private data in a source archive. The old live-data source-ZIP endpoint is disabled; use the reviewed Git source snapshot.

## Structure and licensing

`server.py` provides HTTP routes and planning. `datepipe_backend/` contains matching, source/event scoring and persistence helpers; `static/` contains the phone-first UI. `tests/` retains product regressions and synthetic fixtures. `developer/tools/run_weekly_event_research.py` is retained only because existing tests import its small report helper; other developer infrastructure is absent.

No first-party LICENSE has been assigned. MIT is a conditional release preference, not an ownership grant. Separately licensed test dependencies have their original texts under `third_party/`; see [notices](THIRD_PARTY_NOTICES.md).
