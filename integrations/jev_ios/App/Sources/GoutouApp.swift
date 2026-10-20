import SwiftUI
import UIKit

@main
struct GoutouApp: App {
    @StateObject private var model = AppModel()
    @ObservedObject private var screenshotInbox = ScreenshotInbox.shared
    @State private var selectedTab = 0
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            TabView(selection: $selectedTab) {
                NavigationStack { ConversationView().navigationTitle("狗头军师") }
                    .tabItem { Label("使用", systemImage: "bubble.left.and.bubble.right") }.tag(0)
                NavigationStack { RelationshipView().navigationTitle("关系与目标") }
                    .tabItem { Label("关系", systemImage: "person.crop.circle") }.tag(1)
                NavigationStack { ProviderView().navigationTitle("接口与偏好") }
                    .tabItem { Label("配置", systemImage: "slider.horizontal.3") }.tag(2)
                NavigationStack { TrendView().navigationTitle("关系趋势 K 线") }
                    .tabItem { Label("K 线", systemImage: "chart.xyaxis.line") }.tag(3)
            }
            .tint(Palette.accent)
            .foregroundStyle(Palette.ink)
            .toolbarBackground(Palette.cream, for: .tabBar)
            .environmentObject(model)
            .onAppear { receiveScreenshot(); model.foreground() }
            .onChange(of: screenshotInbox.pending) { _ in receiveScreenshot() }
            .onChange(of: scenePhase) { phase in
                if phase == .active { model.foreground() }
                else if phase == .background { model.background() }
            }
        }
    }
    private func receiveScreenshot() {
        guard let image = screenshotInbox.take() else { return }
        selectedTab = 0
        model.importShortcutScreenshot(image)
    }
}

enum Palette {
    static let accent = Color(uiColor: BrandTheme.accent)
    static let primary = Color(uiColor: BrandTheme.primary)
    static let onPrimary = Color(uiColor: BrandTheme.onPrimary)
    static let ink = Color(uiColor: BrandTheme.text)
    static let secondary = Color(uiColor: BrandTheme.secondaryText)
    static let cream = Color(uiColor: BrandTheme.background)
    static let surface = Color(uiColor: BrandTheme.surface)
    static let highlight = Color(uiColor: BrandTheme.highlight)
    static let separator = Color(uiColor: BrandTheme.separator)
    static let chartRise = Color(uiColor: BrandTheme.chartRise)
    static let chartFall = Color(uiColor: BrandTheme.chartFall)
    static let chartEvent = Color(uiColor: BrandTheme.chartEvent)
}

@MainActor
final class AppModel: ObservableObject {
    @Published var config = AppConfig()
    @Published var relationship = Relationship(id: "current")
    @Published var transcript = "" {
        didSet { if oldValue != transcript { confirmed = false; cloudContext = nil; invalidate(clear: true) } }
    }
    @Published var confirmed = false
    @Published var source = "手动输入"
    @Published var advice: Advice?
    @Published var working = false
    @Published var status = "先核对原文，再让军师分析。"
    @Published var keyboardStatus = ""
    @Published var demo = false
    @Published var shortcutScreenshot: Data?
    @Published var cloudEnabled = false
    @Published var cloudStatus = "需登录"
    @Published var cloudEmail = ""
    @Published var cloudBusy = false
    @Published var cloudProfiles = [Relationship]()
    @Published var cloudContexts = [CloudContext]()
    @Published var cloudConflicts = [CloudConflict]()
    @Published var cloudPendingJobs = [CloudPendingJob]()
    @Published var cloudRecovery = false
    @Published var cloudContext: CloudContext?
    @Published var cloudExplanation = ""
    @Published var cloudMemories = [CloudRecord]()
    @Published var cloudPendingMemoryUids = Set<String>()
    @Published var cloudReviewIdentity = ""
    @Published var cloudEvidence: CloudEvidenceDocument?
    @Published var cloudHistoryMemory: CloudRecord?
    @Published var cloudHistoryVersions = [CloudRecord]()
    @Published var cloudHistoryBefore = 0
    @Published var cloudHistoryMore = false
    @Published var cloudReviewBusy = false
    @Published var cloudReviewStatus = ""
    let transport = HTTPTransport()
    var store: GoutouStore?
    var cloud: SyncCoordinator?
    var cloudLoop: Task<Void, Never>?
    var cloudManualTask: Task<Void, Never>?
    var task: Task<Void, Never>?
    var revision = 0
    var cloudReviewGeneration = UUID()
    var cloudReviewTask: Task<Void, Never>?

