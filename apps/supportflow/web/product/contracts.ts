// Generated from app.openapi() by python -m apps.supportflow.web.generate_types.

export type ActorView = {
  "actor_id": string;
  "username": string;
  "tenant_id": string;
  "role": "support" | "supervisor" | "admin";
  "capabilities": Array<string>;
};

export type ApprovalDecisionResult = {
  "approval_id": string;
  "run_id": string;
  "status": "queued" | "running" | "awaiting_input" | "awaiting_approval" | "resume_queued" | "succeeded" | "handoff" | "rejected" | "expired" | "failed" | "cancelled" | "needs_reconciliation";
  "approval_status": "approved" | "rejected";
  "version": number;
  "decision": "approve" | "reject";
  "reason": string;
  "decided_by": string;
  "decided_at": string;
};

export type ApprovalView = {
  "approval_id": string;
  "run_id": string;
  "status": "pending" | "approved" | "rejected" | "expired" | "cancelled";
  "version": number;
  "decision": "approve" | "reject" | null;
  "reason": string | null;
  "decided_by": string | null;
  "decided_at": string | null;
  "run_status": "queued" | "running" | "awaiting_input" | "awaiting_approval" | "resume_queued" | "succeeded" | "handoff" | "rejected" | "expired" | "failed" | "cancelled" | "needs_reconciliation";
  "can_decide": boolean;
  "proposal": ProposalView;
};

export type CancelRequest = {
  "expected_version": number;
  "client_request_id"?: string | null;
};

export type CancelView = {
  "run_id": string;
  "status": "queued" | "running" | "awaiting_input" | "awaiting_approval" | "resume_queued" | "succeeded" | "handoff" | "rejected" | "expired" | "failed" | "cancelled" | "needs_reconciliation";
  "thread_id": string;
  "message": string;
  "version": number;
  "last_event_seq": number;
  "created_at": string;
  "reason_code": string | null;
  "result": CustomerRunResult | DiagnosisResult | RagDiagnosisResult | RecoveredRagDiagnosisResult | GrantExecutionResult | KnowledgeAnswerResult | CapacityDiagnosisResult | GrantPolicyReply | InputRefusalResult | null;
  "cancel_requested": boolean;
  "pending_input"?: PendingInput | null;
  "proposal"?: ProposalView | null;
  "execution_configuration"?: RunExecutionConfiguration | null;
  "external_issues"?: ExternalIssuesView | null;
  "already_terminal": boolean;
};

export type CapacityDiagnosisResult = {
  "schema_version"?: 6;
  "resolution_code": "DIAGNOSIS_COMPLETE" | "EVIDENCE_REQUIRED" | "FACTS_INCOMPLETE";
  "customer": apps__supportflow__domain__contracts__Customer | null;
  "intent"?: "capacity_diagnosis";
  "facts": Facts;
  "blocking_codes": Array<"CAPACITY_EXHAUSTED" | "PLAN_NOT_ACTIVE" | "DOMAIN_NOT_ALLOWED" | "INVITATION_EXPIRED" | "INVITATION_NOT_YET_VALID" | "INVITATION_PERMISSION_DENIED" | "SERVICE_UNAVAILABLE" | "ALREADY_JOINED" | "INVITATION_REVOKED">;
  "unknown_codes": Array<"KNOWLEDGE_EVIDENCE_MISSING" | "DOMAIN_POLICY_UNKNOWN" | "INVITATION_PERMISSION_UNKNOWN" | "INVITATION_NOT_FOUND" | "INVITATION_UNAVAILABLE" | "INVITATION_STATUS_UNKNOWN" | "SUBSCRIPTION_NOT_FOUND" | "SUBSCRIPTION_UNAVAILABLE" | "SERVICE_STATUS_UNKNOWN" | "PLAN_STATUS_UNKNOWN" | "CUSTOMER_UNAVAILABLE">;
  "evidence_refs": Array<EvidenceRef>;
  "action_receipts": Array<unknown>;
  "observation_refs": Array<ObservationRef>;
  "reply_text": string;
  "diagnosis_scope"?: "subscription_capacity_only";
};

export type CitationView = {
  "evidence_id": string;
  "document_id": string;
  "version_id": string;
  "section_id": string;
  "generation": number;
  "title": string;
  "source": "supportflow.knowledge";
  "start_line": number;
  "end_line": number;
  "content_sha256": string;
  "content": string;
  "document_sha256": string;
};

