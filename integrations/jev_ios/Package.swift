// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "GoutouCore",
    platforms: [.macOS(.v13), .iOS(.v16)],
    products: [.library(name: "GoutouCore", targets: ["GoutouCore"])],
    targets: [
        .target(name: "GoutouCore", path: ".", exclude: ["App/Sources", "App/Assets.xcassets", "App/Resources", "App/Info.plist", "App/GoutouJunshi.entitlements", "Shared/BrandAssets.xcassets", "Keyboard", "Capture", "ImageRecognition", "Tests", "tools", "vendor", "docs", "Design", "GoutouJunshi.xcodeproj", "README.md", "README_EN.md", "PrivacyInfo.xcprivacy", "Signing.xcconfig"], sources: ["Shared", "App/Sync"], linkerSettings: [.linkedLibrary("sqlite3")])
    ]
)
