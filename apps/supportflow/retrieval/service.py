"""One-generation retrieval and current authorization checks for durable references."""
import json
from math import isfinite
import os
import re
from time import monotonic

from psycopg.errors import LockNotAvailable, QueryCanceled
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from .contracts import REF_FIELDS, SOURCE, KnowledgeError, compact, empty_bundle, reference, sha256
from .embedding import default_embedding, default_tokenizer, require_embedding_profile, validate_vectors
from .lexical import SNAPSHOTS, bm25, lexical_version, rrf, tokenize


PHYSICAL_STRATEGIES = ("SF-VECTOR-v1", "SF-HYBRID-v1")
PHYSICAL_PHASES = ("candidate_scope_ms", "vector_search_ms", "snapshot_load_ms",
                   "bm25_ms", "fusion_ms", "fetch_serialize_ms")


def _query_is_valid(query):
    return isinstance(query, str) and bool(query.strip()) and "\0" not in query and len(query.encode("utf-8")) <= 8192


def _elapsed_ms(started):
    return (monotonic() - started) * 1000


def _valid_references(refs):
    if not isinstance(refs, list) or len(refs) > 5:
        raise KnowledgeError("INVALID_REFERENCE")
    seen = set()
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != REF_FIELDS or ref["source"] != SOURCE
                or any(not isinstance(ref[key], str) or not ref[key] or "\0" in ref[key]
                       for key in REF_FIELDS-{"generation", "start_line", "end_line"})
                or type(ref["generation"]) is not int or ref["generation"] < 1
                or type(ref["start_line"]) is not int or ref["start_line"] < 1
                or type(ref["end_line"]) is not int or ref["end_line"] < ref["start_line"]
                or not re.fullmatch("[0-9a-f]{64}", ref["content_sha256"])
                or ref["evidence_id"] in seen):
            raise KnowledgeError("INVALID_REFERENCE")
        seen.add(ref["evidence_id"])
    if len({ref["generation"] for ref in refs}) > 1:
        raise KnowledgeError("INVALID_REFERENCE")


def validate_refs(conn, run, refs, *, current_generation=True, lock=True):
    """No gate/side effects: safe inside the final run transaction after its locks.

    With lock=True, membership/state/document/explicit ACL share locks survive
    until the caller commits. Historical references still require current ACL.
    """
    _valid_references(refs)
    tenant, actor = run["tenant_id"], run["actor_id"]
    suffix = " FOR SHARE" if lock else ""
    actor_row = conn.execute("SELECT role FROM memberships WHERE tenant_id=%s AND user_id=%s" + suffix,
                             (tenant, actor)).fetchone()
    if not actor_row or actor_row["role"] not in {"support", "supervisor", "admin"}:
        raise KnowledgeError("FORBIDDEN")
    if not refs:
        return []
    state = conn.execute("SELECT active_generation FROM knowledge_state WHERE tenant_id=%s"+suffix, (tenant,)).fetchone()
    if not state or state["active_generation"] is None:
        raise KnowledgeError("INDEX_NOT_READY")
    generation = refs[0]["generation"]
    if current_generation and generation != state["active_generation"]:
        raise KnowledgeError("EVIDENCE_STALE")
    generation_row = conn.execute("SELECT * FROM index_generations WHERE tenant_id=%s AND generation=%s",
                                  (tenant, generation)).fetchone()
    if (not generation_row or generation_row["state"] not in {"active", "retired"}
            or not generation_row["vector_ready"] or not generation_row["lexical_ready"]
            or sha256(compact(generation_row["manifest"])) != generation_row["corpus_manifest_hash"]
            or not generation_row["snapshot_checksum"] or not generation_row["snapshot_path"]):
        raise KnowledgeError("INDEX_NOT_READY")
    rows = []
    for ref in refs:
        row = conn.execute("""SELECT c.*,d.visibility,d.active_version_id,v.content_sha256 AS document_sha256
            FROM chunks c JOIN documents d USING(tenant_id,document_id)
            JOIN document_versions v USING(tenant_id,document_id,version_id)
            JOIN index_vectors iv USING(tenant_id,evidence_id)
            WHERE c.tenant_id=%s AND c.evidence_id=%s AND iv.generation=%s
              AND (d.visibility='tenant' OR EXISTS(SELECT 1 FROM document_acl a
                  WHERE a.tenant_id=d.tenant_id AND a.document_id=d.document_id AND a.user_id=%s))"""
            + (" FOR SHARE OF d" if lock else ""), (tenant, ref["evidence_id"], generation, actor)).fetchone()
        if row is None:
            raise KnowledgeError("FORBIDDEN")
        if row["visibility"] == "private":
            grant = conn.execute("SELECT user_id FROM document_acl WHERE tenant_id=%s AND document_id=%s AND user_id=%s"+suffix,
                                 (tenant, row["document_id"], actor)).fetchone()
            if not grant:
                raise KnowledgeError("FORBIDDEN")
        if current_generation and row["active_version_id"] != row["version_id"]:
            raise KnowledgeError("EVIDENCE_STALE")
        if reference(row, generation) != ref or sha256(row["text"]) != row["content_sha256"]:
            raise KnowledgeError("INVALID_REFERENCE")
        expected_chunk = {key: row[key] for key in ("evidence_id", "version_id", "section_id", "content_sha256")}
        if expected_chunk not in generation_row["manifest"]["chunks"]:
            raise KnowledgeError("INDEX_NOT_READY")
        rows.append(row)
    if current_generation:
        reviewed_support(rows)
    return rows