export type ClarificationAccepted = {
  "run_id": string;
  "status": "queued" | "running" | "awaiting_input" | "awaiting_approval" | "resume_queued" | "succeeded" | "handoff" | "rejected" | "expired" | "failed" | "cancelled" | "needs_reconciliation";
};

export type ClarificationSubmit = {
  "input_id": string;
  "answers": Record<string, unknown>;
  "refusal_reason"?: string | null;
  "expected_version": number;
  "client_request_id"?: string | null;
};

export type CreateRun = {
  "message": string;
  "client_request_id"?: string | null;
  "requested_action"?: "grant_temporary_seats" | null;
  "seats"?: number | null;
  "days"?: number | null;
  "invitation_email"?: string | null;
};

export type CreateThread = {
  "ticket_id": string;
  "customer_id"?: string | null;
};

export type CreateTicket = {
  "title": string;
  "customer_id"?: string | null;
};

export type Credentials = {
  "username": string;
  "password": string;
};

export type CsrfToken = {
  "csrf_token": string;
};

export type CustomerRunFacts = {
  "customer_id": string;
  "customer_code": string;
  "display_name": string;
};

export type CustomerRunResult = {
  "schema_version": 1;
  "resolution_code": string;
  "customer": CustomerRunFacts | null;
  "reply_text": string;
};

export type DecisionSubmit = {
  "decision": "approve" | "reject";
  "reason": string;
  "expected_version": number;
  "client_request_id"?: string | null;
};

export type DiagnosisResult = {
  "schema_version"?: 2;
  "resolution_code": "NO_ACTION_REQUIRED" | "EVIDENCE_REQUIRED" | "FACTS_INCOMPLETE" | "CUSTOMER_REQUIRED" | "INPUT_REQUIRED";
  "customer": apps__supportflow__domain__contracts__Customer | null;
  "intent": "subscription_status" | "invitation_diagnosis" | null;
  "facts": Facts;
  "blocking_codes": Array<"CAPACITY_EXHAUSTED" | "PLAN_NOT_ACTIVE" | "DOMAIN_NOT_ALLOWED" | "INVITATION_EXPIRED" | "INVITATION_NOT_YET_VALID" | "INVITATION_PERMISSION_DENIED" | "SERVICE_UNAVAILABLE" | "ALREADY_JOINED" | "INVITATION_REVOKED">;
  "unknown_codes": Array<"KNOWLEDGE_EVIDENCE_MISSING" | "DOMAIN_POLICY_UNKNOWN" | "INVITATION_PERMISSION_UNKNOWN" | "INVITATION_NOT_FOUND" | "INVITATION_UNAVAILABLE" | "INVITATION_STATUS_UNKNOWN" | "SUBSCRIPTION_NOT_FOUND" | "SUBSCRIPTION_UNAVAILABLE" | "SERVICE_STATUS_UNKNOWN" | "PLAN_STATUS_UNKNOWN" | "CUSTOMER_UNAVAILABLE">;
  "evidence_refs": Array<unknown>;
  "action_receipts": Array<unknown>;
  "observation_refs": Array<ObservationRef>;
  "reply_text": string;
};

export type DocumentActivationInput = {
  "expected_generation": number | null;
};

export type DocumentActivationResult = {
  "document_id": string;
  "version_id": string;
  "status": "active" | "building";
  "active_generation": number | null;
};

export type DocumentPage = {
  "items": Array<DocumentView>;
};

export type DocumentStaged = {
  "document_id": string;
  "version_id": string;
  "parse_status": "staged" | "parsed" | "failed";
  "raw_sha256": string;
  "content_sha256": string;
  "created": boolean;
};

export type DocumentView = {
  "document_id": string;
  "title": string;
  "visibility": "tenant" | "private";
  "active_version_id": string | null;
  "created_at": string;
};

export type EvaluationBaseline = {
  "arm": string;
  "model_id": string;
  "graph_version": string;
  "source_revision": string;
};

export type EvaluationFailure = {
  "kind": "product" | "infrastructure" | "fixture" | "oracle" | "runner" | "not_implemented";
  "count": number;
};

export type EvaluationMetric = {
  "label": string;
  "numerator": number | null;
  "denominator": number | null;
};

