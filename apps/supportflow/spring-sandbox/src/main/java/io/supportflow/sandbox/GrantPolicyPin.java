package io.supportflow.sandbox;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.Objects;
import java.util.stream.Collectors;

/** Same source files are packaged by Maven and read at startup, never supplied by a caller. */
record GrantPolicyPin(JsonNode bundle) {
    static GrantPolicyPin load(ObjectMapper json) {
        try (var config = GrantPolicyPin.class.getResourceAsStream("/grant_policy_bundle.json");
             var text = new BufferedReader(new InputStreamReader(Objects.requireNonNull(
                 GrantPolicyPin.class.getResourceAsStream("/temporary-seat-policy-v1.md")), StandardCharsets.UTF_8))) {
            JsonNode pin = json.readTree(Objects.requireNonNull(config));
            String canonical = text.lines().collect(Collectors.joining("\n"));
            String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest(canonical.getBytes(StandardCharsets.UTF_8)));
            if (!hash.equals(pin.path("content_sha256").asText()) ||
                    !pin.path("policy_version").asText().equals("v1") ||
                    !pin.path("constants").path("action_type").asText().equals("grant_temporary_seats")) {
                throw new IllegalStateException("Packaged grant policy pin is inconsistent");
            }
            return new GrantPolicyPin(pin);
        } catch (IOException | NoSuchAlgorithmException error) {
            throw new IllegalStateException("Cannot read packaged grant policy pin", error);
        }
    }
}
