package io.supportflow.sandbox;

import com.fasterxml.jackson.core.JsonFactory;
import com.fasterxml.jackson.core.StreamReadFeature;
import com.fasterxml.jackson.core.json.JsonWriteFeature;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.DateTimeException;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeFormatterBuilder;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.Set;
import java.util.TreeMap;
import java.util.regex.Pattern;

/** The private grant wire format. No transport or storage dependencies. */
record GrantCommand(String actionId, String proposalId, String tenantId, String customerId,
                    String subscriptionId, int seats, int days, long expectedBusinessVersion,
                    String policyVersion, Instant proposalExpiresAt, String argsHash,
                    String authorizationId, Instant authorizedAt) {
    private static final ObjectMapper JSON = new ObjectMapper(JsonFactory.builder()
        .enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
        .disable(JsonWriteFeature.WRITE_HEX_UPPER_CASE).build())
        .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);
    private static final Set<String> FIELDS = Set.of("action_id", "command_schema_version", "proposal_id", "tenant_id",
        "customer_id", "subscription_id", "seats", "days", "expected_business_version", "policy_version",
        "proposal_expires_at", "canonical_args_hash", "authorization_id", "authorized_at");
    private static final Pattern HASH = Pattern.compile("[0-9a-f]{64}");
    private static final Pattern AWARE = Pattern.compile(
        "([0-9]{4}-[0-9]{2}-[0-9]{2})[Tt ]([0-9]{2}:[0-9]{2})(?::([0-9]{2})(?:[.,]([0-9]{1,}))?)?([Zz]|[+-][0-9]{2}:?[0-9]{2})");
    private static final DateTimeFormatter UTC_MICROS = new DateTimeFormatterBuilder().appendInstant(6).toFormatter();

    static final class Invalid extends IllegalArgumentException {
        final int status;
        Invalid(int status) { super(status == 409 ? "IDEMPOTENCY_CONFLICT" : "VALIDATION_ERROR"); this.status = status; }
    }

    static GrantCommand decode(byte[] body) {
        if (body.length == 0 || body.length > 65536) throw new Invalid(422);
        final JsonNode value;
        try {
            // The application sends UTF-8; reject broken sequences rather than accepting replacement characters.
            var decoder = StandardCharsets.UTF_8.newDecoder();
            value = JSON.readTree(decoder.decode(java.nio.ByteBuffer.wrap(body)).toString());
        } catch (IOException invalid) {
            throw new Invalid(422);
        }
        if (value == null || !value.isObject()) throw new Invalid(422);
        var keys = new HashSet<String>();
        value.fieldNames().forEachRemaining(keys::add);
        if (!keys.equals(FIELDS)) throw new Invalid(422);
        integer(value, "command_schema_version", 1, 1);
        String hash = text(value, "canonical_args_hash");
        if (!HASH.matcher(hash).matches()) throw new Invalid(422);
        return new GrantCommand(id(value,"action_id"), id(value,"proposal_id"), id(value,"tenant_id"), id(value,"customer_id"),
            id(value,"subscription_id"), (int)integer(value,"seats",1,20), (int)integer(value,"days",1,7),
            integer(value,"expected_business_version",1,Long.MAX_VALUE), id(value,"policy_version"),
            aware(text(value,"proposal_expires_at")), hash, id(value,"authorization_id"), aware(text(value,"authorized_at")));
    }

    private static String text(JsonNode data, String key) {
        if (!data.get(key).isTextual()) throw new Invalid(422);
        return data.get(key).textValue();
    }

    private static String id(JsonNode data, String key) {
        String value = text(data, key);
        if (!validId(value)) throw new Invalid(422);
        return value;
    }

    static boolean validId(String value) {
        return value.codePointCount(0,value.length())<=128 && value.indexOf(0)==-1
            && value.codePoints().noneMatch(c -> c>=0xD800 && c<=0xDFFF)
            && !value.codePoints().allMatch(c -> Character.isWhitespace(c) || Character.isSpaceChar(c) || c==0x85);
    }

    private static long integer(JsonNode data, String key, long min, long max) {
        JsonNode value = data.get(key);
        if (!value.isIntegralNumber() || !value.canConvertToLong()) throw new Invalid(422);
        long n = value.longValue();
        if (n < min || n > max) throw new Invalid(422);
        return n;
    }

    static Instant aware(String input) {
        var match = AWARE.matcher(input);
        if (!match.matches()) throw new Invalid(422);
        try {
            String fractional = match.group(4) == null ? "" : match.group(4);
            int micros = Integer.parseInt((fractional + "000000").substring(0, 6));
            String seconds = match.group(3) == null ? "00" : match.group(3);
            var local = LocalDateTime.parse(match.group(1) + "T" + match.group(2) + ":" + seconds).withNano(micros * 1000);
            String zone = match.group(5);
            if (local.getYear() == 0) throw new Invalid(422);
            if (zone.equalsIgnoreCase("Z")) return local.toInstant(ZoneOffset.UTC);
            int hours = Integer.parseInt(zone.substring(1, 3));
            int minutes = Integer.parseInt(zone.substring(zone.length()-2));
            if (hours > 23 || minutes > 59) throw new Invalid(422);
            // Python fixed offsets are allowed up to 23:59, wider than ZoneOffset's 18h.
            int offset = (hours * 3600 + minutes * 60) * (zone.charAt(0) == '-' ? -1 : 1);
            return local.toInstant(ZoneOffset.UTC).minusSeconds(offset);
        } catch (DateTimeException invalid) {
            throw new Invalid(422);
        }
    }

    static String utc(Instant instant) {
        int year = instant.atOffset(ZoneOffset.UTC).getYear();
        if (year < 1 || year > 9999) throw new Invalid(409);
        return instant.getNano() == 0 ? instant.toString() : UTC_MICROS.format(instant);
    }

    String canonicalJson() {
        var fields = new TreeMap<String,Object>();
        fields.put("schema_version",1);
        fields.put("action_type","grant_temporary_seats");
        fields.put("command_schema_version",1);
        fields.put("tenant_id",tenantId);
        fields.put("customer_id",customerId);
        fields.put("subscription_id",subscriptionId);
        fields.put("seats",seats);
        fields.put("days",days);
        fields.put("policy_version",policyVersion);
        fields.put("business_version",expectedBusinessVersion);
        fields.put("created_at",utc(proposalExpiresAt.minusSeconds(900)));
        fields.put("expires_at",utc(proposalExpiresAt));
        try {
            return JSON.writeValueAsString(fields);
        } catch (IOException impossible) {
            throw new IllegalStateException("Canonical grant serialization failed", impossible);
        }
    }

    String recomputeHash() {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest(canonicalJson().getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException("SHA-256 missing", impossible);
        }
    }

    void validateHash() {
        if (!MessageDigest.isEqual(argsHash.getBytes(StandardCharsets.US_ASCII),
                recomputeHash().getBytes(StandardCharsets.US_ASCII))) throw new Invalid(409);
    }
}
