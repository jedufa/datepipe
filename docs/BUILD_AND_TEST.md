# Build and test

Private candidate, not licensed or published. Python 3.14.4/Linux, Node 22.22.1 syntax checking, pytest 9.0.2 with hashed dependency pins. Runtime has no third-party imports; Python 3.11+ language/runtime APIs are required but that minimum version was not separately exercised. Unix O_NOFOLLOW is used for private local key files, so Windows is unsupported/unverified here.

Use the exact README newcomer commands. Tests run with disposable HOME/state/temp directories, no inherited API keys, pytest plugin autoload disabled, external HTTP transport disabled by default, synthetic profiles/catalogs and fake provider transports.

All 102 backend/web tests passed after adapting two real-catalog expectations to fictional-host/source-variety contracts. Two static contracts for omitted Android deployment surfaces were removed; functional account/persistence/matching/planning/research-helper tests remain. Fifty synthetic couple/city model cases keep their original quality thresholds. Nine additional local-boundary executions cover denied binds, foreign requests, explicit network opt-in with fake transport, key persistence across a separate process and refusal of a symlink key.

Initial run: 99 passed, 3 failed from removed real-catalog assumptions and inadequate synthetic source variety. The demo was diversified and only catalog-specific expectations adapted; model thresholds were not weakened. All logs/failed receipts are retained privately.

Browser workflow evidence is recorded in private RC_VALIDATION.md. Native Android/emulator, iOS, physical-device permissions/geolocation, live venue/event accuracy, real provider calls and hosted deployment are not verified. This is prototype engineering evidence, not production security or release Qualification. Coverage percentage has not been measured.
