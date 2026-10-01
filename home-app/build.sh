#!/bin/zsh
# Build Life Home (Mac Catalyst) and install it to ~/Applications. See FINDINGS.md for why the
# SystemCapabilities patch and the iOS app ID are needed.
set -e
cd "${0:A:h}"
[[ -n ${LIFE_MCP_APPLE_TEAM:-} ]] || source ~/.zshrc.local
[[ -n ${LIFE_MCP_APPLE_TEAM:-} ]] || { print "Set LIFE_MCP_APPLE_TEAM (your Apple developer team id) in ~/.zshrc.local"; exit 1 }
xcodegen generate --quiet
sed -i '' 's/SystemCapabilities = "\[\\"com.apple.HomeKit\\": \[\\"enabled\\": 1\]\]";/SystemCapabilities = { com.apple.HomeKit = { enabled = 1; }; };/' LifeHome.xcodeproj/project.pbxproj
xcodebuild -project LifeHome.xcodeproj -scheme LifeHome -destination 'platform=macOS,variant=Mac Catalyst' \
  -allowProvisioningUpdates -allowProvisioningDeviceRegistration -derivedDataPath build build | grep -E "error:|BUILD" || true
test -d build/Build/Products/Debug-maccatalyst/LifeHome.app
mkdir -p ~/Applications
rm -rf ~/Applications/"Life Home.app"
cp -R build/Build/Products/Debug-maccatalyst/LifeHome.app ~/Applications/"Life Home.app"
echo "installed ~/Applications/Life Home.app"
