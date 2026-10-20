import Foundation

@MainActor
extension AppModel {
    func setCloudEnabled(_ enabled: Bool) {
        closeCloudReview()
        invalidate(clear: true); transcript = ""; cloudContext = nil; confirmed = false
        store?.cloudEnabled = enabled; cloudEnabled = enabled; reload()
        if enabled { var local = config; local.ocr = .local; try? store?.saveConfig(local); foreground() }
        else { cloudLoop?.cancel(); cloudLoop = nil }
    }
    func foreground() {
        reload(); cloudLoop?.cancel()
        guard cloudEnabled else { return }
        cloudLoop = Task { [weak self] in
            while !Task.isCancelled {
                guard let self = self else { return }
                await self.performSync(manual: false)
                await self.queryKnownJobs()
                do { try await Task.sleep(nanoseconds: 15_000_000_000) } catch { return }
            }
        }
    }
    func background() { cloudLoop?.cancel(); cloudLoop = nil; invalidate(clear: false) }
    func refreshCloudView() {
        guard cloudEnabled else { return }
        do {
            if let current = cloud?.session, try SessionVault.read()?.deviceId != current.deviceId { try cloud?.requireLogin() }
            guard let cloud = cloud, let session = cloud.session, let db = cloud.database else {
                closeCloudReview(); cloudReviewIdentity = ""; cloudMemories = []; cloudPendingMemoryUids = []
                cloudEmail = ""; cloudProfiles = []; cloudContexts = []; cloudConflicts = []; cloudPendingJobs = []
                relationship = Relationship(id: "cloud-empty", label: "请先登录并选择人物")
                cloudContext = nil; advice = nil; transcript = ""; cloudStatus = cloud?.status ?? "需登录"
                return
            }
            let identity = session.accountKey + ":" + session.deviceId
            if cloudReviewIdentity != identity {
                closeCloudReview(); cloudReviewIdentity = identity; config = AppConfig(); config.ocr = .local
                if store?.config() != config { try store?.saveConfig(config) }
            }
            cloudEmail = session.email; cloudStatus = cloud.status
            cloudRecovery = try db.recoveryRequired()
            cloudProfiles = try db.visibleRecords("profiles").map(\.relationship)
            cloudContexts = try db.contexts(); cloudConflicts = try db.conflicts(); cloudPendingJobs = try db.pendingJobs()
            if let preferences = try db.visibleRecords("preferences").first?.payload {
                if let updated = try? config.applyingCloudPreferences(preferences) {
                    config = updated
                    if store?.config() != updated { try store?.saveConfig(updated) }
                }
            }
            let selected = try cloud.selectedProfile()
            if let profile = cloudProfiles.first(where: { $0.id == selected }) ?? cloudProfiles.first {
                if profile.id != relationship.id { closeCloudReview(); invalidate(clear: true); transcript = ""; cloudContext = nil }
                relationship = profile
            } else { closeCloudReview(); relationship = Relationship(id: "cloud-empty", label: "先新建或导入人物") }
            cloudMemories = try db.memories(profileUid: relationship.id)
            cloudPendingMemoryUids = Set(try db.operations().filter { $0.entityType == "memories" }.map(\.entityUid))
            if let history = cloudHistoryMemory, !cloudMemories.contains(where: { $0.uid == history.uid }) { closeCloudReview() }
            if let evidence = cloudEvidence, try db.remoteRecords("analyses").contains(where: { $0.uid == evidence.analysisId && $0.deletedAt != nil }) { closeCloudReview() }
            if let current = cloudContext, let updated = cloudContexts.first(where: { $0.id == current.id && $0.contextRevision == current.contextRevision }) {
                cloudContext = updated
                if !working, let result = updated.result { advice = result.advice() }
            }
        } catch { cloudStatus = error.localizedDescription }
    }
    func loginCloud(base: String, email: String, password: String) {
        guard !cloudBusy else { return }
        cloudManualTask?.cancel(); cloudManualTask = nil
        cloudBusy = true; cloudStatus = "登录中"
        Task {
            defer { cloudBusy = false }
            do {
                guard let cloud = cloud else { throw CloudError.signIn }
                let origin = try GatewayClient.baseURL(base).absoluteString
                try await cloud.login(base: origin, email: email, password: password, deviceName: "iPhone")
                store?.cloudServer = origin
                invalidate(clear: true); transcript = ""; cloudContext = nil
                refreshCloudView(); await performSync(manual: true)
            } catch { cloudStatus = error.localizedDescription }
        }
    }
    func logoutCloud() {
        guard !cloudBusy else { return }
        cloudManualTask?.cancel(); cloudManualTask = nil
        cloudBusy = true; closeCloudReview(); cloudReviewIdentity = ""; cloudMemories = []; cloudPendingMemoryUids = []
        background(); invalidate(clear: true); transcript = ""; cloudContext = nil
        store?.clearHandoff(); store?.clearKeyboardSession(); store?.captureMailbox.clear()
        Task {
            defer { cloudBusy = false; refreshCloudView() }
            do { try await cloud?.logout() } catch { status = error.localizedDescription }
        }
    }
    func syncCloud() {
        cloudManualTask?.cancel()
        cloudManualTask = Task { await performSync(manual: true); if !Task.isCancelled { await queryKnownJobs() } }
    }
    func syncConfirmedChat() {
        do {
            try Transcript(text: transcript, confirmed: confirmed).validate()
            guard let db = cloud?.database, cloud?.session != nil else { throw CloudError.signIn }
            let owner = cloud?.session?.accountKey, device = cloud?.session?.deviceId
            let uid = try db.confirmConversation(profileUid: relationship.id, transcript: transcript, source: source)
            status = "已确认聊天进入本机队列"
            cloudManualTask?.cancel()
            cloudManualTask = Task {
                await performSync(manual: true)
                guard cloud?.session?.accountKey == owner, cloud?.session?.deviceId == device, !Task.isCancelled else { return }
                if let context = try? db.contexts(profileUid: relationship.id).first(where: { $0.id == uid }) { cloudContext = context }
            }
        } catch { status = error.localizedDescription }
    }
    func performSync(manual: Bool) async {
        guard cloudEnabled, cloud?.session != nil else { return }
        let owner = cloud?.session?.accountKey, device = cloud?.session?.deviceId
        do {
            try await cloud?.sync(manual: manual)
            guard cloud?.session?.accountKey == owner, cloud?.session?.deviceId == device, !Task.isCancelled else { return }
            refreshCloudView()
        } catch {
            // The coordinator deliberately clears the current session on 401;
            // only that account's live failure may refresh the sign-in UI.
            guard !Task.isCancelled, cloud?.session == nil || (cloud?.session?.accountKey == owner && cloud?.session?.deviceId == device) else { return }
            if error is CancellationError { return }
            refreshCloudView(); cloudStatus = error.localizedDescription
        }
    }
    func selectCloudProfile(_ profile: Relationship) {
        do {
            closeCloudReview()
            invalidate(clear: true); transcript = ""; cloudContext = nil
            try cloud?.select(profile.id); refreshCloudView()
        } catch { status = error.localizedDescription }
    }
    func saveCloudProfile(_ profile: Relationship) throws {
        try profile.validate()
        guard let cloud = cloud, let db = cloud.database, cloud.session != nil else { throw CloudError.signIn }
        try db.stage(entityType: "profiles", uid: profile.id.lowercased(), payload: profile.cloudPayload)
        try cloud.select(profile.id.lowercased()); refreshCloudView(); syncCloud()
    }
    func deleteCloudProfile(_ uid: String) throws {
        guard let db = cloud?.database, cloud?.session != nil else { throw CloudError.signIn }
        try db.stage(entityType: "profiles", uid: uid, payload: [:], action: "delete")
        refreshCloudView(); try cloud?.publish(); syncCloud()
    }
    func previewLegacy() throws -> [Relationship] {
        guard let db = cloud?.database, let store = store, cloud?.session != nil else { throw CloudError.signIn }
        var profiles = store.archived()
        let current = store.relationship()
        if !profiles.contains(where: { $0.id == current.id }), current != Relationship(id: "current") { profiles.append(current) }
        return try LegacyImporter(database: db).preview(profiles)
    }
    func importLegacy(_ profiles: [Relationship]) {
        do {
            guard let db = cloud?.database else { throw CloudError.signIn }
            try LegacyImporter(database: db).confirm(profiles)
            try cloud?.publish(); refreshCloudView(); syncCloud()
        } catch { cloudStatus = error.localizedDescription }
    }
    func resolveCloud(_ conflict: CloudConflict, useLocal: Bool) {
        do { try cloud?.database?.resolve(opId: conflict.id, useLocal: useLocal); refreshCloudView(); syncCloud() }
        catch { cloudStatus = error.localizedDescription }
    }
    func rebuildCloud() {
        do { try cloud?.rebuild(); refreshCloudView(); syncCloud() } catch { cloudStatus = error.localizedDescription }
    }
    func openCloudContext(_ context: CloudContext) {
        guard context.profileUid == relationship.id else { return }
        closeCloudReview()
        invalidate(clear: true); transcript = context.transcript; confirmed = true; cloudContext = context
        advice = context.result?.advice(); source = "云端已确认聊天"
    }
    func saveCloudPreferences(_ config: AppConfig) throws {
        try config.validate()
        guard let db = cloud?.database, cloud?.session != nil else { throw CloudError.signIn }
        let existing = try db.visibleRecords("preferences").first?.payload ?? [:]
        try db.stage(entityType: "preferences", uid: "00000000-0000-4000-8000-000000000001", payload: config.cloudMergedPreferences(existing))
        syncCloud()
    }
    func closeCloudReview() {
        cloudReviewGeneration = UUID(); cloudReviewTask?.cancel(); cloudReviewTask = nil
        cloudEvidence = nil; cloudHistoryMemory = nil; cloudHistoryVersions = []; cloudHistoryBefore = 0; cloudHistoryMore = false
        cloudReviewBusy = false; cloudReviewStatus = ""
    }
    func openCloudEvidence() {
        guard let context = cloudContext, context.profileUid == relationship.id, let analysisId = context.result?.analysisId, let cloud = cloud else { return }
        closeCloudReview(); let generation = cloudReviewGeneration, identity = cloudReviewIdentity, profile = relationship.id
        cloudReviewBusy = true; cloudReviewStatus = "正在读取原始分析依据…"
        cloudReviewTask = Task {
            do {
                let document = try await cloud.evidence(analysisId: analysisId, context: context)
                guard !Task.isCancelled, cloudReviewGeneration == generation, cloudReviewIdentity == identity, relationship.id == profile else { return }
                cloudEvidence = document; cloudReviewBusy = false; cloudReviewStatus = ""
            } catch {
                guard !Task.isCancelled, cloudReviewGeneration == generation, cloudReviewIdentity == identity, relationship.id == profile else { return }
                cloudReviewBusy = false; cloudReviewStatus = "依据暂不可读取（需联网，原始上下文可能已删除）：" + error.localizedDescription
            }
        }
    }
    func saveCloudMemory(uid: String, draft: CloudMemoryDraft, expectedVersion: Int?, identity: String) throws {
        guard identity == cloudReviewIdentity, !identity.isEmpty, draft.profileUid == relationship.id,
              let db = cloud?.database, cloud?.session != nil else { throw CloudError.signIn }
        try db.saveMemory(uid: uid, draft: draft, expectedVersion: expectedVersion)
        refreshCloudView(); syncCloud()
    }
    func saveCloudClaim(_ claim: CloudEvidenceClaim, document: CloudEvidenceDocument) throws {
        guard document.profileUid == relationship.id, cloudEvidence?.analysisId == document.analysisId, let db = cloud?.database else { throw CloudError.signIn }
        let live = try db.visibleRecords("messages").filter { $0.payload["profile_uid"]?.string == document.profileUid && $0.payload["confirmed"] == .bool(true) }
        let sources = document.verifiedSources(for: claim).map { $0.message.uid }.filter { uid in live.contains { $0.uid == uid } }
        let draft = CloudMemoryDraft(profileUid: document.profileUid, text: claim.text, kind: claim.kind == "fact" ? "fact" : "hypothesis", sourceUids: Array(Set(sources)), changeReason: "从分析依据主动保存")
        try saveCloudMemory(uid: UUID().uuidString.lowercased(), draft: draft, expectedVersion: 0, identity: cloudReviewIdentity)
    }
    func setCloudMemoryStatus(_ memory: CloudRecord, status: String, identity: String) throws {
        var draft = CloudMemoryDraft(record: memory); draft.status = status
        draft.changeReason = status == "confirmed" ? "用户明确确认事实" : "用户退回待确认"
        try saveCloudMemory(uid: memory.uid, draft: draft, expectedVersion: memory.version, identity: identity)
    }
    func deleteCloudMemory(_ memory: CloudRecord, identity: String) throws {
        guard identity == cloudReviewIdentity, !identity.isEmpty, memory.payload["profile_uid"]?.string == relationship.id,
              let db = cloud?.database, cloud?.session != nil else { throw CloudError.signIn }
        try db.stage(entityType: "memories", uid: memory.uid, payload: [:], action: "delete", baseVersion: memory.version)
        refreshCloudView(); syncCloud()
    }
    func openCloudHistory(_ memory: CloudRecord) {
        guard memory.payload["profile_uid"]?.string == relationship.id else { return }
        closeCloudReview(); cloudHistoryMemory = memory; loadCloudHistory()
    }
    func loadCloudHistory() {
        guard !cloudReviewBusy, let memory = cloudHistoryMemory, let cloud = cloud else { return }
        let generation = cloudReviewGeneration, identity = cloudReviewIdentity, profile = relationship.id, before = cloudHistoryBefore
        cloudReviewBusy = true; cloudReviewStatus = ""
        cloudReviewTask = Task {
            do {
                let page = try await cloud.memoryHistory(uid: memory.uid, profileUid: profile, beforeVersion: before)
                guard !Task.isCancelled, cloudReviewGeneration == generation, cloudReviewIdentity == identity, relationship.id == profile else { return }
                let known = Set(cloudHistoryVersions.map(\.version))
                cloudHistoryVersions += page.versions.filter { !known.contains($0.version) }
                cloudHistoryBefore = page.nextBeforeVersion; cloudHistoryMore = page.hasMore; cloudReviewBusy = false
            } catch {
                guard !Task.isCancelled, cloudReviewGeneration == generation, cloudReviewIdentity == identity, relationship.id == profile else { return }
                if case CloudError.http(404, _) = error { cloudHistoryVersions = []; cloudHistoryMore = false }
                cloudReviewBusy = false; cloudReviewStatus = "历史需要联网；已删除的记忆无法恢复：" + error.localizedDescription
            }
        }
    }
    func restoreCloudMemory(_ version: CloudRecord, identity: String) throws {
        guard identity == cloudReviewIdentity, !identity.isEmpty, version.payload["profile_uid"]?.string == relationship.id,
              version.uid == cloudHistoryMemory?.uid, let db = cloud?.database, cloud?.session != nil else { throw CloudError.signIn }
        try db.restoreMemory(version); closeCloudReview(); refreshCloudView(); syncCloud()
    }
    func analyzeCloud(kind: String) {
        guard !working else { return }
        do {
            try Transcript(text: transcript, confirmed: confirmed).validate()
            guard let cloud = cloud, let session = cloud.session, let db = cloud.database else { throw CloudError.signIn }
            let owner = session.accountKey, profile = relationship.id, text = transcript
            let prior = cloudContext
            if kind != "analysis", prior?.result == nil { throw GoutouError.invalid("先选择已有分析结果") }
            invalidate(clear: kind == "analysis"); let generation = revision
            working = true; status = "保存已确认聊天并同步…"
            task = Task { [weak self] in
                do {
                    let conversation: String
                    if let prior = prior, prior.transcript == text { conversation = prior.conversationUid }
                    else { conversation = try db.confirmConversation(profileUid: profile, transcript: text, source: self?.source ?? "手动输入") }
                    try await cloud.sync(manual: true)
                    guard let context = try db.contexts(profileUid: profile).first(where: { $0.id == conversation }), context.contextRevision > 0 else { throw GoutouError.invalid("聊天尚未完成同步；先处理冲突或重试同步") }
                    guard try !db.pendingJobs().contains(where: { $0.conversationUid == conversation }) else { throw GoutouError.invalid("该聊天已有待查询任务，请到云账号页查询或使用原请求 ID 重试") }
                    guard let self = self, self.revision == generation, cloud.session?.accountKey == owner, self.relationship.id == profile else { return }
                    self.cloudContext = context; self.status = "提交持久分析任务…"
                    let pending = try await cloud.submit(conversationUid: conversation, revision: context.contextRevision, kind: kind, parent: prior?.result?.analysisId, config: self.config)
                    self.refreshCloudView()
                    for _ in 0..<60 {
                        try Task.checkCancellation()
                        let job = try await cloud.query(pending)
                        guard self.revision == generation, cloud.session?.accountKey == owner, self.relationship.id == profile, self.cloudContext?.id == conversation else { return }
                        if job.status == "succeeded", let result = job.result {
                            guard result.matches(conversationUid: conversation, revision: context.contextRevision) else { throw CloudError.protocolError }
                            self.advice = result.advice(); self.cloudContext?.result = result
                            self.cloudExplanation = result.text ?? ""; self.working = false; self.confirmed = true; self.status = "云端分析完成"
                            await self.performSync(manual: true); return
                        }
                        if ["failed", "cancelled", "indeterminate"].contains(job.status) { throw GoutouError.invalid("任务 \(job.status)：\(job.error?.code ?? "结果不明时请手动决定是否重新发起")") }
                        self.status = "云端任务 \(job.status)；关闭页面后可继续查询"
                        try await Task.sleep(nanoseconds: 2_000_000_000)
                    }
                    self.working = false; self.status = "任务仍在运行，可在云端账号页查询。"
                } catch {
                    guard let self = self, self.revision == generation else { return }
                    self.working = false; self.status = error is CancellationError ? "已停止等待；云端任务保留，可再次查询。" : error.localizedDescription
                    self.refreshCloudView()
                }
            }
        } catch { status = error.localizedDescription }
    }
    func retryCloudJob(_ pending: CloudPendingJob) {
        let owner = cloud?.session?.accountKey, device = cloud?.session?.deviceId
        Task {
            do {
                _ = try await cloud?.retryOriginal(pending, config: config)
                guard cloud?.session?.accountKey == owner, cloud?.session?.deviceId == device else { return }
                refreshCloudView(); await queryKnownJobs()
            } catch {
                guard cloud?.session?.accountKey == owner, cloud?.session?.deviceId == device else { return }
                cloudStatus = error.localizedDescription
            }
        }
    }
    func queryKnownJobs() async {
        guard cloudEnabled, let cloud = cloud, let db = cloud.database, let owner = cloud.session?.accountKey, let device = cloud.session?.deviceId else { return }
        do {
            for pending in try db.pendingJobs() where pending.analysisId != nil {
                let job = try await cloud.query(pending)
                guard cloud.session?.accountKey == owner, cloud.session?.deviceId == device else { return }
                if job.status == "succeeded", let result = job.result, cloud.session?.accountKey == owner,
                   let context = cloudContext, context.profileUid == relationship.id,
                   result.matches(conversationUid: context.id, revision: context.contextRevision), !working {
                    advice = result.advice(); cloudContext?.result = result
                }
            }
            guard cloud.session?.accountKey == owner, cloud.session?.deviceId == device else { return }
            refreshCloudView()
        } catch {
            guard cloud.session?.accountKey == owner, cloud.session?.deviceId == device else { return }
            cloudStatus = error.localizedDescription
        }
    }
}
