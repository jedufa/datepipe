# DatePipe

### Make time for each other. Keep the surprise.

Finding a free evening is one thing. Turning it into a thoughtful surprise takes a little more: an idea, a time window, a hint, and a few details worth keeping together.

DatePipe is a phone-first local prototype for planning dates with a partner and trusted accomplices. Explore ideas, make a plan, and keep its reveal hint, backup and checklist in one place.

## A look inside

<table>
  <tr>
    <td valign="top"><strong>Find an idea</strong><br><img src="docs/screenshots/discovery.png" alt="DatePipe browser showing fictional date suggestions" width="250"></td>
    <td valign="top"><strong>Make a plan</strong><br><img src="docs/screenshots/planning.png" alt="A fictional jazz evening with a hint, backup and Make this plan button" width="250"></td>
    <td valign="top"><strong>Keep the details together</strong><br><img src="docs/screenshots/plan.png" alt="Saved fictional plan with reveal hint, notes, backup and checklist" width="250"></td>
  </tr>
</table>

Actual browser captures at a 390 × 844 phone viewport, using independently authored fictional data. These are browser screenshots, not native Android captures. Demo links use `.invalid` and do not lead to real venues.

## From a free evening to a small surprise

- **Find a starting point.** Browse the fictional idea library with deterministic matching and calendar windows.
- **Keep the surprise manageable.** Save a visible hint alongside the details you want to reveal later.
- **Remember the practical bits.** Keep a fallback, attachments and a checklist with each plan.
- **Come back to it.** Plans and completed checklist steps survive a server restart; browser preferences stay locally.

The source also includes trusted-accomplice planning fields and account prototypes. Those flows are not a verified hosted sharing service. DatePipe is a planning prototype, not a booking service, venue guide or general dating platform. Demo scores describe heuristics and field completeness, not real event quality.

## Run the local demo

Requires Python **3.11+** on a Unix-like host; verified on Linux with **Python 3.14.4**. Runtime uses only the standard library. No API key, account, external checkout or pip package is needed to start.

```sh
git clone https://github.com/jedufa/datepipe.git
cd datepipe
python3 server.py --host 127.0.0.1 --port 8812
```

Open **http://127.0.0.1:8812/**. Complete the fictional onboarding, find date ideas, open one, choose **Make this plan**, inspect its details and check off a step. Restart the server to verify persistence. Add your own ideas and verify real places before booking anything.

| Scope | Verified facts |
| --- | --- |
| Primary demo | Python backend and phone-sized browser UI |
| Runtime dependencies | Python standard library; native browser APIs |
| Engineering checks | 103 backend/web tests; nine rendered Chromium checks |
| Storage | Local JSON/files, pipeline SQLite, browser localStorage |
| Native/mobile binaries | Android and iOS excluded; no APK or store release |
| License | [MIT](LICENSE) for first-party code/tests; [upstream notices](THIRD_PARTY_NOTICES.md) separately |

## Test and explore

With Python, Node **22.22.1** and a fresh disposable environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m pytest -q -p no:cacheprovider tests/backend tests/web
node --check static/app.js
python3 -m py_compile server.py datepipe_backend/*.py
```

Node runs the frontend sorting regression as well as syntax checking. See [build and test](docs/BUILD_AND_TEST.md) for exact scope and limitations. `server.py` provides HTTP routes and planning, `datepipe_backend/` holds scoring/persistence helpers, and `static/` is the UI. Tests retain product regressions and synthetic fixtures. A small weekly-research report helper remains because existing tests import it.

## Privacy and boundaries

The server accepts only IPv4 loopback binding, rejects foreign Host/Origin/browser fetch-site requests, and disables backend outbound HTTP by default. It is a trusted single-machine prototype; local processes can access its default browser context. It is not safe to expose as a hosted or shared-machine service.

State and reports default to ignored `runtime/`; browser preferences and feedback use localStorage. Ordinary files are not an encrypted vault. An automatically generated private per-state key supports account hashing: retain it with state if using those prototype accounts. The live-data source-ZIP endpoint is disabled. See [security](SECURITY.md).

Optional AI, source scouting, geocoding and image fetching require explicit `DATEPIPE_ALLOW_NETWORK=1`; these integrations are unverified and can send planning context or location to external services. The default synthetic demo makes no external requests. Deliberately following an external link remains a browser navigation.

The release omits native Android, original couple/research/catalog data, account-backed research tools, live calendars, deployment/gateway integration and private history. Physical devices, geolocation/permissions, actual venue accuracy, provider calls, hosted sharing and production security are unverified. Test coverage percentage has not been measured.

## Take it further

The source is open so someone else can study the planning workflow, adapt it to their own life, or carry it forward. **Experimental and unmaintained, looking for a maintainer.** Independent forks are welcome; support, PR review, security updates and future releases are not guaranteed. Start with [contributing](CONTRIBUTING.md).

Copyright (c) 2026 Jean-Francois Dufour. First-party code and tests are [MIT licensed](LICENSE); third-party materials retain their own terms.
