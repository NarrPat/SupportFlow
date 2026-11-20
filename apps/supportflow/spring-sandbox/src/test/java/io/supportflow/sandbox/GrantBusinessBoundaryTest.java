package io.supportflow.sandbox;

import org.junit.jupiter.api.Test;
import java.time.Instant;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;

class GrantBusinessBoundaryTest {
    private static final Instant NOW = Instant.parse("2026-11-07T00:00:00Z");
    private static final String HASH = "0".repeat(64);
    private static final GrantStore.Scope SCOPE = new GrantStore.Scope("tenant_a","customer","sub");

    @Test void expiryEqualityCannotStartNewGrant() {
        var command = new GrantCommand("action","proposal","tenant_a","customer","sub",10,3,7,"v1",NOW,HASH,"auth",NOW);
        var sub = new GrantStore.Subscription("sub",100,100,0,null,"active",7);
        assertDoesNotThrow(() -> GrantStore.verifyCurrent(command,sub,NOW.minusNanos(1000)));
        assertEquals("PROPOSAL_EXPIRED",assertThrows(SandboxError.class,() -> GrantStore.verifyCurrent(command,sub,NOW)).getMessage());
    }

    @Test void cooldownEqualityAndTemporaryExpiryUseClosedOpenIntervals() {
        var committed = NOW.minusSeconds(2592000);
        var expiry = committed.plusSeconds(86400);
        var receipt = new GrantStore.Receipt("old",SCOPE,HASH,6,7,committed,4,1,expiry,"v1","old-p","old-auth",committed);
        var sub = new GrantStore.Subscription("sub",100,100,4,expiry,"active",7);
        assertEquals("GRANT_COOLDOWN_ACTIVE",assertThrows(SandboxError.class,
            () -> GrantStore.verifyHistory(sub,SCOPE,List.of(receipt),NOW.minusNanos(1000))).getMessage());
        assertDoesNotThrow(() -> GrantStore.verifyHistory(sub,SCOPE,List.of(receipt),NOW));
        var noHistory = new GrantStore.Subscription("sub",100,100,4,NOW,"active",7);
        assertEquals("TEMPORARY_GRANT_ACTIVE",assertThrows(SandboxError.class,
            () -> GrantStore.verifyHistory(noHistory,SCOPE,List.of(),NOW.minusNanos(1000))).getMessage());
        assertDoesNotThrow(() -> GrantStore.verifyHistory(noHistory,SCOPE,List.of(),NOW));
    }
}
