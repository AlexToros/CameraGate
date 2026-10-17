# Video Viewer

## Scope and workflow

- Deployment to the user's VPS with both bogdanova34.site and bogdanova34.ru is authorized. Both hosts must serve the same portal without redirecting one to the other. GitHub: public AlexToros/video-viewer; main requires owner review for collaborators, with owner bypass. Never give collaborators administrator access merely to push changes.
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

- Top-level boundaries: frontend/, backend/ (Python package), database/, infrastructure/, deployment/, scripts/dev/ and tests/backend/. Keep runtime state in root .runtime/; do not move databases or keys when reorganizing source. backend/paths.py resolves source paths independently of cwd.
- Launch: sh scripts/dev/start.sh or python -m backend. Native subprocesses use python -m backend.cameras.vesta with cwd set to the repository root; the optional gateway uses python -m backend.cameras.gateway. Keep PWA/public asset URLs unchanged when moving their files.

- `backend/server.py`: aiohttp HTTP/API and one multiplexed WebSocket per browser. Bind loopback locally. PUBLIC_ORIGIN/COOKIE_SECURE configure a future HTTPS entrypoint.
- `backend/store.py`, `database/schema.sql`: PostgreSQL 17, transactional bootstrap/invitations, server sessions, ACL snapshots and saved per-user grids. Passwords use salted PBKDF2; upstream credentials use Fernet with a separate runtime key.
- Initial SQL is idempotent; introduce versioned migrations before changing an already deployed schema.
- Active users/rights/sessions are an in-memory bulk snapshot, refreshed atomically after mutations. This currently requires one portal process; replicas require shared invalidation and source coordination.
- `backend/cameras/streams.py`: one lazy shared producer per unique camera; frames stay in memory. Stop unused producers. Do not duplicate upstream connections per viewer/tile.
- `backend/cameras/vesta.py` owns only the native transport/decoder. Credentials enter worker stdin, never command-line arguments. Native media workers skip redundant catalog queries; catalog discovery is separate.
- `backend/cameras/gateway.py` and `backend/cameras/gateway_client.py`: optional token-authenticated media worker. Browser access to the gateway is forbidden. Linux x86_64 can run native workers with CAMERA_SDK_PATH pointing to its separate ELF library and FFMPEG_BINARY=/usr/bin/ffmpeg; macOS keeps Rosetta and dylib. Proprietary binaries stay outside git and are mounted read-only.
- `frontend/app.js`, `frontend/style.css`: vanilla UI; wall presets 1/4/6/9/16, custom widths and reordering. Tile picture height follows decoded image aspect ratio; a ResizeObserver measures intrinsic tiles for dense CSS grid row spans. Legacy saved height stays compatible but no longer stretches the picture. Keep captions minimal. Camera chooser supports search/groups; preview shares the existing WebSocket and cached frames. Mobile layout may reduce visible columns without changing the saved layout. Keyboard/touch reordering is available in the tile options dialog.
- Invitation links must be actual clickable anchors. Handle hash navigation in an already open app; ignore late responses from a previous route so they cannot replace the registration form.
- Admin tab navigation renders immediately and ignores responses for detached tab bodies. Show a retry action for failed loads. Mobile admin tables wrap into cards, coarse-pointer tile controls have larger touch targets. Disconnect tile observers on route/logout; clear fullscreen layout when leaving the session.
- Manifest/icons/service worker implement PWA. Only public app-shell assets may enter CacheStorage. Phone installation requires trusted HTTPS; desktop localhost works for local preview.
- `infrastructure/compose.yaml` keeps the local optional gateway setup. `deployment/compose.production.yaml` runs native Linux transport, PostgreSQL and nginx with memory/log limits; only nginx publishes 80/443. Encrypted connection data requires the matching /data/secret.key when migrating the DB. Do not expose initial bootstrap publicly before restoring the existing user database.
- Production TLS: Certbot webroot /var/www/letsencrypt, HTTP challenge in infrastructure/nginx/acme.conf, infrastructure/nginx/production.conf after issuance. deployment/scripts/enable-https.sh installs the deploy hook and enables certbot.timer; deployment/scripts/renew-certificate.sh validates and reloads nginx after renewal.
- TRUST_PROXY=1 is production-only: app has no published port, nginx overwrites X-Real-IP, and login throttles use that validated client IP. Never enable it with direct untrusted access to app. The local optional gateway compose leaves it off.
- PUBLIC_ORIGIN is the primary host; ADDITIONAL_PUBLIC_ORIGINS is an explicit comma-separated allowlist. Select CSP/WebSocket origin by the configured request Host, and require Origin to match that host, even when another host is also allowed. Certbot certificate name bogdanova34-portal covers both domains; HTTPS readiness marker is certificate-specific.

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

- `.runtime/venv/bin/python -m unittest discover -s tests -t . -v`; PostgreSQL suites require TEST_DATABASE_URL and use disposable schemas.
- Verify authorization, concurrent bootstrap/invite consumption, active stream revocation/blocking and layout persistence against real PostgreSQL.
- Exercise Chromium desktop/mobile and real streams separately. Artificial image fixtures prove UI/ACL behavior, not real camera availability.
- For wall changes, verify actual frame proportions (including 4:3 and widescreen), non-overlapping tiles, search, shared preview, width/order, add/remove and persistence. Check 320/390/768/1440 widths, touch controls and delayed admin responses without touching real accounts.
- Keep CSP enabled during product validation. Playwright waitForFunction uses eval and conflicts with CSP; use locators/polling instead of weakening product policy.
- `node --check frontend/app.js`, `node --check frontend/sw.js`, `docker compose -f infrastructure/compose.yaml config --quiet`.

## Current state

- Local portal implemented with PostgreSQL/PWA; the user has created a real account. Preserve users and layouts during routine development and restarts.
- User confirmed live video works with VPN disabled. Earlier missing-media diagnostics do not establish a recorder or decoder fault; do not resume connection experiments without a new reason.
- VPS: Ubuntu x86_64; deploy from a root session. aspyankov has SSH access but no sudo/Docker privilege; do not grant root-equivalent group membership as a deployment shortcut.
- Source reorganization is local only. The VPS still runs the preceding flat-layout release. Before deploying this layout, reinstall systemd units and certificate deploy hook from deployment/ as documented there; persistent paths, Compose project name and volume names stay unchanged.
- Production containers built and started; existing user/layout migrated with the matching encryption key and without local sessions. Authenticated API and two real JPEG streams verified on Linux. Domain is newly registered: REG.RU authoritative servers publish A correctly, but .ru delegation was still NXDOMAIN at deployment time. Initial HTTPS timer waits for DNS; certificate issuance and renewal dry-run remain unverified until delegation propagates.
