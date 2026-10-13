# Security posture

Trusted local prototype only, with no guaranteed security updates or monitored disclosure channel. IPv4 loopback binding and Host/Origin/fetch-site checks are enforced; browser CSP restricts connections/images and framing by default. It is not a multi-user hosting boundary. Other local processes can use the unauthenticated default context.

JSON, SQLite and browser localStorage are not encrypted storage. A private runtime key replaces the old shared development HMAC default; it remains outside source. Optional outbound services require explicit network opt-in and may receive planning/location data; those live paths were not exercised. Do not place credentials, personal plans or raw exports in public issues/contributions.
