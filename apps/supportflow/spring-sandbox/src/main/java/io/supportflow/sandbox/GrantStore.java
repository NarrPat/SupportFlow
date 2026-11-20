package io.supportflow.sandbox;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.context.annotation.Profile;
import org.springframework.dao.DataAccessException;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.TransactionException;
import org.springframework.transaction.support.TransactionTemplate;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** All facts, subscription row locks, receipts and policy observations use one business data source. */
@Component
@Profile("!migrate")
class GrantStore {
    static final int HISTORY_MAX = 50;
    static final String RECEIPT_COLUMNS = "action_id,tenant_id,customer_id,subscription_id,canonical_args_hash,"
        + "before_version,after_version,committed_business_at,seats,days,temporary_expires_at,policy_version,"
        + "proposal_id,authorization_id,authorized_at";
    private static final String SUB_COLUMNS = "subscription_id,base_seats,used_seats,temporary_seats,temporary_expires_at,plan_status,business_version";
    private final JdbcClient db;
    private final TransactionTemplate reader;
    private final TransactionTemplate writer;
    private final ObjectMapper json;
    private final GrantPolicyPin pin;

    GrantStore(JdbcClient db, PlatformTransactionManager transactions, ObjectMapper json) {
        this.db = db;
        this.json = json;
        this.pin = GrantPolicyPin.load(json);
        reader = new TransactionTemplate(transactions);
        reader.setReadOnly(true);
        reader.setIsolationLevel(TransactionDefinition.ISOLATION_REPEATABLE_READ);
        reader.setTimeout(4);
        writer = new TransactionTemplate(transactions);
        writer.setIsolationLevel(TransactionDefinition.ISOLATION_READ_COMMITTED);
        writer.setTimeout(7);
    }

    <T> T read(java.util.function.Supplier<T> operation) {
        return reader.execute(status -> operation.get());
    }

    void assertReady() {
        db.sql("select exists(select 1 from subscriptions) OR exists(select 1 from grant_policy_state)")
            .query(Boolean.class).single();
    }

    record Scope(String tenant, String customer, String subscription) {}
    record Subscription(String id, int base, int used, int temporary, Instant expires, String plan, long version) {
        Map<String,Object> wire() {
            var result = new LinkedHashMap<String,Object>();
            result.put("subscription_id",id);
            result.put("base_seats",base);
            result.put("used_seats",used);
            result.put("temporary_seats",temporary);
            result.put("temporary_expires_at",stamp(expires));
            result.put("plan_status",plan);
            result.put("business_version",version);
            return result;
        }
    }

    record Receipt(String action, Scope scope, String hash, long before, long after, Instant committed, int seats, int days,
                   Instant expires, String policy, String proposal, String authorization, Instant authorized) {
        Map<String,Object> wire(boolean audit) {
            var result = new LinkedHashMap<String,Object>();
            result.put("action_id",action);
            result.put("tenant_id",scope.tenant);
            result.put("customer_id",scope.customer);
            result.put("subscription_id",scope.subscription);
            result.put("canonical_args_hash",hash);
            result.put("before_version",before);
            result.put("after_version",after);
            result.put("committed_business_at",stamp(committed));
            result.put("seats",seats);
            result.put("days",days);
            result.put("temporary_expires_at",stamp(expires));
            result.put("policy_version",policy);
            if (audit) {
                result.put("proposal_id",proposal);
                result.put("authorization_id",authorization);
                result.put("authorized_at",stamp(authorized));
            }
            return result;
        }
    }

    record GrantResult(Receipt receipt, boolean created) {}

    static Instant instant(ResultSet rows, String column) throws SQLException {
        var value = rows.getObject(column,OffsetDateTime.class);
        return value == null ? null : value.toInstant();
    }

    static String stamp(Instant value) { return value == null ? null : GrantCommand.utc(value); }

    private static final RowMapper<Subscription> SUB = (r,n) -> new Subscription(r.getString("subscription_id"),
        r.getInt("base_seats"),r.getInt("used_seats"),r.getInt("temporary_seats"),instant(r,"temporary_expires_at"),
        r.getString("plan_status"),r.getLong("business_version"));

    private static final RowMapper<Receipt> RECEIPT = (r,n) -> new Receipt(r.getString("action_id"),
        new Scope(r.getString("tenant_id"),r.getString("customer_id"),r.getString("subscription_id")),
        r.getString("canonical_args_hash"),r.getLong("before_version"),r.getLong("after_version"),instant(r,"committed_business_at"),
        r.getInt("seats"),r.getInt("days"),instant(r,"temporary_expires_at"),r.getString("policy_version"),
        r.getString("proposal_id"),r.getString("authorization_id"),instant(r,"authorized_at"));

    private Subscription subscription(String tenant, String customer, String subscription, boolean lock) {
        if (lock && subscription == null) throw new IllegalArgumentException("Resource lock needs a subscription ID");
        String query = "SELECT " + SUB_COLUMNS + " FROM subscriptions WHERE tenant_id=:tenant AND customer_id=:customer";
        var statement = db.sql(query + (lock ? " AND subscription_id=:subscription FOR UPDATE" : ""))
            .param("tenant",tenant).param("customer",customer);
        if (lock) statement.param("subscription",subscription);
        return statement.query(SUB).optional().orElseThrow(SandboxError::missing);
    }