export type EvaluationReport = {
  "report_id": string;
  "title": string;
  "experiment_id": string;
  "generated_at": string;
  "scope": string;
  "experiment_url": string | null;
  "baseline": EvaluationBaseline | null;
  "metrics": Array<EvaluationMetric>;
  "failure_classes": Array<EvaluationFailure>;
};

export type EvaluationReportSummary = {
  "report_id": string;
  "title": string;
  "experiment_id": string;
  "generated_at": string;
};

export type EventPage = {
  "items": Array<PublicEvent>;
  "next_after": number;
  "has_more": boolean;
  "min_seq": number;
  "last_seq": number;
  "snapshot_url": string;
};

export type EvidenceRef = {
  "evidence_id": string;
  "document_id": string;
  "version_id": string;
  "section_id": string;
  "generation": number;
  "title": string;
  "source": "supportflow.knowledge";
  "start_line": number;
  "end_line": number;
  "content_sha256": string;
};

export type ExternalIssueClue = {
  "number": number;
  "state": "open" | "closed";
  "title": string;
  "url": string;
  "created_at": string;
  "updated_at": string;
  "closed_at": string | null;
};

export type ExternalIssuesView = {
  "source": "github";
  "trust": "external_unverified";
  "trusted_for_policy": false;
  "repository": string;
  "observed_at": string;
  "ok": boolean;
  "error_code": "NOT_FOUND" | "FORBIDDEN" | "VALIDATION" | "TIMEOUT" | "RATE_LIMIT" | "UPSTREAM_UNAVAILABLE" | "UNKNOWN_OUTCOME" | "RESULT_TOO_LARGE" | null;
  "retryable": boolean;
  "has_more": boolean;
  "issues": Array<ExternalIssueClue>;
};

export type Facts = {
  "as_of": string;
  "subscription": SubscriptionFacts | null;
  "invitation": InvitationFacts | null;
  "service_status": ServiceStatus | null;
};

export type GrantExecutionResult = {
  "schema_version"?: 4;
  "resolution_code": string;
  "customer": apps__supportflow__domain__contracts__Customer | null;
  "effect_status": "none" | "unknown" | "committed";
  "action_id": string;
  "action_receipts": Array<InternalGrantReceipt>;
  "evidence_refs": Array<EvidenceRef>;
  "verification": GrantVerification | null;
  "reply_text": string;
};

export type GrantPolicyReply = {
  "schema_version"?: 7;
  "resolution_code"?: "CAPACITY_NOT_EXHAUSTED";
  "customer": apps__supportflow__domain__contracts__Customer | null;
  "intent"?: "grant_temporary_seats";
  "facts": Facts;
  "blocking_codes": Array<"CAPACITY_EXHAUSTED" | "PLAN_NOT_ACTIVE" | "DOMAIN_NOT_ALLOWED" | "INVITATION_EXPIRED" | "INVITATION_NOT_YET_VALID" | "INVITATION_PERMISSION_DENIED" | "SERVICE_UNAVAILABLE" | "ALREADY_JOINED" | "INVITATION_REVOKED">;
  "unknown_codes": Array<"KNOWLEDGE_EVIDENCE_MISSING" | "DOMAIN_POLICY_UNKNOWN" | "INVITATION_PERMISSION_UNKNOWN" | "INVITATION_NOT_FOUND" | "INVITATION_UNAVAILABLE" | "INVITATION_STATUS_UNKNOWN" | "SUBSCRIPTION_NOT_FOUND" | "SUBSCRIPTION_UNAVAILABLE" | "SERVICE_STATUS_UNKNOWN" | "PLAN_STATUS_UNKNOWN" | "CUSTOMER_UNAVAILABLE">;
  "evidence_refs": Array<EvidenceRef>;
  "action_receipts": Array<unknown>;
  "observation_refs": Array<ObservationRef>;
  "reply_text": string;
  "requested_seats": number;
  "requested_days": number;
};

export type GrantVerification = {
  "status": "verified_current" | "verified_historical" | "mismatch" | "pending";
  "subscription": Subscription | null;
  "observed_at": string | null;
};

export type InputRefusalResult = {
  "schema_version"?: 9;
  "resolution_code"?: "INPUT_DECLINED";
  "customer"?: null;
  "evidence_refs": Array<EvidenceRef>;
  "observation_refs"?: Array<unknown>;
  "action_receipts"?: Array<unknown>;
  "reply_text"?: string;
};

