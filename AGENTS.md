# Video Viewer

## Scope and workflow

- Local development only. Do not deploy to the VPS without a new explicit request.
- Keep changes in small coherent commits. Never commit `.env`, `.runtime/`, databases, SDK binaries, secrets, diagnostics or camera images.
- Shared global user instructions and `/Users/aspyankov/.codex/RTK.md` apply. Prefix shell commands with `rtk`.
- This file holds repository architecture and implementation context as requested by the user. Temporary reports and detailed task journals belong outside git.

## Product requirements

- Bootstrap the first administrator once; no default credentials. Two roles: admin and user.
- Users sign in, see only permitted cameras and save their own configurable video wall.
- Administrators can issue one-use invitations with preassigned first/last names, role and camera/group grants. Registration asks only for login and password.
- Administrators manage users, blocking, grants, camera groups and globally visible camera names.
- The built-in group «Все камеры» always contains all cameras. Administrators implicitly have access to it.
- Device connection settings (ID, UserName, Password) are write-only in the API/UI. Missing settings produce a useful viewer error.
- Blocking and permission changes must affect existing sessions and active streams, not only new requests.
- PostgreSQL in all environments; do not introduce SQLite. Keep Docker Compose and nginx configuration for later deployment, but simplify the local preview.
- Installable PWA is required. Finish core flows first, then verify responsive layouts and touch controls. Never cache authenticated API responses or video in the service worker.
- Show the install button only on mobile platforms (including iPad desktop UA), and hide it inside the installed app. A narrow desktop window is not a mobile platform.
- Invitation registration greets the assigned last/first name, explains the login, and asks for login/password/password confirmation. A mismatch must not submit or consume the invite.

- Password minimum is 6 characters, maximum 128, in both forms and backend. Existing users must survive routine restarts.
- Delete accepted and revoked invitations physically; purge expired/legacy inactive invitations in bulk. Keep only pending invitations in the UI.
- Keep the wall concise: no wall title, autosave progress/success labels or explanatory grid-fit text. Save silently and show errors when saving fails.

## Portal architecture

- `server.py`: aiohttp HTTP/API and one multiplexed WebSocket per browser. Bind loopback locally. PUBLIC_ORIGIN/COOKIE_SECURE configure a future HTTPS entrypoint.
- `store.py`, `schema.sql`: PostgreSQL 17, transactional bootstrap/invitations, server sessions, ACL snapshots and saved per-user grids. Passwords use salted PBKDF2; upstream credentials use Fernet with a separate runtime key.
- Initial SQL is idempotent; introduce versioned migrations before changing an already deployed schema.
- Active users/rights/sessions are an in-memory bulk snapshot, refreshed atomically after mutations. This currently requires one portal process; replicas require shared invalidation and source coordination.
- `streams.py`: one lazy shared producer per unique camera; frames stay in memory. Stop unused producers. Do not duplicate upstream connections per viewer/tile.
- `vesta.py` owns only the native transport/decoder. Credentials enter worker stdin, never command-line arguments. Native media workers skip redundant catalog queries; catalog discovery is separate.
- `gateway.py` and `gateway_client.py`: optional token-authenticated media worker for a Linux portal container. Browser access to the gateway is forbidden. Full Linux camera transport remains a separate prerequisite; macOS dylib is not portable.
- `app.js`, `style.css`: vanilla UI, wall presets/custom sizes/reordering, administration. Mobile layout may reduce visible columns without changing the saved layout. Keyboard/touch reordering is available in the tile-size dialog.
- Invitation links must be actual clickable anchors. Handle hash navigation in an already open app; ignore late responses from a previous route so they cannot replace the registration form.
- Manifest/icons/service worker implement PWA. Only public app-shell assets may enter CacheStorage. Phone installation requires trusted HTTPS; desktop localhost works for local preview.
- `compose.yaml`: PostgreSQL, portal and nginx, persistent DB/key volumes, file secrets. Only nginx publishes a loopback port. No VPS deployment is authorized.

## Existing video transport

- Original prototype: Python HTTP server, single shared selected camera, MJPEG browser player.
- VestaCloud uses XMEye/Xiongmai P2P and DVRIP. Only the low-level `vv_nat_*` functions are used; the high-level vendor login crashed.
- macOS x86_64 vendor library lives in ignored `.runtime/libnetsdk.dylib`. Rosetta is required on Apple Silicon for the transport process; FFmpeg is native.
- Never expose serial number, device login/password, password-equivalent hashes or raw SDK output. Native stdout/stderr are silenced before loading the library.
- DVRIP media must be routed by message id, assembled across packets and decoded only from a complete keyframe. Reject packets from another DVRIP session.
- SDK send length includes a 12-byte transport envelope. Start-monitor does not necessarily return an acknowledgement.
- Each stream must isolate stale frames by generation. The browser must wait for the actual new image before revealing it.
- Do not confuse SDK connection caching with web caching. Disabling the old SDK cache was a compatibility workaround, not a proven speed optimization.
- Dynamic P2P discovery remains the default. An operator may override it with `P2P_SERVER` or ignored `.runtime/p2p-server` when a discovered node cannot reach the recorder. Public node addresses can change; never bake one into the source.

## Validation

- `.runtime/venv/bin/python -m unittest -v test_protocol.py test_store.py test_api.py test_gateway.py`; PostgreSQL suites require TEST_DATABASE_URL and use disposable schemas.
- Verify authorization, concurrent bootstrap/invite consumption, active stream revocation/blocking and layout persistence against real PostgreSQL.
- Exercise Chromium desktop/mobile and real streams separately. Artificial image fixtures prove UI/ACL behavior, not real camera availability.
- Keep CSP enabled during product validation. Playwright waitForFunction uses eval and conflicts with CSP; use locators/polling instead of weakening product policy.
- `node --check app.js`, `node --check sw.js`, `docker compose config --quiet`.

## Current state

- Local portal implemented with PostgreSQL/PWA; first real administrator is deliberately left for the user to create.
- Docker configuration is prepared; Docker daemon was stopped during development, so container build/run is not yet verified.
- The VPS was cleared of GRACE in earlier work and is not part of this implementation.
