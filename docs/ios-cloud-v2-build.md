# 云端接入第二阶段构建记录

2026-10-21（北京时间）已在用户授权的 Fork 完成真实 macOS 检查及两个 Xcode 构建。

- Fork：`hoksafita49-hash/goutoujunshi-jev-chat`
- 分支：`codex/pocketbase-cloud-v1`
- 构建提交：`c500dca95b912745768ef4baea15a422bba5e079`
- [GitHub Actions 运行 39485965448](https://github.com/hoksafita49-hash/goutoujunshi-jev-chat/actions/runs/39485965448)：`success`，job 耗时 6 分 25 秒。
- 标准 `macos-15-intel` runner，实际镜像 `macos-15`（`20260824.0482.1`），Xcode 16.4、iOS 18.5 SDK。

此次包含偏好兼容、证据快照、记忆待确认/纠错/历史及待同步确认保护。构建过程中修复了历史恢复页面 `catch` 的隐式 `error` 遮蔽页面错误状态的问题，修复后重新运行全部检查与两个 Xcode 构建。未使用大型付费 runner，未增加凭据权限。

## 实际验证结果

`bash tools/check.sh` 成功执行：

- 165 项 iOS 核心检查。
- 33 项离线打字检查，正式词库 65,125 项。
- 27 项截图/OCR 检查，使用生成的聊天图像。
- 83 项云契约、SQLite、迁移与账号隔离检查，包含偏好自由文本兼容、样式保留/清除、证据与历史解析、记忆纠错与待同步确认保护。
- Swift 源码解析、plist 校验、工程 target 文件归属、扩展嵌入、SQLite 链接、共享组与隐私声明检查。

以上 308 项运行检查均来自本次成功运行，未请求真实模型 API，未使用私人聊天或生产凭据。

两个 `xcodebuild` 步骤均输出 `BUILD SUCCEEDED`，退出成功：

1. `generic/platform=iOS Simulator`：主 App、键盘扩展、Capture 扩展。
2. `generic/platform=iOS`：相同设备 target 的未签名编译，`CODE_SIGNING_ALLOWED=NO`。

日志仍有 AppIntents 元数据提示，以及 `checkout@v4` / `upload-artifact@v4` 的 Node.js 20 迁移提示；构建和上传均成功。这些日志不构成已验证 Siri/AppIntents 功能的证据。

## 产物与限制

工作流上传 `goutoujunshi-ios-simulator-preview.zip`，大小 6,359,077 字节。用户交付副本为 `goutoujunshi-cloud-v2-ios-SIMULATOR-ONLY.zip`。

SHA-256：`e631295d95c96e3b051b5a9418dcfdebd7379c9798a7e23611f616c47d284059`。

下载后已验证 ZIP CRC、三个 bundle 的 `iPhoneSimulator` 平台、主 App 与两个扩展的 Mach-O 可执行文件及实际 debug 动态库（均含 x86_64/arm64）和可执行文件 `0755` 权限。

此 ZIP 仅供 macOS 上的 iOS 模拟器开发，**不能安装到 iPhone**。设备 target 的未签名编译不包含 Apple 开发者签名、描述文件、TestFlight 或真机验收。iPhone 上的登录、同步、App Group/Keychain、键盘 Full Access 与三端数据往返仍需后续签名后验证。未发布安装包。
