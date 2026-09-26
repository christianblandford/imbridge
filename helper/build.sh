#!/bin/zsh
# Builds imbridge's helper: the BlueBubbles Private API helper at the commit pinned in helper/UPSTREAM, with the
# patches in helper/patches applied in order. Needs only the Xcode Command Line Tools, plus git and network access
# the first time (to fetch upstream).
#
#   helper/build.sh [output.dylib]      (default: src/imbridge/helper/imbridge-helper.dylib)
set -euo pipefail
HERE=${0:A:h}
ROOT=${HERE:h}
source "$HERE/UPSTREAM"
OUT=${1:-$ROOT/src/imbridge/helper/imbridge-helper.dylib}
OUT=${OUT:A}
WORK=$ROOT/build/upstream

# Fetch the pinned commit once, then reset it to a clean tree and apply the patch series.
if [[ ! -d $WORK/.git ]]; then
  git init -q "$WORK"
  git -C "$WORK" remote add origin "$HELPER_REPO"
fi
if ! git -C "$WORK" cat-file -e "$HELPER_COMMIT^{commit}" 2>/dev/null; then
  git -C "$WORK" fetch -q --depth 1 origin "$HELPER_COMMIT"
fi
git -C "$WORK" checkout -q -f --detach "$HELPER_COMMIT"
git -C "$WORK" clean -q -fdx
for patch in "$HERE"/patches/*.patch; do
  git -C "$WORK" apply "$patch"
done

SRC=$WORK/Messages/MacOS-11+
SDK=$(xcrun --sdk macosx --show-sdk-path)
mkdir -p "${OUT:h}"

# Messages is an arm64e process, so that is the only slice that matters (loading it needs -arm64e_preview_abi).
# Link exactly what the stock BlueBubbles build links. ChatKit is deliberately absent: on macOS 26+ it lives under
# /System/iOSSupport, and the helper only reaches its classes at runtime through NSClassFromString.
clang -arch arm64e -mmacosx-version-min=11.5 -dynamiclib -fobjc-arc -fmodules -O2 -w \
  -isysroot "$SDK" -F"$SDK/System/Library/PrivateFrameworks" \
  -I"$SRC/BlueBubblesHelper" -I"$SRC/BlueBubblesHelper/ZKSwizzle" -I"$SRC/Pods/CocoaAsyncSocket/Source/GCD" \
  "$SRC"/BlueBubblesHelper/*.m "$SRC/BlueBubblesHelper/ZKSwizzle/ZKSwizzle.m" \
  "$SRC/Pods/CocoaAsyncSocket/Source/GCD/GCDAsyncSocket.m" \
  -framework Foundation -framework AppKit -framework CoreSpotlight -framework CoreLocation \
  -framework Security -framework CFNetwork -framework CoreServices \
  -framework IMCore -framework IMSharedUtilities -framework IMDPersistence -framework IDS -framework FMF \
  -install_name "@rpath/${OUT:t}" -o "$OUT"
codesign --force --sign - "$OUT"
echo "built $OUT"
