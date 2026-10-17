#!/bin/sh
# Downloads the original manufacturer transport; never installs the VMS app.
set -eu
cd "$(dirname "$0")"
mkdir -p .runtime
if [ ! -f .runtime/libnetsdk.dylib ]; then
  curl --fail --location --output .runtime/vesta.rar 'https://t54.ru/downloads/vesta/VMS_VeSta_MACOS_V1.21.0.2.T.20210202.rar'
  tar -xf .runtime/vesta.rar -C .runtime
  mkdir -p .runtime/sdk-mount
  hdiutil attach -readonly -nobrowse -mountpoint "$PWD/.runtime/sdk-mount" .runtime/VMS_VeSta_MACOS_V1.21.0.2.T.20210202.dmg
  trap 'hdiutil detach "$PWD/.runtime/sdk-mount" >/dev/null' EXIT
  cp '.runtime/sdk-mount/VMS Vesta.app/Contents/Frameworks/libnetsdk.dylib' .runtime/libnetsdk.dylib
  hdiutil detach "$PWD/.runtime/sdk-mount"
  trap - EXIT
  rm .runtime/vesta.rar .runtime/VMS_VeSta_MACOS_V1.21.0.2.T.20210202.dmg
fi
uv pip install --python /usr/bin/python3 --target .runtime/packages imageio-ffmpeg==0.6.0
if [ ! -x .runtime/venv/bin/python ]; then
  uv venv --python 3.14 .runtime/venv
fi
uv pip install --python .runtime/venv/bin/python -r requirements.txt
echo 'Готово. Запуск: sh start.sh'
