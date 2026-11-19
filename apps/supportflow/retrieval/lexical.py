"""Persisted term frequencies, authorized BM25 and rank-based fusion."""
from collections import Counter, OrderedDict
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
from uuid import uuid4

from .contracts import KnowledgeError, compact, sha256


@lru_cache(maxsize=1)
def segmenter():
    import jieba
    dictionary = Path(jieba.__file__).with_name("dict.txt")
    tokenizer = jieba.Tokenizer(str(dictionary))
    tokenizer.initialize()
    return tokenizer, "jieba-0.42.1:" + sha256(dictionary.read_bytes()) + ":bm25-k1=1.5-b=0.75"


def lexical_version():
    return segmenter()[1]


def tokenize(text):
    return [part.lower() for part in segmenter()[0].cut(text, HMM=False) if re.search(r"[\w\u3400-\u9fff]", part)]


def term_document(evidence_id, text):
    terms = Counter(tokenize(text))
    return {"evidence_id": evidence_id, "length": sum(terms.values()), "terms": dict(sorted(terms.items()))}


def bm25(query_tokens, documents, *, limit=20):
    """The caller supplies only currently authorized documents of one generation."""
    if not documents:
        return []
    query_terms = set(query_tokens)
    n = len(documents)
    average = sum(doc["length"] for doc in documents) / n or 1
    df = {term: sum(term in doc["terms"] for doc in documents) for term in query_terms}
    scores = []
    for doc in documents:
        score = 0.0
        for term in query_terms:
            frequency = doc["terms"].get(term, 0)
            if frequency:
                idf = math.log(1 + (n-df[term]+0.5)/(df[term]+0.5))
                score += idf * frequency * 2.5 / (frequency + 1.5*(0.25+0.75*doc["length"]/average))
        if score > 0:
            scores.append((doc["evidence_id"], score))
    return sorted(scores, key=lambda row: (-row[1], row[0]))[:limit]


def rrf(vector_ids, lexical_ids, *, limit=5):
    scores = {}
    for ranking in (vector_ids, lexical_ids):
        if len(set(ranking)) != len(ranking):
            raise KnowledgeError("INVALID_RANKING")
        for rank, evidence_id in enumerate(ranking, 1):
            scores[evidence_id] = scores.get(evidence_id, 0.0) + 1/(60+rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]


def snapshot_root():
    return Path(os.environ.get("SF_KB_SNAPSHOT_DIR", "/data/knowledge/indexes")).resolve()


def write_snapshot(snapshot):
    root = snapshot_root()
    relative = sha256(snapshot["tenant_id"])[:32] + "/" + str(snapshot["generation"]) + ".json"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = compact(snapshot)
    temporary = target.with_suffix("." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return relative, sha256(raw)


class SnapshotCache:
    """At most active plus one predecessor per tenant; no body text is cached."""
    def __init__(self):
        self.entries = {}

    def load(self, generation):
        tenant, number, checksum = (generation[key] for key in ("tenant_id", "generation", "snapshot_checksum"))
        cache = self.entries.setdefault(tenant, OrderedDict())
        key = number, checksum
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        try:
            root = snapshot_root()
            target = (root / generation["snapshot_path"]).resolve(strict=True)
            if not target.is_relative_to(root) or target.stat().st_size > 64*1024*1024:
                raise ValueError()
            raw = target.read_bytes()
            if sha256(raw) != checksum:
                raise ValueError()
            data = json.loads(raw)
            if (data["schema_version"] != 1 or data["tenant_id"] != tenant or data["generation"] != number
                    or any(data[name] != generation[name] for name in
                           ("corpus_manifest_hash", "tokenizer_version", "chunker_version", "lexical_version"))):
                raise ValueError()
            documents = data["documents"]
            if len({item["evidence_id"] for item in documents}) != len(documents):
                raise ValueError()
            if "manifest" in generation and sorted(item["evidence_id"] for item in documents) != sorted(
                    item["evidence_id"] for item in generation["manifest"]["chunks"]):
                raise ValueError()
            for doc in documents:
                if (type(doc["length"]) is not int or doc["length"] < 0 or
                    any(type(count) is not int or count <= 0 for count in doc["terms"].values()) or
                    sum(doc["terms"].values()) != doc["length"]):
                    raise ValueError()
        except (OSError, KeyError, TypeError, ValueError):
            raise KnowledgeError("INDEX_NOT_READY") from None
        cache[key] = data
        while len(cache) > 2:
            cache.popitem(last=False)
        return data


SNAPSHOTS = SnapshotCache()
