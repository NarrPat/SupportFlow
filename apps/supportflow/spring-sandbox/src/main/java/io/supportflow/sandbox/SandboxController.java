package io.supportflow.sandbox;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Profile;
import org.springframework.dao.DataAccessException;
import org.springframework.http.ResponseEntity;
import org.springframework.transaction.TransactionException;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

/** Internal wire endpoints; callers supply identity only after authenticating as the Python service. */
@RestController
@Profile("!migrate")
class SandboxController {
    private static final Pattern IDENTITY = Pattern.compile("[A-Za-z0-9_-]{1,128}");
    private static final Pattern HASH = Pattern.compile("[0-9a-f]{64}");
    private final GrantStore store;
    private final BusinessFacts facts;
    private final byte[] expectedToken;

    SandboxController(GrantStore store, BusinessFacts facts, @Value("${sf.service-token-file}") String tokenFile) throws IOException {
        this.store = store;
        this.facts = facts;
        String token = Files.readString(Path.of(tokenFile), StandardCharsets.US_ASCII).strip();
        if (!token.matches("[A-Za-z0-9._~+/-]{32,512}={0,2}")) throw new IllegalStateException("Invalid sandbox service credential");
        expectedToken = ("Bearer " + token).getBytes(StandardCharsets.UTF_8);
    }

    @GetMapping("/health")
    ResponseEntity<Map<String,String>> health() {
        try {
            store.assertReady();
            return ResponseEntity.ok(Map.of("status", "ok"));
        } catch (DataAccessException | TransactionException unavailable) {
            return ResponseEntity.status(503).body(Map.of("status", "unavailable"));
        }
    }

    private String identity(HttpServletRequest request, boolean internal) {
        String auth = header(request,"Authorization");
        if (!MessageDigest.isEqual(expectedToken,auth.getBytes(StandardCharsets.UTF_8)))
            throw new SandboxError(401,internal ? "UNAUTHORIZED" : "FORBIDDEN");
        String tenant = header(request,"X-SupportFlow-Tenant"), actor = header(request,"X-SupportFlow-Actor");
        if (!IDENTITY.matcher(tenant).matches() || !IDENTITY.matcher(actor).matches())
            throw new SandboxError(internal ? 422 : 400,internal ? "VALIDATION_ERROR" : "VALIDATION");
        return tenant;
    }

    private static String header(HttpServletRequest request, String key) {
        String v = request.getHeader(key);
        return v==null ? "" : v;
    }

    @GetMapping("/api/v1/customers/{customerId}/{kind:subscription|subscription-grant|invitation|service-status}")
    ResponseEntity<Map<String,Object>> read(@PathVariable String customerId, @PathVariable String kind, HttpServletRequest request) {
        try {
            String tenant = identity(request,false);
            return switch (kind) {
                case "subscription" -> envelope(200,store.subscription(tenant,customerId,false),null,"sandbox.subscriptions",false);
                case "subscription-grant" -> envelope(200,store.subscription(tenant,customerId,true),null,"sandbox.subscription_grant",false);
                case "invitation" -> envelope(200,facts.invitation(tenant,customerId,email(request)),null,"sandbox.invitations",false);
                case "service-status" -> envelope(200,facts.serviceStatus(tenant,customerId),null,"sandbox.service_statuses",false);
                default -> throw new SandboxError(404,"NOT_FOUND");
            };
        } catch (SandboxError error) {
            return envelope(error.status,null,error.getMessage(),"sandbox",error.retryable);
        } catch (DataAccessException | TransactionException unavailable) {
            return envelope(503,null,"UPSTREAM_UNAVAILABLE","sandbox",true);
        }
    }

    private static String email(HttpServletRequest request) {
        String[] values = request.getParameterValues("email");
        if (values==null || values.length==0) throw new SandboxError(422,"VALIDATION");
        try { return BusinessFacts.normalizeEmail(values[values.length-1]); }
        catch (IllegalArgumentException error) { throw new SandboxError(422,"VALIDATION"); }
    }

    @PostMapping("/internal/v1/subscriptions/{subscriptionId}/temporary-seat-grants")
    ResponseEntity<Map<String,Object>> grant(@PathVariable String subscriptionId, HttpServletRequest request) {
        String source = "sandbox.temporary_grants";
        try {
            String tenant = identity(request,true);
            if (request.getQueryString()!=null && !request.getQueryString().isEmpty()) throw new GrantCommand.Invalid(422);
            // Validate after auth, before database access. The command is a single bounded JSON object.
            var command = GrantCommand.decode(request.getInputStream().readNBytes(65537));
            if (!subscriptionId.equals(command.subscriptionId())) throw new GrantCommand.Invalid(422);
            var result = store.grant(command,tenant);
            // TransactionTemplate returned after transaction completion; no receipt can escape before commit.
            return envelope(result.created() ? 201 : 200,result.receipt().wire(true),null,source,false);
        } catch (GrantCommand.Invalid error) {
            return envelope(error.status,null,error.getMessage(),source,false);
        } catch (SandboxError error) {
            return envelope(error.status,null,error.getMessage(),source,false);
        } catch (IOException invalidBody) {
            return envelope(422,null,"VALIDATION_ERROR",source,false);
        }
    }

    @GetMapping("/internal/v1/actions/{actionId}")
    ResponseEntity<Map<String,Object>> receipt(@PathVariable String actionId, HttpServletRequest request) {
        String source = "sandbox.action_receipts";
        try {
            String tenant = identity(request,true);
            Map<String,String[]> params = request.getParameterMap();
            if (!params.keySet().equals(Set.of("customer_id","subscription_id","canonical_args_hash"))
                    || params.values().stream().anyMatch(items -> items.length!=1)) throw new GrantCommand.Invalid(422);
            String customer = params.get("customer_id")[0], subscription = params.get("subscription_id")[0],
                hash = params.get("canonical_args_hash")[0];
            if (!GrantCommand.validId(actionId) || !GrantCommand.validId(customer) || !GrantCommand.validId(subscription) || !HASH.matcher(hash).matches())
                throw new GrantCommand.Invalid(422);
            return envelope(200,store.receipt(actionId,new GrantStore.Scope(tenant,customer,subscription),hash).wire(true),null,source,false);
        } catch (GrantCommand.Invalid error) {
            return envelope(error.status,null,error.getMessage(),source,false);
        } catch (SandboxError error) {
            return envelope(error.status,null,error.getMessage(),source,error.retryable);
        } catch (DataAccessException | TransactionException unavailable) {
            return envelope(503,null,"DEPENDENCY_UNAVAILABLE",source,true);
        }
    }

    private static ResponseEntity<Map<String,Object>> envelope(int status, Map<String,Object> data, String code,
            String source, boolean retryable) {
        Map<String,Object> body = new LinkedHashMap<>();
        body.put("ok", code == null);
        body.put("data", data);
        body.put("error_code", code);
        body.put("retryable", retryable);
        body.put("source", source);
        body.put("observed_at", GrantStore.stamp(Instant.now().truncatedTo(java.time.temporal.ChronoUnit.MICROS)));
        body.put("schema_version", 1);
        return ResponseEntity.status(status).body(body);
    }
}