def read_citation(conn, tenant_id, actor_id, evidence_id, *, generation=None):
    with conn.transaction():
        row = conn.execute("""SELECT c.*,g.generation FROM chunks c
            JOIN index_vectors v USING(tenant_id,evidence_id)
            JOIN index_generations g USING(tenant_id,generation)
            WHERE c.tenant_id=%s AND c.evidence_id=%s AND g.state IN ('active','retired')
              AND (%s::integer IS NULL OR g.generation=%s) ORDER BY g.generation DESC LIMIT 1""",
            (tenant_id, evidence_id, generation, generation)).fetchone()
        if row is None:
            raise KnowledgeError("NOT_FOUND")
        ref = reference(row, row["generation"])
        loaded = validate_refs(conn, {"tenant_id": tenant_id, "actor_id": actor_id}, [ref],
                               current_generation=False, lock=True)[0]
        return {**ref, "content": loaded["text"], "document_sha256": loaded["document_sha256"]}


def current_ref_visible(conn, tenant_id, actor_id, evidence_id):
    try:
        read_citation(conn, tenant_id, actor_id, evidence_id)
        return True
    except KnowledgeError:
        return False


def reviewed_support(rows):
    from apps.supportflow.knowledge.reviewed import reviewed_sections
    approved = reviewed_sections()
    approved_ids = {key[0] for key in approved}
    topics, codes = set(), set()
    for row in rows:
        # A reviewed whole section does not automatically approve a partial chunk.
        key = row["section_id"], row["content_sha256"]
        entry = approved.get(key)
        if row["section_id"] in approved_ids and not entry:
            raise KnowledgeError("EVIDENCE_CONFLICT")
        if entry and row["section_content_sha256"] == row["content_sha256"]:
            topics.update(entry["supported_topics"])
            codes.update(entry["supported_codes"])
    return sorted(topics), sorted(codes)


def supporting_refs(refs, *, required_topic="invitation_diagnosis", blocking_codes=(), unknown_codes=(), checked_topics=()):
    """Choose citations for actual claims, after bundle authorization/completeness.

    This pure helper grants no authorization and cannot replace load_refs().
    Unrelated retrieval candidates are not citations merely because they rank.
    """
    from apps.supportflow.knowledge.reviewed import reviewed_sections
    _valid_references(refs)
    approved, codes = reviewed_sections(), set(blocking_codes)
    unknown = set(unknown_codes)
    topics = {required_topic, *checked_topics}
    selected = []
    for ref in refs:
        row = approved.get((ref["section_id"], ref["content_sha256"]))
        if row and (topics.intersection(row["supported_topics"]) or codes.intersection(row["supported_codes"]) or unknown.intersection(row.get("supported_unknown_codes", []))):
            selected.append(dict(ref))
    return selected


def bundle_measurements(result, tokenizer):
    return len(compact(result)), len(tokenizer.encode(compact(result["context"]).decode("utf-8")).ids)


def bundle(rows, generation, vector_origin, tokenizer):
    result = empty_bundle(generation=generation)
    result["refs"] = [reference(row, generation) for row in rows]
    result["context"] = [{"evidence_id": row["evidence_id"], "text": row["text"]} for row in rows]
    result["supported_topics"], result["supported_codes"] = reviewed_support(rows)
    # Real vectors establish provenance, not a frozen semantic evaluation design.
    # A later experiment must independently qualify its corpus, tasks and gold.
    result["quality_eligible"] = False
    raw_bytes, projection_tokens = bundle_measurements(result, tokenizer)
    if raw_bytes > 65536 or len(compact(result["context"])) > 16000 or projection_tokens > 1500:
        raise KnowledgeError("RESULT_TOO_LARGE", details={"raw_bytes": raw_bytes,
                             "projection_tokens": projection_tokens})
    return result


