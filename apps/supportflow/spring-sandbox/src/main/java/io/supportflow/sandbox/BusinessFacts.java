package io.supportflow.sandbox;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.context.annotation.Profile;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

@Component
@Profile("!migrate")
class BusinessFacts {
    private static final Pattern DOMAIN = Pattern.compile("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?");
    private static final Pattern LOCAL = Pattern.compile("[a-z0-9.!#$%&'*+/=?^_`{|}~-]+");
    private final GrantStore store;
    private final JdbcClient db;

    BusinessFacts(GrantStore store, JdbcClient db) { this.store = store; this.db = db; }

    static String normalizeDomain(String value) {
        if (value.codePoints().anyMatch(c -> c>127)) throw new IllegalArgumentException("Non-ASCII domain");
        String domain = value.strip().toLowerCase(Locale.ROOT);
        if (domain.length()>253 || domain.isEmpty()) throw new IllegalArgumentException("Invalid domain");
        for (String label : domain.split("\\.",-1)) {
            if (!DOMAIN.matcher(label).matches()) throw new IllegalArgumentException("Invalid domain label");
        }
        return domain;
    }

    static String normalizeEmail(String value) {
        if (value.codePoints().anyMatch(c -> c>127)) throw new IllegalArgumentException("Non-ASCII email");
        String email = value.strip().toLowerCase(Locale.ROOT);
        String[] parts = email.split("@",-1);
        if (parts.length!=2 || email.length()>254 || parts[0].length()>64 || !LOCAL.matcher(parts[0]).matches()
                || parts[0].startsWith(".") || parts[0].endsWith(".") || parts[0].contains(".."))
            throw new IllegalArgumentException("Invalid email");
        return parts[0]+"@"+normalizeDomain(parts[1]);
    }

    record Invitation(String id, String email, String domain, Instant created, Instant expires, String status, String reason) {
        Map<String,Object> wire() {
            var out = new LinkedHashMap<String,Object>();
            out.put("invitation_id",id);
            out.put("email",email);
            out.put("email_domain",domain);
            out.put("created_at",GrantStore.stamp(created));
            out.put("expires_at",GrantStore.stamp(expires));
            out.put("status",status);
            out.put("reason_code",reason);
            return out;
        }
    }

    private static final RowMapper<Invitation> INVITATION = (row,n) -> new Invitation(row.getString("invitation_id"),
        row.getString("email"),row.getString("email_domain"),GrantStore.instant(row,"created_at"),
        GrantStore.instant(row,"expires_at"),row.getString("status"),row.getString("reason_code"));

    private Map<String,Object> invitationPolicy(String tenant, String customer) {
        var policy = db.sql("SELECT allowed_domains::text, allow_subdomains, invitation_permission "
                + "FROM invitation_policies WHERE tenant_id=:tenant AND customer_id=:customer")
            .param("tenant",tenant).param("customer",customer).query((r,n) -> {
                var out = new LinkedHashMap<String,Object>();
                String rawDomains = r.getString(1);
                if (rawDomains==null) out.put("allowed_domains",null);
                else {
                    JsonNode domains = store.parseJson(rawDomains);
                    if (!domains.isArray() || domains.size()>50) throw SandboxError.invalidFacts();
                    var normalized = new ArrayList<String>();
                    for (var domain : domains) {
                        if (!domain.isTextual()) throw SandboxError.invalidFacts();
                        normalized.add(normalizeDomain(domain.textValue()));
                    }
                    out.put("allowed_domains",normalized);
                }
                out.put("allow_subdomains",r.getObject(2,Boolean.class));
                out.put("invitation_permission",r.getObject(3,Boolean.class));
                return out;
            }).optional();
        if (policy.isPresent()) return policy.get();
        var unknown = new LinkedHashMap<String,Object>();
        for (String key : new String[]{"allowed_domains","allow_subdomains","invitation_permission"}) unknown.put(key,null);
        return unknown;
    }

    Map<String,Object> invitation(String tenant, String customer, String email) {
        return store.read(() -> {
            var rows = db.sql("SELECT invitation_id,email,email_domain,created_at,expires_at,status,reason_code FROM invitations "
                + "WHERE tenant_id=:tenant AND customer_id=:customer AND email=:email ORDER BY created_at DESC,invitation_id DESC LIMIT 6")
                .param("tenant",tenant).param("customer",customer).param("email",email).query(INVITATION).list();
            if (rows.isEmpty()) throw SandboxError.missing();
            try {
                for (var row : rows) {
                    if (!email.equals(normalizeEmail(row.email)) || !row.domain.equals(email.substring(email.indexOf('@')+1))
                        || !row.expires.isAfter(row.created)) throw SandboxError.invalidFacts();
                }
                return Map.of("invitation",rows.getFirst().wire(),"policy",invitationPolicy(tenant,customer),
                    "history",rows.subList(1,rows.size()).stream().map(Invitation::wire).toList());
            } catch (IllegalArgumentException inconsistentData) {
                throw SandboxError.invalidFacts();
            }
        });
    }

    Map<String,Object> serviceStatus(String tenant, String customer) {
        return store.read(() -> db.sql("SELECT component,status,updated_at FROM service_statuses "
                + "WHERE tenant_id=:tenant AND customer_id=:customer AND component='invitations'")
            .param("tenant",tenant).param("customer",customer).<Map<String,Object>>query((r,n) ->
                Map.of("component",r.getString("component"),"status",r.getString("status"),
                    "updated_at",GrantStore.stamp(GrantStore.instant(r,"updated_at"))))
            .optional().orElseThrow(SandboxError::missing));
    }
}
