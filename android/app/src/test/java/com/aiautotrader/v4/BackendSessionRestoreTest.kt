package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Test

class BackendSessionRestoreTest {
    @Test
    fun validSavedTokenIsRestoredForBothDisplayAndAutomaticConnection() {
        val token = "unit-test-bearer-token-with-more-than-thirty-two-characters"

        val restored = restoredBackendSession(token)

        assertEquals(token, restored.editableToken)
        assertEquals(token, restored.activeToken)
    }

    @Test
    fun missingOrInvalidSavedTokenDoesNotActivateConnection() {
        assertEquals("", restoredBackendSession(null).editableToken)
        assertEquals("", restoredBackendSession(null).activeToken)
        assertEquals("", restoredBackendSession("too-short").editableToken)
        assertEquals("", restoredBackendSession("too-short").activeToken)
    }
}