export type InternalGrantReceipt = {
  "action_id": string;
  "tenant_id": string;
  "customer_id": string;
  "subscription_id": string;
  "canonical_args_hash": string;
  "before_version": number;
  "after_version": number;
  "committed_business_at": string;
  "seats": number;
  "days": number;
  "temporary_expires_at": string;
  "policy_version": string;
  "proposal_id": string;
  "authorization_id": string;
  "authorized_at": string;
};

export type Invitation = {
  "invitation_id": string;
  "email": string;
  "email_domain": string;
  "created_at": string;
  "expires_at": string;
  "status": string;
  "reason_code": string | null;
};

export type InvitationFacts = {
  "invitation_id": string;
  "email": string;
  "email_domain": string;
  "created_at": string;
  "expires_at": string;
  "status": string;
  "reason_code": string | null;
  "history"?: Array<Invitation>;
  "within_validity": boolean;
  "domain_allowed": boolean | null;
  "invitation_permission": boolean | null;
};

export type KnowledgeAnswerResult = {
  "schema_version"?: 5;
  "resolution_code": "KNOWLEDGE_ANSWERED" | "EVIDENCE_REQUIRED" | "FACTS_INCOMPLETE";
  "customer": apps__supportflow__domain__contracts__Customer | null;
  "intent"?: "knowledge_question";
  "knowledge_topic": "seat_capacity" | "temporary_seats_policy" | "invitation_diagnosis" | "invitation_roles" | "document_search" | "domain_policy" | "invitation_lifecycle" | "service_health_rules" | "service_unknown" | "knowledge_governance" | "unsupported";
  "answer_mode"?: "verified_excerpt";
  "evidence_refs": Array<EvidenceRef>;
  "action_receipts": Array<unknown>;
  "observation_refs": Array<ObservationRef>;
  "reply_text": string;
};

export type ObservationRef = {
  "run_id": string;
  "execution_generation": number;
  "ordinal": number;
  "tool_id": "get_customer" | "get_subscription" | "get_invitation" | "get_service_status" | "list_external_issues";
};

export type Page_ApprovalView_ = {
  "items": Array<ApprovalView>;
  "limit": number;
  "offset": number;
  "has_more": boolean;
};

export type Page_Customer_ = {
  "items": Array<apps__supportflow__api__objects__Customer>;
  "limit": number;
  "offset": number;
  "has_more": boolean;
};

export type Page_EvaluationReportSummary_ = {
  "items": Array<EvaluationReportSummary>;
  "limit": number;
  "offset": number;
  "has_more": boolean;
};

export type Page_RunSummary_ = {
  "items": Array<RunSummary>;
  "limit": number;
  "offset": number;
  "has_more": boolean;
};

export type Page_RunView_ = {
  "items": Array<RunView>;
  "limit": number;
  "offset": number;
  "has_more": boolean;
};

export type Page_Ticket_ = {
  "items": Array<Ticket>;
  "limit": number;
  "offset": number;
  "has_more": boolean;
};

export type PendingInput = {
  "input_id": string;
  "version": number;
  "prompt": string;
  "required_fields": Array<"customer_id" | "invitation_email" | "seats" | "days">;
  "expires_at": string;
};

export type ProposalObservationRef = {
  "run_id": string;
  "execution_generation": number;
  "ordinal": number;
  "tool_id": "get_customer" | "get_subscription" | "get_invitation" | "get_service_status";
};

export type ProposalView = {
  "proposal_id": string;
  "action_id": string;
  "approval_id"?: string | null;
  "proposal_seq": number;
  "schema_version": 1;
  "action_type": "grant_temporary_seats";
  "command_schema_version": 1;
  "customer_id": string;
  "subscription_id": string;
  "seats": number;
  "days": number;
  "canonical_args_hash": string;
  "policy_version": string;
  "business_version": number;
  "created_at": string;
  "expires_at": string;
  "evidence_refs": Array<EvidenceRef>;
  "observation_refs": Array<ProposalObservationRef>;
};

export type PublicEvent = {
  "schema_version": number;
  "app"?: "supportflow";
  "run_id": string;
  "seq": number;
  "type": string;
  "status_version": number;
  "spec_version"?: null;
  "occurred_at": string;
  "payload": Record<string, unknown>;
};

