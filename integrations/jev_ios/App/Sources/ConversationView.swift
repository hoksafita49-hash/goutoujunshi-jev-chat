import SwiftUI
import UIKit
import PhotosUI
import UniformTypeIdentifiers

struct ConversationView: View {
    @EnvironmentObject var model: AppModel
    @State private var photo: PhotosPickerItem?
    @State private var importer = false
    @State private var details = false
    @State private var cloudConsent = false
    @State private var pendingImage: Data?
    @State private var pendingProvider = OCRProvider.local
    @State private var screenshotPreview: UIImage?
    @State private var loadingImage = false
    @State private var shortcutGuide = false

    var body: some View {
        ScrollViewReader { proxy in
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if model.cloudEnabled {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("云端已确认聊天").font(.headline)
                        Text(model.cloudStatus).font(.caption)
                        ForEach(model.cloudContexts.filter { $0.profileUid == model.relationship.id }) { context in
                            Button { model.openCloudContext(context) } label: {
                                Text(String(context.transcript.prefix(80)) + (context.result == nil ? " · 已同步" : " · 含分析结果"))
                                    .font(.caption).lineLimit(3)
                            }
                        }
                        Button("立即同步") { model.syncCloud() }
                    }.card()
                }
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 12) {
                        ShibaBadge()
                        VStack(alignment: .leading, spacing: 4) {
                            Text("你的随身狗头军师").font(.title3.bold())
                            Text("把话说得像自己").font(.callout).foregroundStyle(Palette.secondary)
                        }
                    }.padding(.bottom, 4)
                    Text("\(model.relationship.label) · \(model.relationship.stage) · \(model.relationship.goal)").foregroundStyle(Palette.secondary)
                    Text(model.keyboardStatus).font(.caption).foregroundStyle(Palette.secondary)
                    DisclosureGroup("在聊天 App 里怎么用") {
                        Text("① 设置 → 通用 → 键盘 → 键盘 → 添加“狗头军师”。开启允许完全访问后可读取剪贴板和联网分析。\n② 点聊天输入框，用地球键切换到狗头军师，直接输入中文拼音、英文、数字或符号。\n③ 聊天截屏后，在这里选择截图；也可在照片中拷贝截图，点键盘顶部的图片按钮读取并识别。文字消息可复制后用军师的“读取剪贴板”。\n④ 核对原文、双方身份与对象，再确认分析；主 App 的结果可交给键盘。选候选填入草稿，由你自己发送；点“打字”继续修改。")
                            .font(.callout).padding(.top, 8)
                    }
                }.card()

                VStack(alignment: .leading, spacing: 12) {
                    Label("从截图读取聊天", systemImage: "photo.on.rectangle").font(.headline)
                    Text("截取聊天画面，选择截图后提取文字。当前：\(model.config.ocr.rawValue)")
                        .font(.caption).foregroundStyle(Palette.secondary)
                    HStack {
                        PhotosPicker(selection: $photo, matching: .images) {
                            Label("选择聊天截图", systemImage: "photo")
                        }.buttonStyle(BrandPrimaryButtonStyle()).accessibilityIdentifier("selectChatScreenshot")
                        Button("粘贴截图") {
                            do { stageImage(try ClipboardScreenshot.read()) }
                            catch { model.status = error.localizedDescription }
                        }.buttonStyle(.bordered).accessibilityIdentifier("pasteChatScreenshot")
                    }.disabled(model.working || loadingImage)
                    Button { importer = true } label: { Label("从文件选择截图", systemImage: "folder") }
                        .font(.callout).disabled(model.working || loadingImage)
                    Button { shortcutGuide = true } label: { Label("设置一键截屏识别", systemImage: "viewfinder") }
                        .font(.callout).accessibilityIdentifier("screenshotShortcutGuide")
                    if loadingImage { ProgressView("正在读取所选截图…") }
                    if let image = screenshotPreview {
                        Image(uiImage: image).resizable().scaledToFit().frame(maxHeight: 180)
                            .frame(maxWidth: .infinity).accessibilityLabel("本轮聊天截图")
                        Button("收起截图预览") { screenshotPreview = nil }.font(.caption)
                    }
                    Text("本地 OCR 不上传图片；云端识图会逐张确认。识别结果显示在下方，先核对错字和说话人。")
                        .font(.caption).foregroundStyle(Palette.secondary)
                }.card()

                VStack(alignment: .leading, spacing: 10) {
                    Text("核对本轮对话").font(.headline)
                    Text("支持粘贴多轮原文，或选择聊天截图识别。键盘无法自动读取聊天历史。")
                        .font(.caption).foregroundStyle(Palette.secondary)
                    HStack {
                        PasteButton(payloadType: String.self) { values in
                            model.transcript = values.joined(separator: "\n"); model.source = "手动粘贴"
                        }.labelStyle(.iconOnly).accessibilityLabel("粘贴对话")
                        Spacer()
                        Button("离线示例") { model.loadDemo() }
                    }.disabled(model.working || loadingImage)
                    Text("来源：\(model.source) · 识别方式：\(model.config.ocr.rawValue)").font(.caption).foregroundStyle(Palette.secondary)
                    TextEditor(text: $model.transcript).frame(minHeight: 200).padding(8)
                        .scrollContentBackground(.hidden).background(Palette.cream).clipShape(RoundedRectangle(cornerRadius: 14))
                        .accessibilityIdentifier("transcriptEditor")
                    Toggle("已核对原文、双方身份与当前对象", isOn: $model.confirmed)
                        .font(.callout).disabled(model.working)
                    if model.cloudEnabled {
                        Button("仅同步已确认聊天") { model.syncConfirmedChat() }.disabled(!model.confirmed || model.working)
                    }
                    if model.working {
                        HStack { ProgressView(); Button("取消") { model.cancel() } }
                    } else {
                        Button("确认并分析") { model.analyze() }.buttonStyle(BrandPrimaryButtonStyle())
                            .disabled(!model.confirmed || model.transcript.trimmed.isEmpty || model.store == nil)
                            .accessibilityIdentifier("analyzeButton")
                    }
                }.card().id("transcriptReview")

                if let advice = model.advice {
                    AdviceSummary(advice: advice)
                    HStack {
                        Button("更像我一点") { model.analyze(rewrite: true) }.disabled(model.working || model.demo || advice.candidates.isEmpty)
                        Spacer()
                        Button("详细分析") { details = true }.accessibilityIdentifier("detailsButton")
                    }.buttonStyle(.bordered)
                    if model.cloudEnabled {
                        Button("请求云端详细解释") { model.analyzeCloud(kind: "explanation") }.disabled(model.working || model.demo)
                        if !model.cloudExplanation.isEmpty { Text(model.cloudExplanation).textSelection(.enabled).card() }
                    }
                    Button("把本轮结果交给键盘") { model.sendToKeyboard() }
                        .buttonStyle(BrandPrimaryButtonStyle()).disabled(!model.confirmed || model.demo || model.working || model.store == nil)
                    Text("主动交接在本机暂存 5 分钟，交接文件取用后删除。键盘最近一轮另暂存 5 分钟，收起后可恢复；可在键盘或配置页立即清除。")
                        .font(.caption).foregroundStyle(Palette.secondary)
                }
                Text(model.status).font(.footnote).foregroundStyle(Palette.secondary).textSelection(.enabled)
            }.padding()
        }
        .background(Palette.cream)
        .sheet(isPresented: $shortcutGuide) { ScreenshotShortcutGuide() }
        .sheet(isPresented: $details) {
            if let advice = model.advice { NavigationStack { DetailView(advice: advice, transcript: model.transcript) } }
        }
        .onChange(of: model.source) { source in
            if source == "离线示例" || source == "手动粘贴" { screenshotPreview = nil }
        }
        .onChange(of: model.shortcutScreenshot) { data in
            if data != nil { receiveShortcutScreenshot(proxy) }
        }
        .onAppear { receiveShortcutScreenshot(proxy) }
        .onChange(of: photo) { item in
            guard let item = item else { return }
            loadingImage = true
            Task {
                do {
                    guard let data = try await item.loadTransferable(type: Data.self), data.count <= 20_000_000 else {
                        throw GoutouError.invalid("请选择 20 MB 以内的截图")
                    }
                    stageImage(data)
                } catch { model.status = "截图读取失败，请换一张图片" }
                loadingImage = false; photo = nil
            }
        }
        .fileImporter(isPresented: $importer, allowedContentTypes: [.image]) { result in
            do {
                let url = try result.get()
                let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                guard let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 20_000_000 else { throw GoutouError.invalid("截图超过 20 MB") }
                stageImage(try Data(contentsOf: url))
            } catch { model.status = "无法读取截图，请检查文件格式或大小" }
        }
        .confirmationDialog("将所选聊天截图发送至 \(pendingProvider.rawValue)？", isPresented: $cloudConsent, titleVisibility: .visible) {
            Button("发送截图并识别") { if let data = pendingImage { model.recognize(data, provider: pendingProvider) }; pendingImage = nil }
            Button("取消", role: .cancel) { pendingImage = nil; screenshotPreview = nil }
        } message: { Text("只发送这张截图，会产生接口用量。识别后仍需核对原文和双方身份。") }
        }
    }
    private func receiveShortcutScreenshot(_ proxy: ScrollViewProxy) {
        guard let data = model.shortcutScreenshot else { return }
        screenshotPreview = (try? LocalOCR.image(data, maxPixelSize: 600)).map { UIImage(cgImage: $0) }
        model.shortcutScreenshot = nil
        if model.config.ocr != .local {
            pendingProvider = model.config.ocr
            pendingImage = data; cloudConsent = true
        }
        DispatchQueue.main.async { withAnimation { proxy.scrollTo("transcriptReview", anchor: .top) } }
    }
    private func stageImage(_ data: Data) {
        do {
            let jpeg = try LocalOCR.boundedJPEG(data)
            screenshotPreview = UIImage(cgImage: try LocalOCR.image(jpeg, maxPixelSize: 600))
            pendingProvider = model.config.ocr
            if pendingProvider == .local { model.recognize(jpeg, provider: .local) }
            else { pendingImage = jpeg; cloudConsent = true }
        } catch { model.status = error.localizedDescription }
    }
}