class SearchService:
    def __init__(self, conn, run, gate, *, embedding=None, observe=None, cache=None):
        self.conn, self.run, self.gate = conn, run, gate
        self.embedding, self.observe, self.cache = embedding, observe, cache or SNAPSHOTS

    def _remaining(self):
        # One elapsed-time budget includes both internal attempts. Synchronous
        # ONNX work is checked on return, rather than claiming forcible CPU
        # cancellation. No late context may reach the generation model.
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise KnowledgeError("TIMEOUT")
        return remaining

    def _sql_budget(self):
        milliseconds = max(1, int(min(5.0, self._remaining()) * 1000))
        self.conn.execute("SELECT set_config('statement_timeout',%s,true)", (str(milliseconds)+"ms",))
        self.conn.execute("SELECT set_config('lock_timeout',%s,true)", (str(min(2000, milliseconds))+"ms",))

    def _progress(self, kind, payload):
        from apps.supportflow.jobs.core import append_progress
        append_progress(self.conn, self.run["run_id"], self.run["execution_generation"], kind, payload)

    def _persist(self, ordinal, query, generation, vector_ids, lexical_ids, fused_ids, refs, error,
                 *, raw_bytes=None, projection_tokens=None):
        self.gate.check("knowledge.persist")
        with self.conn.transaction():
            if error is None:
                self._sql_budget()
            self.conn.execute("""INSERT INTO run_retrievals(run_id,execution_generation,ordinal,tenant_id,index_generation,
                query_sha256,vector_ids,lexical_ids,fused_ids,refs,error_code,raw_bytes,projection_tokens)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (self.run["run_id"], self.run["execution_generation"], ordinal, self.run["tenant_id"], generation,
                 sha256(query), Jsonb(vector_ids), Jsonb(lexical_ids), Jsonb(fused_ids), Jsonb(refs), error,
                 raw_bytes if raw_bytes is None or raw_bytes <= 65536 else None, projection_tokens))
            if error is None:
                self._remaining()

    def _candidates(self, vector, embedding, *, sample=None):
        tenant, actor = self.run["tenant_id"], self.run["actor_id"]
        if sample is not None:
            scope_started = monotonic()
        with self.conn.transaction():
            self._sql_budget()
            generation = self.conn.execute("""SELECT g.* FROM knowledge_state s
                JOIN index_generations g ON g.tenant_id=s.tenant_id AND g.generation=s.active_generation
                JOIN memberships m ON m.tenant_id=s.tenant_id AND m.user_id=%s
                WHERE s.tenant_id=%s AND m.role IN ('support','supervisor','admin')""", (actor, tenant)).fetchone()
            if (not generation or generation["state"] != "active" or not generation["vector_ready"]
                    or not generation["lexical_ready"] or generation["embedding_model"] != embedding.model_id
                    or generation["embedding_revision"] != embedding.revision
                    or generation["tokenizer_version"] != embedding.tokenizer.version
                    or generation["lexical_version"] != lexical_version()
                    or generation["vector_origin"] != embedding.vector_origin):
                raise KnowledgeError("INDEX_NOT_READY")
            scope = """FROM index_vectors v JOIN chunks c USING(tenant_id,evidence_id)
                JOIN documents d USING(tenant_id,document_id)
                WHERE v.tenant_id=%s AND v.generation=%s AND d.active_version_id=c.version_id
                  AND (d.visibility='tenant' OR EXISTS(SELECT 1 FROM document_acl a
                      WHERE a.tenant_id=d.tenant_id AND a.document_id=d.document_id AND a.user_id=%s))"""
            parameters = tenant, generation["generation"], actor
            self._sql_budget()
            authorized = {row["evidence_id"] for row in self.conn.execute("SELECT c.evidence_id "+scope, parameters).fetchall()}
            if sample is not None:
                sample["generation"] = generation["generation"]
                sample["candidate_scope_ms"] = _elapsed_ms(scope_started)
                sample["authorized_candidates"] = len(authorized)
            self._sql_budget()
            if sample is not None:
                vector_started = monotonic()
            ranked = self.conn.execute("SELECT c.evidence_id,v.embedding <=> %s::vector AS distance "+scope+
                " ORDER BY distance,c.evidence_id LIMIT 20",
                (compact(vector).decode("ascii"), *parameters)).fetchall()
            if sample is not None:
                sample["vector_search_ms"] = _elapsed_ms(vector_started)
                sample["vector_candidates"] = [dict(row) for row in ranked]
        return generation, authorized, [row["evidence_id"] for row in ranked]

    def _lexical_candidates(self, query, generation_row, authorized, *, sample=None):
        if sample is not None:
            snapshot_started = monotonic()
        snapshot = self.cache.load(generation_row)
        self._remaining()
        if sample is not None:
            sample["snapshot_load_ms"] = _elapsed_ms(snapshot_started)
            lexical_started = monotonic()
        documents = [item for item in snapshot["documents"] if item["evidence_id"] in authorized]
        ranked = bm25(tokenize(query), documents)
        if sample is not None:
            sample["bm25_ms"] = _elapsed_ms(lexical_started)
            sample["bm25_scanned"] = len(documents)
            sample["lexical_candidates"] = [{"evidence_id": evidence_id, "score": score}
                                            for evidence_id, score in ranked]
        return [row[0] for row in ranked]

    def _selected_bundle(self, selected_ids, generation_row, tokenizer):
        generation = generation_row["generation"]
        with self.conn.transaction():
            self._sql_budget()
            selected = []
            for evidence_id in selected_ids:
                row = self.conn.execute("SELECT * FROM chunks WHERE tenant_id=%s AND evidence_id=%s",
                                        (self.run["tenant_id"], evidence_id)).fetchone()
                if row is None:
                    raise KnowledgeError("INDEX_NOT_READY")
                selected.append(reference(row, generation))
            rows = validate_refs(self.conn, self.run, selected, current_generation=True, lock=True)
            # Empty retrievals still need the generation race check;
            # validate_refs already obtained the membership lock first.
            state = self.conn.execute("SELECT active_generation FROM knowledge_state WHERE tenant_id=%s FOR SHARE",
                                      (self.run["tenant_id"],)).fetchone()
            if not state or state["active_generation"] != generation:
                raise KnowledgeError("EVIDENCE_STALE")
            result = bundle(rows, generation, generation_row["vector_origin"], tokenizer)
            self._remaining()
        return result

    def search_physical(self, query, *, query_vector, strategy, deadline_at=None):
        """Measure only the retrieval module in a registered disposable database."""
        database = os.environ.get("PGDATABASE", "")
        if (not re.fullmatch(r"sf_eval_capacity_[a-f0-9]{12}_app", database)
                or self.conn.info.dbname != database or not self.conn.autocommit
                or self.conn.info.transaction_status != TransactionStatus.IDLE
                or self.embedding is None or self.embedding.vector_origin != "synthetic_vector"):
            raise KnowledgeError("PHYSICAL_RETRIEVAL_FORBIDDEN")
        require_embedding_profile(self.embedding)
        if strategy not in PHYSICAL_STRATEGIES:
            raise KnowledgeError("INVALID_RETRIEVAL_STRATEGY")
        started = monotonic()
        if deadline_at is not None and (type(deadline_at) not in (int, float) or not isfinite(deadline_at)):
            raise KnowledgeError("INVALID_RETRIEVAL_DEADLINE")
        self._deadline = min(started + 8.0, deadline_at) if deadline_at is not None else started + 8.0
        attempts = []
        result = empty_bundle("VALIDATION")
        if _query_is_valid(query):
            for attempt in range(1, 3):
                sample = dict.fromkeys(PHYSICAL_PHASES)
                sample.update(generation=None, authorized_candidates=None, bm25_scanned=None,
                              vector_candidates=[], lexical_candidates=[], selected_ids=[], error_code=None)
                attempts.append(sample)
                try:
                    self._remaining()
                    vector = validate_vectors([query_vector], 1)[0]
                    generation_row, authorized, vector_ids = self._candidates(vector, self.embedding, sample=sample)
                    if strategy == "SF-HYBRID-v1":
                        lexical_ids = self._lexical_candidates(query, generation_row, authorized, sample=sample)
                        fusion_started = monotonic()
                        selected_ids = [row[0] for row in rrf(vector_ids, lexical_ids)]
                        sample["fusion_ms"] = _elapsed_ms(fusion_started)
                    else:
                        selected_ids = vector_ids[:5]
                    sample["selected_ids"] = selected_ids
                    self._remaining()
                    if self.observe is not None:
                        # The candidate transaction has ended. An isolated
                        # observer can synchronize before current authorization.
                        self.observe("after_candidates", {"generation": generation_row["generation"],
                                     "attempt": attempt, "evidence_ids": list(selected_ids)})
                        self._remaining()
                    fetch_started = monotonic()
                    result = self._selected_bundle(selected_ids, generation_row, self.embedding.tokenizer)
                    sample["fetch_serialize_ms"] = _elapsed_ms(fetch_started)
                    self._remaining()
                    break
                except (KnowledgeError, LockNotAvailable, QueryCanceled) as error:
                    sample["error_code"] = error.code if isinstance(error, KnowledgeError) else "TIMEOUT"
                    result = empty_bundle(sample["error_code"], sample["generation"])
                    if sample["error_code"] not in {"EVIDENCE_STALE", "FORBIDDEN"}:
                        break
        totals = {}
        for phase in PHYSICAL_PHASES:
            measurements = [sample[phase] for sample in attempts if sample[phase] is not None]
            totals[phase] = sum(measurements) if measurements else None
        return result, {"strategy": strategy, "attempts": attempts, **totals,
                        "retrieval_total_ms": _elapsed_ms(started)}

    def search(self, query):
        if not _query_is_valid(query):
            return empty_bundle("VALIDATION")
        self._deadline = monotonic() + 8.0
        for attempt in range(2):
            self.gate.check("knowledge.search.before")
            ordinal = self.gate.reserve_tool()
            self._progress("tool.started", {"tool_id": "search_knowledge"})
            generation, vector_ids, lexical_ids, fused_ids = None, [], [], []
            try:
                self._remaining()
                embedding = self.embedding or default_embedding()
                require_embedding_profile(embedding)
                self._remaining()
                vector = validate_vectors([embedding.query(query)], 1)[0]
                self._remaining()
                self.gate.check("knowledge.embedding.after")
                generation_row, authorized, vector_ids = self._candidates(vector, embedding)
                generation = generation_row["generation"]
                lexical_ids = self._lexical_candidates(query, generation_row, authorized)
                fused_ids = [row[0] for row in rrf(vector_ids, lexical_ids)]
                self._remaining()
                if self.observe is not None:
                    # Private injection seam. Called after the candidate tx was
                    # released; production never imports a test observer.
                    self.observe("after_candidates", {"run_id": self.run["run_id"], "generation": generation,
                                 "attempt": attempt+1, "evidence_ids": fused_ids})
                self.gate.check("knowledge.final_authorization")
                self._remaining()
                result = self._selected_bundle(fused_ids, generation_row, embedding.tokenizer)
                raw_bytes, projection_tokens = bundle_measurements(result, embedding.tokenizer)
                self._persist(ordinal, query, generation, vector_ids, lexical_ids, fused_ids, result["refs"], None,
                              raw_bytes=raw_bytes, projection_tokens=projection_tokens)
                self._progress("tool.finished", {"tool_id": "search_knowledge", "ok": True})
                return result
            except (KnowledgeError, LockNotAvailable, QueryCanceled) as error:
                if not isinstance(error, KnowledgeError):
                    error = KnowledgeError("TIMEOUT")
                self._persist(ordinal, query, generation, vector_ids, lexical_ids, fused_ids, [], error.code,
                              raw_bytes=error.details.get("raw_bytes"), projection_tokens=error.details.get("projection_tokens"))
                self._progress("tool.finished", {"tool_id": "search_knowledge", "ok": False})
                if error.code in {"EVIDENCE_STALE", "FORBIDDEN"} and attempt == 0:
                    continue
                return empty_bundle(error.code, generation)
        return empty_bundle("EVIDENCE_STALE")

    def load_refs(self, refs):
        self.gate.check("knowledge.references.before")
        try:
            with self.conn.transaction():
                rows = validate_refs(self.conn, self.run, refs, current_generation=True, lock=True)
                generation = refs[0]["generation"] if refs else None
                current = self.conn.execute("SELECT vector_origin FROM index_generations WHERE tenant_id=%s AND generation=%s",
                                            (self.run["tenant_id"], generation)).fetchone() if refs else None
                tokenizer = self.embedding.tokenizer if self.embedding else default_tokenizer()
                result = bundle(rows, generation, current["vector_origin"] if current else "synthetic_vector", tokenizer)
            self.gate.check("knowledge.references.after")
            return result
        except KnowledgeError as error:
            return empty_bundle(error.code)
