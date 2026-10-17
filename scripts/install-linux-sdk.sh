#!/bin/sh
# Keep the proprietary transport outside git and the image; verify the pinned binary.
set -eu
cd "$(dirname "$0")/.."
[ "$(uname -m)" = x86_64 ] || { echo 'Linux SDK requires x86_64' >&2; exit 1; }
mkdir -p .runtime
sdk_file=.runtime/libxmnetsdk.so
sdk_sha=f37d5a36c2223e1e6774208382dd536b71e733c9ca84e6dbdd60a0c4653cd018
if [ ! -f "$sdk_file" ]; then
  curl --fail --location --retry 3 --output "$sdk_file.download" \
    'https://raw.githubusercontent.com/1996scarlet/IPCamera-CPython-Interface/313c705de436ddb73f97ef914a64c71068cc3a55/xm_interface_v4/sdk/x64/libxmnetsdk.so'
  printf '%s  %s\n' "$sdk_sha" "$sdk_file.download" | sha256sum --check --status
  mv "$sdk_file.download" "$sdk_file"
fi
printf '%s  %s\n' "$sdk_sha" "$sdk_file" | sha256sum --check --status
chmod 644 "$sdk_file"