    init() {
        do {
            store = try GoutouStore()
            cloud = SyncCoordinator(directory: store!.cloudDirectory)
            try cloud?.restore(); reload()
        }
        catch { status = error.localizedDescription }
    }
    func reload() {
        guard let store = store else { return }
        cloudEnabled = store.cloudEnabled
        if cloudEnabled {
            config = AppConfig(); config.ocr = .local
            keyboardStatus = store.keyboardStatus
            refreshCloudView()
            return
        }
        let updated = store.config(), context = store.relationship()
        store.purgeExpiredHandoff()
        if updated != config || context != relationship {
            confirmed = false; invalidate(clear: true, clearKeyboardSession: false)
        }
        config = updated; relationship = context
        keyboardStatus = store.keyboardStatus
    }
    func invalidate(clear: Bool, clearKeyboardSession: Bool = true) {
        revision += 1; task?.cancel(); task = nil; working = false
        if clear {
            advice = nil; demo = false; store?.clearHandoff()
            if clearKeyboardSession { store?.clearKeyboardSession() }
        }
    }
    func cancel() { invalidate(clear: false); status = "已取消；已发出的服务请求无法撤回。" }
    func api() throws -> ModelAPI { ModelAPI(transport: transport, config: config, secrets: try CredentialVault.load()) }

    func analyze(rewrite: Bool = false) {
        if cloudEnabled { analyzeCloud(kind: rewrite ? "rewrite" : "analysis"); return }
        guard !working else { return }
        let original = advice
        do {
            let text = Transcript(text: transcript, confirmed: confirmed)
            try text.validate()
            let api = try api(); try api.preflight(); try relationship.validate()
            invalidate(clear: !rewrite)
            let generation = revision, context = relationship
            working = true; status = rewrite ? "调整候选口吻…" : "开始分析…"
            task = Task { [weak self] in
                do {
                    let pipeline = GoutouPipeline(api: api)
                    let result: Advice
                    if rewrite, let original = original {
                        result = try await pipeline.rewrite(original, transcript: text, relationship: context)
                    } else {
                        result = try await pipeline.analyze(text, relationship: context) { [weak self] stage in
                            Task { @MainActor in
                                guard let self = self, self.revision == generation, self.working else { return }
                                self.status = stage
                            }
                        }
                    }
                    try Task.checkCancellation()
                    guard let self = self, self.revision == generation else { return }
                    self.advice = result; self.working = false; self.demo = false
                    self.status = "分析完成 · 填入后请核对接收人，发送由你决定。"
                } catch {
                    guard let self = self, self.revision == generation else { return }
                    self.working = false
                    self.status = error is CancellationError ? "已取消" : error.localizedDescription
                }
            }
        } catch { status = error.localizedDescription }
    }
    func loadDemo() {
        invalidate(clear: true)
        transcript = DemoConversation.text; confirmed = true; source = "离线示例"
        advice = DemoConversation.advice; demo = true
        status = "离线示例 · 未读取聊天、未调用接口；数字仅用于展示。"
    }
    func sendToKeyboard() {
        if cloudEnabled {
            do { try cloud?.publish(); status = "已更新键盘只读缓存；需完全访问，切换键盘后核对当前对象。" }
            catch { status = error.localizedDescription }
            return
        }
        guard let store = store, let advice = advice, confirmed, !demo, !working else { return }
        do {
            try store.handoff(KeyboardHandoff(transcript: transcript, advice: advice, relationship: relationship,
                                             config: config, createdAt: Date()))
            status = "已交给键盘 · 5 分钟内切回目标聊天，选择狗头军师键盘。取用后会删除临时内容。"
        } catch { status = error.localizedDescription }
    }
    func recognize(_ data: Data, provider: OCRProvider) {
        if cloudEnabled { performRecognition(data, provider: .local, sourceLabel: "Apple Vision · 本地"); return }
        guard provider == config.ocr else { status = "识图配置已变化，请重新选择截图并确认。"; return }
        performRecognition(data, provider: provider, sourceLabel: provider.rawValue)
    }
    func importShortcutScreenshot(_ data: Data) {
        shortcutScreenshot = data
        if config.ocr == .local {
            performRecognition(data, provider: .local, sourceLabel: "快捷指令 · Apple Vision 本地")
        } else {
            invalidate(clear: true); confirmed = false
            source = "快捷指令截图 · 待 \(config.ocr.rawValue) 确认"
            status = "已收到截图；确认后使用 \(config.ocr.rawValue)，不会自动改为本地识图。"
        }
    }
    private func performRecognition(_ data: Data, provider: OCRProvider, sourceLabel: String) {
        let snapshot = config
        invalidate(clear: true)
        confirmed = false
        let generation = revision
        working = true; status = provider == .local ? "Apple Vision 本地识别中…" : "正在发送所选截图到识图接口…"
        task = Task { [weak self] in
            do {
                let text: String
                if provider == .local { text = try await LocalOCR.recognize(data) }
                else {
                    guard let self = self else { return }
                    let jpeg = try LocalOCR.boundedJPEG(data)
                    let api = ModelAPI(transport: self.transport, config: snapshot, secrets: try CredentialVault.load())
                    text = try await api.recognizeImage(base64: jpeg.base64EncodedString(),
                        provider: provider == .deepseek ? .deepseek : .openrouter)
                }
                try Task.checkCancellation()
                guard let self = self, self.revision == generation else { return }
                self.transcript = text; self.confirmed = false; self.source = sourceLabel
                self.working = false; self.task = nil
                self.status = "已识别 · 请修正错字，并把每条消息标为“我：”或“对方：”，再确认分析。"
            } catch {
                guard let self = self, self.revision == generation else { return }
                self.working = false; self.status = error is CancellationError ? "已取消" : error.localizedDescription
            }
        }
    }
}
