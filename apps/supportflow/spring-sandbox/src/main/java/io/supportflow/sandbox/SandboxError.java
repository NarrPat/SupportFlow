package io.supportflow.sandbox;

final class SandboxError extends RuntimeException {
    final int status;
    final boolean retryable;

    SandboxError(int status, String code) { this(status, code, false); }

    SandboxError(int status, String code, boolean retryable) {
        super(code);
        this.status = status;
        this.retryable = retryable;
    }

    static SandboxError conflict(String code) { return new SandboxError(409, code); }
    static SandboxError missing() { return new SandboxError(404, "NOT_FOUND"); }
    static SandboxError invalidFacts() { return new SandboxError(502, "UPSTREAM_UNAVAILABLE"); }
}
