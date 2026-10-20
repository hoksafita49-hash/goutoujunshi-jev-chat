package com.jev.probe.cloud

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class CloudContractsTest {
    @Test fun pipelineRewriteTextKeepsOriginalAnalysisAndSelectedCandidate() {
        fun fixture(name: String) = JSONObject(checkNotNull(javaClass.classLoader!!.getResourceAsStream(name)).bufferedReader().use { it.readText() })
        val parent = CloudProtocol.analysis(fixture("cloud-v1-result.json"))
        val rewrite = CloudRewrite.read(fixture("cloud-v1-rewrite.json"), parent, parent.rankedReplies[0].text)
        assertEquals("嗯，你先忙，咱们有空再聊。", rewrite.text)
        assertSame(parent, rewrite.parent)
        assertEquals(listOf("那你先忙", "有空再聊"), parent.rankedReplies.map { it.text })
        assertEquals(parent.rankedReplies[0].text, rewrite.selectedText)
        assertNotEquals(parent.analysisId, rewrite.taskId)
    }
    @Test fun sharedCrossPlatformResultFixtureKeepsOrderAndUnknownFieldsAreCompatible() {
        val fixture = JSONObject(checkNotNull(javaClass.classLoader!!.getResourceAsStream("cloud-v1-result.json")).bufferedReader().use { it.readText() })
        val result = CloudProtocol.analysis(fixture)
        assertEquals(listOf("那你先忙", "有空再聊"), result.rankedReplies.map { it.text })
        assertTrue(result.rankedReplies.all { it.prob == null })
        assertEquals(3, result.contextRevision)
        assertEquals(listOf("可能暂时没有空回复"), result.hypotheses)
    }
    @Test fun onlyHttpsOrExactDevelopmentLoopbackCanReceiveCredentials() {
        assertEquals("https://api.example.com", CloudProtocol.baseUrl("https://api.example.com/"))
        assertEquals("http://127.0.0.1:18000", CloudProtocol.baseUrl("http://127.0.0.1:18000"))
        for (url in listOf("http://example.com", "http://192.168.1.1:18000", "https://user:pass@example.com", "https://example.com?q=1", "https://example.com/#secret", "https://example.com/path")) {
            assertThrows(IllegalArgumentException::class.java) { CloudProtocol.baseUrl(url) }
        }
    }
    @Test fun unavailableRankingKeepsServerOrderAndNullWeights() {
        val result = JSONObject("""{"schema_version":1,"analysis_id":"a","conversation_uid":"c","context_revision":2,"facts":[],"hypotheses":[],"unknowns":[],"strategy":"承接","stop_condition":"","candidates":[{"candidate_id":"second","text":"乙","weight":null},{"candidate_id":"first","text":"甲","weight":null}],"ranking_status":"unavailable","pipeline_version":"cloud-v1","extra":"future"}""")
        val parsed = CloudProtocol.analysis(result)
        assertEquals(listOf("乙", "甲"), parsed.rankedReplies.map { it.text })
        assertTrue(parsed.rankedReplies.all { it.prob == null })
        assertEquals(listOf("second", "first"), parsed.rankedReplies.map { it.candidateId })
        result.remove("conversation_uid")
        assertThrows(IllegalArgumentException::class.java) { CloudProtocol.analysis(result) }
    }
    @Test fun legacyIdentityIsStablePerAccountAndDoesNotCollapseShortMessages() {
        assertEquals(CloudProtocol.legacyUid("account-a", "log/person/0"), CloudProtocol.legacyUid("account-a", "log/person/0"))
        assertNotEquals(CloudProtocol.legacyUid("account-a", "log/person/0"), CloudProtocol.legacyUid("account-b", "log/person/0"))
        assertNotEquals(CloudProtocol.legacyUid("account-a", "log/person/0"), CloudProtocol.legacyUid("account-a", "log/person/1"))
        assertEquals(listOf("me\u0000好", "me\u0000好"), CloudProtocol.newScreenTail(emptyList(), listOf("me\u0000好", "me\u0000好")))
        assertEquals(emptyList<String>(), CloudProtocol.newScreenTail(listOf("me\u0000好", "me\u0000好"), listOf("me\u0000好", "me\u0000好")))
        assertEquals(listOf("other\u0000好"), CloudProtocol.newScreenTail(listOf("me\u0000好", "other\u0000嗯"), listOf("other\u0000嗯", "other\u0000好")))
    }
}