export type RagDiagnosisResult = {
  "schema_version"?: 3;
  "resolution_code": "NO_ACTION_REQUIRED" | "EVIDENCE_REQUIRED" | "FACTS_INCOMPLETE" | "CUSTOMER_REQUIRED" | "INPUT_REQUIRED" | "DIAGNOSIS_COMPLETE";
  "customer": apps__supportflow__domain__contracts__Customer | null;
  "intent": "subscription_status" | "invitation_diagnosis" | null;
  "facts": Facts;
  "blocking_codes": Array<"CAPACITY_EXHAUSTED" | "PLAN_NOT_ACTIVE" | "DOMAIN_NOT_ALLOWED" | "INVITATION_EXPIRED" | "INVITATION_NOT_YET_VALID" | "INVITATION_PERMISSION_DENIED" | "SERVICE_UNAVAILABLE" | "ALREADY_JOINED" | "INVITATION_REVOKED">;
  "unknown_codes": Array<"KNOWLEDGE_EVIDENCE_MISSING" | "DOMAIN_POLICY_UNKNOWN" | "INVITATION_PERMISSION_UNKNOWN" | "INVITATION_NOT_FOUND" | "INVITATION_UNAVAILABLE" | "INVITATION_STATUS_UNKNOWN" | "SUBSCRIPTION_NOT_FOUND" | "SUBSCRIPTION_UNAVAILABLE" | "SERVICE_STATUS_UNKNOWN" | "PLAN_STATUS_UNKNOWN" | "CUSTOMER_UNAVAILABLE">;
  "evidence_refs": Array<EvidenceRef>;
  "action_receipts": Array<unknown>;
  "observation_refs": Array<ObservationRef>;
  "reply_text": string;
};

export type ReadRecovery = {
  "contract_version": 1;
  "execution_generation": number;
  "recovery_count": number;
  "tool_rounds": number;
};

export type RecoveredRagDiagnosisResult = {
  "schema_version"?: 8;
  "resolution_code": "NO_ACTION_REQUIRED" | "EVIDENCE_REQUIRED" | "FACTS_INCOMPLETE" | "CUSTOMER_REQUIRED" | "INPUT_REQUIRED" | "DIAGNOSIS_COMPLETE";
  "customer": apps__supportflow__domain__contracts__Customer | null;
  "intent": "subscription_status" | "invitation_diagnosis" | null;
  "facts": Facts;
  "blocking_codes": Array<"CAPACITY_EXHAUSTED" | "PLAN_NOT_ACTIVE" | "DOMAIN_NOT_ALLOWED" | "INVITATION_EXPIRED" | "INVITATION_NOT_YET_VALID" | "INVITATION_PERMISSION_DENIED" | "SERVICE_UNAVAILABLE" | "ALREADY_JOINED" | "INVITATION_REVOKED">;
  "unknown_codes": Array<"KNOWLEDGE_EVIDENCE_MISSING" | "DOMAIN_POLICY_UNKNOWN" | "INVITATION_PERMISSION_UNKNOWN" | "INVITATION_NOT_FOUND" | "INVITATION_UNAVAILABLE" | "INVITATION_STATUS_UNKNOWN" | "SUBSCRIPTION_NOT_FOUND" | "SUBSCRIPTION_UNAVAILABLE" | "SERVICE_STATUS_UNKNOWN" | "PLAN_STATUS_UNKNOWN" | "CUSTOMER_UNAVAILABLE">;
  "evidence_refs": Array<EvidenceRef>;
  "action_receipts": Array<unknown>;
  "observation_refs": Array<RecoveryObservationRef>;
  "reply_text": string;
  "recovery": ReadRecovery;
};

export type RecoveryObservationRef = {
  "run_id": string;
  "execution_generation": number;
  "ordinal": number;
  "tool_id": "get_customer" | "get_subscription" | "get_invitation" | "get_service_status";
};

export type RunAccepted = {
  "run_id": string;
  "status": "queued" | "running" | "awaiting_input" | "awaiting_approval" | "resume_queued" | "succeeded" | "handoff" | "rejected" | "expired" | "failed" | "cancelled" | "needs_reconciliation";
};

export type RunExecutionConfiguration = {
  "graph_version"?: string | null;
  "model_mode"?: "live" | "scripted" | null;
  "model_id"?: string | null;
  "max_output_tokens"?: number | null;
  "tool_round_limit"?: number | null;
  "budget_ms": number;
};