    Map<String,Object> subscription(String tenant, String customer, boolean forGrant) {
        return read(() -> {
            var sub = subscription(tenant,customer,null,false);
            var result = sub.wire();
            if (forGrant) {
                var scope = new Scope(tenant,customer,sub.id);
                var receipts = history(scope);
                var policy = currentPolicy();
                // The Python response DTO cannot return a 51-item list. Do not silently report an empty history.
                if (receipts.size()>HISTORY_MAX || policy==null || !validPolicyShape(policy)) throw SandboxError.invalidFacts();
                result.put("grant_history",Map.of("complete",true,"receipts",receipts.stream().map(r -> r.wire(false)).toList()));
                result.put("policy",policy);
            }
            return result;
        });
    }

    JsonNode parseJson(String value) {
        try { return json.readTree(value); }
        catch (JsonProcessingException error) { throw SandboxError.invalidFacts(); }
    }

    private ObjectNode currentPolicy() {
        return db.sql("SELECT policy_version,section_id,content_sha256,constants::text FROM grant_policy_state WHERE singleton")
            .query((r,n) -> {
                ObjectNode policy = json.createObjectNode();
                policy.put("policy_version",r.getString(1));
                policy.put("section_id",r.getString(2));
                policy.put("content_sha256",r.getString(3));
                policy.set("constants",parseJson(r.getString(4)));
                return policy;
            }).optional().orElse(null);
    }

    private boolean validPolicyShape(ObjectNode policy) {
        var expected = pin.bundle().path("constants");
        var constants = policy.path("constants");
        if (!constants.isObject() || expected.size()!=constants.size()) return false;
        var names = expected.fieldNames();
        while (names.hasNext()) {
            String key = names.next();
            if (constants.path(key).getNodeType()!=expected.path(key).getNodeType()
                    || expected.path(key).isIntegralNumber() && !constants.path(key).isIntegralNumber()) return false;
        }
        return true;
    }

    private List<Receipt> history(Scope scope) {
        return db.sql("SELECT " + RECEIPT_COLUMNS + " FROM action_receipts WHERE tenant_id=:tenant AND customer_id=:customer "
            + "AND subscription_id=:subscription ORDER BY committed_business_at DESC,action_id LIMIT 51")
            .param("tenant",scope.tenant).param("customer",scope.customer).param("subscription",scope.subscription)
            .query(RECEIPT).list();
    }

    private Receipt lookup(String action, Scope scope, String hash, boolean replay) {
        var record = db.sql("SELECT " + RECEIPT_COLUMNS + " FROM action_receipts WHERE action_id=:action")
            .param("action",action).query(RECEIPT).optional().orElse(null);
        if (record==null) return null;
        if (!record.scope.equals(scope)) {
            if (replay && record.scope.tenant.equals(scope.tenant)) throw SandboxError.conflict("IDEMPOTENCY_CONFLICT");
            throw SandboxError.missing();
        }
        if (!record.hash.equals(hash)) throw SandboxError.conflict("IDEMPOTENCY_CONFLICT");
        if (record.before==Long.MAX_VALUE || record.after!=record.before+1 || record.proposal.isBlank()
            || record.authorization.isBlank() || !record.expires.equals(record.committed.plusSeconds(record.days*86400L)))
            throw SandboxError.conflict("RECEIPT_INCOMPATIBLE");
        return record;
    }

    Receipt receipt(String action, Scope scope, String hash) {
        return read(() -> {
            var receipt = lookup(action,scope,hash,false);
            if (receipt==null) throw SandboxError.missing();
            return receipt;
        });
    }

    GrantResult grant(GrantCommand command, String tenant) {
        if (!command.tenantId().equals(tenant)) throw SandboxError.missing();
        command.validateHash();
        var scope = new Scope(tenant,command.customerId(),command.subscriptionId());
        try {
            return writer.execute(status -> grantLocked(command,scope));
        } catch (DuplicateKeyException race) {
            // Any attempted subscription change has been rolled back by the first transaction.
            // A competing global action key on another resource must not expose that receipt.
            try { return new GrantResult(receipt(command.actionId(),scope,command.argsHash()),false); }
            catch (DataAccessException | TransactionException failedLookup) { throw new SandboxError(503,"DEPENDENCY_UNAVAILABLE"); }
        } catch (DataAccessException | TransactionException unavailable) {
            throw new SandboxError(503,"DEPENDENCY_UNAVAILABLE");
        }
    }

