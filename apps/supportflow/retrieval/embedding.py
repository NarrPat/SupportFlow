"""Pinned CPU embedding. Production only loads verified, pre-provisioned files."""
from functools import lru_cache
import json
import math
import os
from pathlib import Path

from .contracts import DIMENSION, MODEL_ID, MODEL_REPOSITORY, MODEL_REVISION, KnowledgeError, sha256


def validate_vectors(values, count):
    try:
        rows = list(values)
        if len(rows) != count:
            raise ValueError()
        converted = []
        for row in rows:
            if len(row) != DIMENSION or any(isinstance(x, bool) for x in row):
                raise ValueError()
            vector = [float(x) for x in row]
            if any(not math.isfinite(x) for x in vector) or sum(x*x for x in vector) == 0:
                raise ValueError()
            converted.append(vector)
        return converted
    except (TypeError, ValueError, OverflowError):
        raise KnowledgeError("INVALID_EMBEDDING") from None


def verified_directory():
    path = Path(os.environ.get("SF_EMBEDDING_MODEL_DIR", ""))
    try:
        if not path.is_absolute():
            raise ValueError()
        path = path.resolve(strict=True)
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        pins = json.loads(Path(__file__).with_name("asset_pins.json").read_text(encoding="utf-8"))
        if (manifest["model_id"] != MODEL_ID or manifest["repository"] != MODEL_REPOSITORY
                or manifest["revision"] != MODEL_REVISION or manifest["dimension"] != DIMENSION):
            raise ValueError()
        required = {"model_optimized.onnx", "tokenizer.json", "config.json", "tokenizer_config.json",
                    "special_tokens_map.json"}
        files = manifest["files"]
        if sorted(files, key=lambda item: item["path"]) != sorted(pins["files"], key=lambda item: item["path"]):
            raise ValueError()
        if not required.issubset({item["path"] for item in files}):
            raise ValueError()
        for item in files:
            target = (path / item["path"]).resolve(strict=True)
            if not target.is_relative_to(path) or target.stat().st_size != item["size"]:
                raise ValueError()
            if sha256(target.read_bytes()) != item["sha256"]:
                raise ValueError()
        return path, manifest
    except (OSError, ValueError, KeyError, TypeError):
        raise KnowledgeError("EMBEDDING_UNAVAILABLE") from None


class DocumentTokenizer:
    def __init__(self, path):
        from tokenizers import Tokenizer
        raw = Path(path).read_bytes()
        self.version = "bge-small-zh-v1.5:" + sha256(raw)
        self.tokenizer = Tokenizer.from_str(raw.decode("utf-8"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()

    def encode(self, text):
        return self.tokenizer.encode(text, add_special_tokens=False)


@lru_cache(maxsize=1)
def default_tokenizer():
    """Token budgeting does not initialize an ONNX inference session."""
    try:
        path = Path(os.environ["SF_EMBEDDING_MODEL_DIR"]) / "tokenizer.json"
        pins = json.loads(Path(__file__).with_name("asset_pins.json").read_text(encoding="utf-8"))
        expected = next(item for item in pins["files"] if item["path"] == "tokenizer.json")
        if path.stat().st_size != expected["size"] or sha256(path.read_bytes()) != expected["sha256"]:
            raise ValueError()
        return DocumentTokenizer(path)
    except (OSError, KeyError, StopIteration, ValueError):
        raise KnowledgeError("EMBEDDING_UNAVAILABLE") from None


class LocalEmbedding:
    model_id, revision, dimension, vector_origin = MODEL_ID, MODEL_REVISION, DIMENSION, "live_local"

    def __init__(self):
        from fastembed import TextEmbedding
        path, self.asset_manifest = verified_directory()
        self.tokenizer = DocumentTokenizer(path / "tokenizer.json")
        self.model = TextEmbedding(model_name=MODEL_ID, specific_model_path=str(path),
                                   local_files_only=True, threads=1, providers=["CPUExecutionProvider"])

    def passages(self, texts):
        if any(len(self.tokenizer.encode(text).ids) > 450 for text in texts):
            raise KnowledgeError("INVALID_TOKENIZATION")
        return validate_vectors(self.model.passage_embed(texts, batch_size=16, parallel=None), len(texts))

    def query(self, text):
        if not text.strip() or len(self.tokenizer.encode(text).ids) > 450:
            raise KnowledgeError("VALIDATION")
        return validate_vectors(self.model.query_embed(text), 1)[0]


@lru_cache(maxsize=1)
def default_embedding():
    try:
        return LocalEmbedding()
    except KnowledgeError:
        raise
    except (ImportError, OSError, RuntimeError, ValueError):
        raise KnowledgeError("EMBEDDING_UNAVAILABLE") from None


def require_embedding_profile(embedding):
    if embedding.vector_origin == "synthetic_vector":
        database = os.environ.get("PGDATABASE", "")
        if os.environ.get("SF_RUNTIME_PROFILE") != "eval" or not database.startswith("sf_eval_") or not database.endswith("_app"):
            raise KnowledgeError("SYNTHETIC_EMBEDDING_FORBIDDEN")
    elif embedding.vector_origin != "live_local":
        raise KnowledgeError("INVALID_EMBEDDING")
