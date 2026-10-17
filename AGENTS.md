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

## Existing video transport

- Original prototype: Python HTTP server, single shared selected camera, MJPEG browser player.
- VestaCloud uses XMEye/Xiongmai P2P and DVRIP. Only the low-level `vv_nat_*` functions are used; the high-level vendor login crashed.
- macOS x86_64 vendor library lives in ignored `.runtime/libnetsdk.dylib`. Rosetta is required on Apple Silicon for the transport process; FFmpeg is native.
- Never expose serial number, device login/password, password-equivalent hashes or raw SDK output. Native stdout/stderr are silenced before loading the library.
- DVRIP media must be routed by message id, assembled across packets and decoded only from a complete keyframe. Reject packets from another DVRIP session.
- SDK send length includes a 12-byte transport envelope. Start-monitor does not necessarily return an acknowledgement.
- Each stream must isolate stale frames by generation. The browser must wait for the actual new image before revealing it.
- Do not confuse SDK connection caching with web caching. Disabling the old SDK cache was a compatibility workaround, not a proven speed optimization.

## Validation

- `python3 -m unittest -v test_protocol.py` verifies the existing transport framing and recovery behavior.
- Add focused authorization, invitation-consumption, permission-revocation and layout-persistence tests with the portal.
- Exercise the UI in Chromium, including a delayed-image race and multiple camera tiles.

## Current state

- Baseline recorded before implementing the local authenticated portal.
- Local camera server is stopped. The VPS was cleared of GRACE and is not part of this implementation.