struct AdviceSummary: View {
    let advice: Advice
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("对方可能的意图").font(.headline).foregroundStyle(Palette.ink)
            Text(advice.intent).font(.title3.bold())
            Text(advice.confidenceLabel).font(.caption).foregroundStyle(Palette.secondary)
            Text("军师建议 · \(advice.strategy.rawValue)").font(.headline)
            Text(advice.recommendation)
            Text("候选回复排序").font(.headline)
            if advice.candidates.isEmpty { Text("本轮建议不回复。") }
            ForEach(Array(advice.candidates.enumerated()), id: \.offset) { index, candidate in
                VStack(alignment: .leading, spacing: 8) {
                    Text("#\(index + 1)" + (candidate.weight.map { " · \($0)%" } ?? " · 权重暂不可用"))
                        .font(.subheadline.bold()).foregroundStyle(Palette.ink)
                    Text(candidate.text).font(.title3).textSelection(.enabled)
                    Button("复制候选") { UIPasteboard.general.string = candidate.text }.buttonStyle(.bordered)
                    DisclosureGroup("为什么这样回") {
                        Text("理由：\(candidate.reason)\n代价：\(candidate.tradeoff)").font(.callout).padding(.top, 6)
                    }
                }.padding().frame(maxWidth: .infinity, alignment: .leading)
                    .background(index == 0 ? Palette.highlight : Palette.cream)
                    .clipShape(RoundedRectangle(cornerRadius: 18))
            }
            Text(advice.rankingStatus == "demo" ? "离线示例，权重为演示数字。" :
                advice.rankingStatus == "unavailable" ? "排序暂不可用，保留生成顺序，不补造权重。" : Advice.weightNote)
                .font(.caption).foregroundStyle(Palette.secondary)
        }.card()
    }
}

