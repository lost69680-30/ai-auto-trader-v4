package com.aiautotrader.v4

import org.junit.Assert.assertEquals
import org.junit.Test

class SoloAlertTransitionTest {
    @Test fun initialInactiveSnapshotDoesNotCreateAnAlert() {
        assertEquals(SoloAlertTransitionAction.NONE, evaluateSoloAlertTransition(null, false))
    }

    @Test fun firstVerifiedActiveSnapshotCreatesAnAlert() {
        assertEquals(SoloAlertTransitionAction.PUBLISH, evaluateSoloAlertTransition(null, true))
    }

    @Test fun repeatedActiveSnapshotsDoNotCreateDuplicateAlerts() {
        assertEquals(SoloAlertTransitionAction.NONE, evaluateSoloAlertTransition(true, true))
    }

    @Test fun recoveryResolvesTheExistingAlert() {
        assertEquals(SoloAlertTransitionAction.RESOLVE, evaluateSoloAlertTransition(true, false))
    }

    @Test fun aNewActivationAfterRecoveryCreatesANewAlert() {
        assertEquals(SoloAlertTransitionAction.PUBLISH, evaluateSoloAlertTransition(false, true))
    }
}
