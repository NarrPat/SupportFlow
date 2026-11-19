"""Small data contracts shared by the index builder, graph and HTTP readers."""
import hashlib
import json


SOURCE = "supportflow.knowledge"
DIMENSION = 512
CHUNKER_VERSION = "md-paragraph-v1:450:60"
MODEL_ID = "BAAI/bge-small-zh-v1.5"
MODEL_REPOSITORY = "Qdrant/bge-small-zh-v1.5"
MODEL_REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
REF_FIELDS = {"evidence_id", "document_id", "version_id", "section_id", "generation", "title", "source",
              "start_line", "end_line", "content_sha256"}


class KnowledgeError(Exception):
    """A fixed category, never an upstream response or a user document body."""
    def __init__(self, code, *, details=None):
        super().__init__(code)
        self.code, self.details = code, details or {}


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def reference(row, generation):
    return {**{key: row[key] for key in REF_FIELDS - {"generation", "source"}},
            "generation": generation, "source": SOURCE}


def empty_bundle(code=None, generation=None):
    return {"schema_version": 1, "ok": code is None, "error_code": code, "generation": generation,
            "refs": [], "context": [], "supported_topics": [], "supported_codes": [],
            "source": SOURCE, "quality_eligible": False}