struct DetailView: View {
    let advice: Advice
    let transcript: String
    @Environment(\.dismiss) private var dismiss
    @State private var original = false
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Toggle("查看核对原文", isOn: $original)
                if original { Text(transcript).textSelection(.enabled).card() }
                else {
                    block("对方可能的意图", advice.intent + "\n" + advice.confidenceLabel + "\n" + Advice.intentNote)
                    block("军师建议 · \(advice.strategy.rawValue)", advice.recommendation)
                    block("先照顾好自己的感受", advice.support)
                    rows("已知事实", advice.facts); rows("合理推测", advice.hypotheses); rows("仍未知", advice.unknowns)
                    block("下一步与观察窗口", advice.nextStep)
                    block("停止条件", advice.stopCondition)
                    if !advice.question.isEmpty { block("需要你补充", advice.question) }
                    if let decision = advice.decision {
                        block("策略判断来源", decision.summary)
                        if !decision.probabilities.isEmpty {
                            rows("七策略相对权重", Strategy.allCases.map {
                                "\($0.rawValue)：\(Decision.percent(decision.probabilities[$0.rawValue]))"
                            })
                        }
                        rows("判断依据", decision.evidence.facts)
                    }
                    Text(Advice.weightNote).font(.footnote).foregroundStyle(Palette.secondary)
                }
            }.padding()
        }.background(Palette.cream).navigationTitle("详细分析")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
    }
    private func block(_ title: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 10) { Text(title).font(.headline); Text(text).textSelection(.enabled) }.card()
    }
    private func rows(_ title: String, _ values: [String]) -> some View {
        block(title, values.isEmpty ? "暂缺信息" : values.map { "• " + $0 }.joined(separator: "\n"))
    }
}

extension View {
    func card() -> some View {
        padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.surface).clipShape(RoundedRectangle(cornerRadius: 22))
            .overlay(RoundedRectangle(cornerRadius: 22).stroke(Palette.separator.opacity(0.45), lineWidth: 0.5))
    }
}