    private GrantResult grantLocked(GrantCommand command, Scope scope) {
        var prior = lookup(command.actionId(),scope,command.argsHash(),true);
        if (prior!=null) return new GrantResult(prior,false);
        var sub = subscription(scope.tenant,scope.customer,scope.subscription,true);
        prior = lookup(command.actionId(),scope,command.argsHash(),true);
        if (prior!=null) return new GrantResult(prior,false);
        if (!pin.bundle().equals(currentPolicy()) || !pin.bundle().path("policy_version").asText().equals(command.policyVersion()))
            throw SandboxError.conflict("POLICY_INVALID");
        var now = db.sql("SELECT clock_timestamp() AS now").query((r,n) -> instant(r,"now")).single();
        verifyCurrent(command,sub,now);
        verifyHistory(sub,scope,history(scope),now);
        if ((long)sub.base-sub.used>=1) throw SandboxError.conflict("CAPACITY_NOT_EXHAUSTED");
        if ((long)sub.base+command.seats()-sub.used<1) throw SandboxError.conflict("GRANT_CAPACITY_INSUFFICIENT");
        var receipt = new Receipt(command.actionId(),scope,command.argsHash(),sub.version,sub.version+1,now,
            command.seats(),command.days(),now.plusSeconds(command.days()*86400L),command.policyVersion(),
            command.proposalId(),command.authorizationId(),command.authorizedAt());
        int updated = db.sql("UPDATE subscriptions SET temporary_seats=:seats,temporary_expires_at=:expires,business_version=:version "
             + "WHERE tenant_id=:tenant AND customer_id=:customer AND subscription_id=:subscription")
            .param("seats",receipt.seats).param("expires",receipt.expires.atOffset(ZoneOffset.UTC)).param("version",receipt.after)
            .param("tenant",scope.tenant).param("customer",scope.customer).param("subscription",scope.subscription).update();
        if (updated!=1) throw new SandboxError(503,"DEPENDENCY_UNAVAILABLE");
        int inserted = db.sql("INSERT INTO action_receipts (" + RECEIPT_COLUMNS + ") VALUES "
             + "(:action,:tenant,:customer,:subscription,:hash,:before,:after,:committed,:seats,:days,:expires,:policy,:proposal,:authorization,:authorized)")
            .param("action",receipt.action).param("tenant",scope.tenant).param("customer",scope.customer).param("subscription",scope.subscription)
            .param("hash",receipt.hash).param("before",receipt.before).param("after",receipt.after)
            .param("committed",now.atOffset(ZoneOffset.UTC)).param("seats",receipt.seats).param("days",receipt.days)
            .param("expires",receipt.expires.atOffset(ZoneOffset.UTC)).param("policy",receipt.policy)
            .param("proposal",receipt.proposal).param("authorization",receipt.authorization)
            .param("authorized",receipt.authorized.atOffset(ZoneOffset.UTC)).update();
        if (inserted!=1) throw new SandboxError(503,"DEPENDENCY_UNAVAILABLE");
        return new GrantResult(receipt,true);
    }

    static void verifyCurrent(GrantCommand command, Subscription sub, Instant now) {
        if (!now.isBefore(command.proposalExpiresAt())) throw SandboxError.conflict("PROPOSAL_EXPIRED");
        if (sub.version!=command.expectedBusinessVersion()) throw SandboxError.conflict("BUSINESS_VERSION_CHANGED");
        if (sub.version==Long.MAX_VALUE) throw SandboxError.conflict("BUSINESS_VERSION_EXHAUSTED");
        if (!sub.plan.equals("active")) throw SandboxError.conflict(switch (sub.plan) {
            case "inactive", "cancelled", "suspended" -> "PLAN_NOT_ACTIVE";
            default -> "FACTS_INCOMPLETE";
        });
    }

    static void verifyHistory(Subscription sub, Scope scope, List<Receipt> receipts, Instant now) {
        if (receipts.size()>HISTORY_MAX) throw SandboxError.conflict("GRANT_HISTORY_INVALID");
        Receipt previous = null;
        var actions = new HashSet<String>();
        for (var receipt : receipts) {
            if (!receipt.scope.equals(scope) || !actions.add(receipt.action) || receipt.before==Long.MAX_VALUE
                || receipt.after != receipt.before+1 || receipt.after>sub.version || receipt.committed.isAfter(now)
                || !receipt.expires.equals(receipt.committed.plusSeconds(receipt.days*86400L))
                || previous!=null && (!receipt.committed.isBefore(previous.committed) || receipt.after>=previous.after))
                throw SandboxError.conflict("GRANT_HISTORY_INVALID");
            previous = receipt;
        }
        if (!receipts.isEmpty()) {
            var latest = receipts.getFirst();
            if (latest.after==sub.version && (sub.temporary!=latest.seats || !latest.expires.equals(sub.expires)))
                throw SandboxError.conflict("GRANT_HISTORY_INVALID");
        }
        if (sub.temporary>0 && sub.expires!=null && now.isBefore(sub.expires)) throw SandboxError.conflict("TEMPORARY_GRANT_ACTIVE");
        if (!receipts.isEmpty() && now.isBefore(receipts.getFirst().committed.plusSeconds(2592000)))
            throw SandboxError.conflict("GRANT_COOLDOWN_ACTIVE");
    }
}
