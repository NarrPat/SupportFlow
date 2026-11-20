package io.supportflow.sandbox;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.*;

class GrantCommandTest {
    private final ObjectMapper json = new ObjectMapper();

    @Test void matchesFrozenPythonCanonicalBytesAndHashes() throws Exception {
        try (var stream = getClass().getResourceAsStream("/contract-v1.json")) {
            var vectors = json.readTree(stream).get("command_vectors");
            assertEquals(4, vectors.size());
            for (var vector : vectors) {
                String name = vector.get("case").asText();
                var request = GrantCommand.decode(json.writeValueAsBytes(vector.get("request")));
                assertEquals(vector.get("canonical_json").asText(), request.canonicalJson(), name);
                assertEquals(vector.get("sha256").asText(), request.recomputeHash(), name);
                assertDoesNotThrow(request::validateHash, name);
            }
        }
    }

    @Test void rejectionDistinguishesShapeFromDifferentBusinessParameters() throws Exception {
        try (var stream = getClass().getResourceAsStream("/contract-v1.json")) {
            var vectors = json.readTree(stream).get("rejected_commands");
            assertEquals(10, vectors.size());
            for (var vector : vectors) {
                String name = vector.get("case").asText();
                assertEquals(vector.get("status").asInt(), assertThrows(GrantCommand.Invalid.class, () ->
                    GrantCommand.decode(json.writeValueAsBytes(vector.get("request"))).validateHash(), name).status, name);
            }
        }
    }

    @Test void duplicateKeysTrailingDocumentAndInvalidEncodingAreRejected() throws Exception {
        try (var stream = getClass().getResourceAsStream("/contract-v1.json")) {
            String command = json.readTree(stream).get("command_vectors").get(0).get("request").toString();
            for (String raw : new String[]{"{\"seats\":10," + command.substring(1), command + "{}"}) {
                assertEquals(422, assertThrows(GrantCommand.Invalid.class,
                    () -> GrantCommand.decode(raw.getBytes(StandardCharsets.UTF_8))).status);
            }
            assertEquals(422, assertThrows(GrantCommand.Invalid.class, () -> GrantCommand.decode(new byte[]{(byte)0xfe})).status);
        }
    }

    @Test void parserUsesMicrosecondsAndPreservesTheActualInstant() {
        var expected = Instant.parse("2026-11-07T00:15:00.012345Z");
        assertEquals(expected, GrantCommand.aware("2026-11-07t05:45:00,012345999+0530"));
        assertEquals(Instant.parse("2026-11-07T00:15:00Z"), GrantCommand.aware("2026-11-07 00:15z"));
    }
}
