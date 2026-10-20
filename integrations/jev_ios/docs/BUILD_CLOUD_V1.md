# 云端接入第一阶段构建记录

2026-10-21（北京时间）已在用户授权的 Fork 完成真实 macOS 检查及 Xcode 构建。

- 仓库：`hoksafita49-hash/goutoujunshi-jev-chat`
- 测试分支：`codex/pocketbase-cloud-v1`
- 构建提交：`85c6958d41022f21f73d97cb760f6969ebef4b60`
- [GitHub Actions 运行 39478226288](https://github.com/hoksafita49-hash/goutoujunshi-jev-chat/actions/runs/39478226288)：`success`
- 环境：标准 `macos-15` runner，实际镜像 `macos-15-arm64`（`20260907.0337.1`），Xcode 16.4、iOS 18.5 SDK。

首次 `macos-latest` 运行尚未分配 runner，未开始编译；切换到上述标准 runner 后取消旧运行。此提交相对 `f1444297065e74dfdc4343aabe9ccd36d24c15ab` 仅更改工作流的 runner，未改业务代码。

## 实际验证结果

`bash tools/check.sh` 成功执行：

- 165 项 iOS 核心检查。
- 33 项离线打字检查，正式词库 65,125 项。
- 27 项截图/OCR 检查，使用生成的聊天图像。
- 49 项云契约、SQLite、旧资料迁移与账号隔离检查，包含迟到 401、账号切换、历史分页、墓碑及未确认任务请求 ID 保留。
- Swift 源码解析、plist 校验、工程 target 文件归属、扩展嵌入、SQLite 链接、共享组与隐私声明检查。

以上 274 项运行检查未请求真实模型 API，未使用私人聊天或生产凭据。

两个 `xcodebuild` 步骤均输出 `BUILD SUCCEEDED`，退出成功：

1. `generic/platform=iOS Simulator`：主 App、键盘扩展、Capture 扩展。
2. `generic/platform=iOS`：相同设备 target 的未签名编译，`CODE_SIGNING_ALLOWED=NO`。

日志中有 Xcode 的 AppIntents 元数据提示，包括 `No AppIntents.framework dependency found` 和 `Unable to parse extract.actionsdata`；两次构建最终均成功。这些日志不构成已验证 Siri/AppIntents 功能的证据。

## 产物与限制

工作流上传 `goutoujunshi-ios-simulator-preview.zip`，大小 5,841,700 字节。

SHA-256：`36a3d46fa26d041d5215870fe8ab5dfe820a4b178779fff2082c29c45612c4b3`。

下载后已验证 ZIP CRC、`Info.plist` 的 `iPhoneSimulator` 平台、主 App 与两个扩展的 Mach-O 可执行文件（x86_64/arm64）及执行权限。

此 ZIP 仅供 macOS 上的 iOS 模拟器开发，**不能安装到 iPhone**。设备 target 编译成功不包含 Apple 开发者签名、描述文件、TestFlight 或真机验收；登录、App Group/Keychain 权限、键盘 Full Access 与实际三端数据往返仍需签名后的 iPhone 验证。当前未发布安装包。
