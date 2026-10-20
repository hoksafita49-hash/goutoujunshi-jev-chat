#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
check_dir="$(mktemp -d /tmp/goutou-ios-check.XXXXXX)"
trap 'rm -rf "$check_dir"' EXIT
swiftc -module-cache-path "$check_dir/modules" -parse-as-library \
  Shared/*.swift Tests/CoreChecks.swift -o "$check_dir/core-checks"
"$check_dir/core-checks"
swiftc -module-cache-path "$check_dir/modules" -parse-as-library \
  Keyboard/Input/*.swift Tests/TypingChecks.swift -o "$check_dir/typing-checks"
"$check_dir/typing-checks" Keyboard/Resources/pinyin.tsv
swiftc -module-cache-path "$check_dir/modules" -parse-as-library \
  Shared/*.swift ImageRecognition/LocalOCR.swift Tests/ImageChecks.swift -o "$check_dir/image-checks"
"$check_dir/image-checks"
swiftc -module-cache-path "$check_dir/modules" -parse-as-library \
  Shared/*.swift App/Sync/*.swift Tests/CloudChecks.swift -lsqlite3 -o "$check_dir/cloud-checks"
"$check_dir/cloud-checks"
swiftc -frontend -parse App/Sources/*.swift Keyboard/Sources/*.swift Keyboard/Input/*.swift ImageRecognition/*.swift Capture/Sources/*.swift
plutil -lint GoutouJunshi.xcodeproj/project.pbxproj App/Info.plist Keyboard/Info.plist Capture/Info.plist \
  App/GoutouJunshi.entitlements Keyboard/GoutouKeyboard.entitlements Capture/GoutouCapture.entitlements PrivacyInfo.xcprivacy
python3 tools/check_project.py