export type RunSummary = {
  "run_id": string;
  "status": "queued" | "running" | "awaiting_input" | "awaiting_approval" | "resume_queued" | "succeeded" | "handoff" | "rejected" | "expired" | "failed" | "cancelled" | "needs_reconciliation";
  "thread_id": string;
  "ticket_id": string;
  "ticket_title": string;
  "created_at": string;
};

export type RunView = {
  "run_id": string;
  "status": "queued" | "running" | "awaiting_input" | "awaiting_approval" | "resume_queued" | "succeeded" | "handoff" | "rejected" | "expired" | "failed" | "cancelled" | "needs_reconciliation";
  "thread_id": string;
  "message": string;
  "version": number;
  "last_event_seq": number;
  "created_at": string;
  "reason_code": string | null;
  "result": CustomerRunResult | DiagnosisResult | RagDiagnosisResult | RecoveredRagDiagnosisResult | GrantExecutionResult | KnowledgeAnswerResult | CapacityDiagnosisResult | GrantPolicyReply | InputRefusalResult | null;
  "cancel_requested"?: boolean;
  "pending_input"?: PendingInput | null;
  "proposal"?: ProposalView | null;
  "execution_configuration"?: RunExecutionConfiguration | null;
  "external_issues"?: ExternalIssuesView | null;
};

export type ServiceStatus = {
  "component": "invitations";
  "status": string;
  "updated_at": string;
};

export type Subscription = {
  "subscription_id": string;
  "base_seats": number;
  "used_seats": number;
  "temporary_seats": number;
  "temporary_expires_at": string | null;
  "plan_status": string;
  "business_version": number;
};

export type SubscriptionFacts = {
  "subscription_id": string;
  "base_seats": number;
  "used_seats": number;
  "temporary_seats": number;
  "temporary_expires_at": string | null;
  "plan_status": string;
  "business_version": number;
  "subscription_active": boolean;
  "effective_temporary": number;
  "capacity": number;
  "remaining": number;
  "over_capacity": boolean;
};

export type Thread = {
  "thread_id": string;
  "ticket_id": string;
  "customer_id": string | null;
};

export type Ticket = {
  "ticket_id": string;
  "title": string;
  "status": string;
  "customer_id": string | null;
  "assignee_id": string;
  "version": number;
};

export type TicketDetail = {
  "ticket_id": string;
  "title": string;
  "status": string;
  "customer_id": string | null;
  "assignee_id": string;
  "version": number;
  "latest_thread_id": string | null;
};

export type VersionPage = {
  "items": Array<VersionView>;
  "active_generation": number | null;
};

export type VersionView = {
  "version_id": string;
  "raw_sha256": string;
  "content_sha256": string;
  "parse_status": "staged" | "parsed" | "failed";
  "corpus_origin": "uploaded" | "synthetic_business";
  "created_at": string;
  "index_state": "staged" | "building" | "ready" | "active" | "retired" | "failed" | "interrupted";
  "index_generation": number | null;
  "failure_code": string | null;
};

export type apps__supportflow__api__objects__Customer = {
  "customer_id": string;
  "customer_code": string;
  "display_name": string;
};

export type apps__supportflow__domain__contracts__Customer = {
  "customer_id": string;
  "customer_code": string;
  "display_name": string;
};

export type Actor = ActorView;
export type LoginInput = Credentials;
export type LoginResult = CsrfToken;
export type CustomersPage = Page_Customer_;
export type TicketsPage = Page_Ticket_;
export type TicketInput = CreateTicket;
export type ThreadInput = CreateThread;
export type RunInput = CreateRun;
export type RunsPage = Page_RunView_;
export type RunDetail = RunView;
export type RunsIndexPage = Page_RunSummary_;
export type CancelInput = CancelRequest;
export type CancelResult = CancelView;
export type ApprovalsPage = Page_ApprovalView_;
export type ApprovalDetail = ApprovalView;
export type ApprovalDecisionSubmit = DecisionSubmit;
export type RunEventsPage = EventPage;
export type Citation = CitationView;
export type DocumentsPage = DocumentPage;
export type DocumentVersionsPage = VersionPage;
export type EvaluationReportsPage = Page_EvaluationReportSummary_;
