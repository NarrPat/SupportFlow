"""Download the pinned public ONNX assets, verify each file, run a tiny CPU probe.

Invoke inside the registered runtime window. No provider key is needed; product
workers load these assets offline. This is an environment/model-interface Spike,
not an SF-R quality experiment. Preserves failed output for a new batch to retry.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "deploy/supportflow/embedding-model.lock.json"


def verify(path, expected):
    data = path.read_bytes()
    if len(data) != expected["size"]:
        raise ValueError("Asset size mismatch: " + expected["path"])
    digest = hashlib.sha256(data).hexdigest()
    if "sha256" in expected:
        if digest != expected["sha256"]:
            raise ValueError("Asset SHA256 mismatch: " + expected["path"])
    else:
        git_digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if git_digest != expected["git_blob_sha1"]:
            raise ValueError("Asset git object mismatch: " + expected["path"])
    return {"path": expected["path"], "size": len(data), "sha256": digest}


def download(directory, model, deadline):
    files = []
    for expected in model["files"]:
        name = expected["path"]
        if Path(name).name != name or name.startswith("."):
            raise ValueError("Unsafe locked asset path")
        target = directory / name
        if not target.exists():
            url = f"https://huggingface.co/{model['repository']}/resolve/{model['revision']}/{name}"
            partial = target.with_suffix(target.suffix + ".part")
            for attempt in range(2):
                try:
                    request = urllib.request.Request(url, headers={"User-Agent": "SupportFlow-pinned-model-setup/1"})
                    with urllib.request.urlopen(request, timeout=30) as response, partial.open("wb") as handle:
                        total = 0
                        while chunk := response.read(1024 * 1024):
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Asset download deadline")
                            total += len(chunk)
                            if total > expected["size"]:
                                raise ValueError("Asset exceeds locked size")
                            handle.write(chunk)
                    verify(partial, expected)
                    partial.replace(target)
                    break
                except (OSError, TimeoutError):
                    if attempt or time.monotonic() >= deadline:
                        raise
        files.append(verify(target, expected))
        print(f"Verified model asset: {name}", flush=True)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assets-only", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a new output path")
    model = json.loads(LOCK.read_text(encoding="utf-8"))
    directory = ROOT / ".cache/models/bge-small-zh-v1.5" / model["revision"]
    directory.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "model_id": model["model_id"],
              "repository": model["repository"], "revision": model["revision"],
              "dimension": model["dimension"], "model_lock_sha256": hashlib.sha256(LOCK.read_bytes()).hexdigest(),
              "quality_experiment": False, "generation_model_calls": 0, "status": "error"}
    try:
        files = download(directory, model, start + 600)
        manifest = {"schema_version": 1, "model_id": model["model_id"], "repository": model["repository"],
                    "revision": model["revision"], "dimension": model["dimension"], "files": files}
        destination = directory / "manifest.json"
        if destination.exists():
            if json.loads(destination.read_text(encoding="utf-8")) != manifest:
                raise ValueError("Existing asset manifest disagrees")
        else:
            with destination.open("x", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        report["files"] = files
        if not args.assets_only:
            from fastembed import TextEmbedding
            from tokenizers import Tokenizer
            tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
            tokenizer.no_truncation()
            embedding = TextEmbedding(model_name=model["model_id"], specific_model_path=str(directory),
                                      local_files_only=True, providers=["CPUExecutionProvider"], threads=1)
            texts = ["邀请失败，订阅已用席位达到容量上限。", "查看成员管理权限与邀请有效期。"]
            vectors = [row.tolist() for row in embedding.embed(texts, batch_size=2)]
            assert len(vectors) == 2 and all(len(row) == model["dimension"] for row in vectors)
            assert all(type(value) is float and math.isfinite(value) for row in vectors for value in row)
            assert vectors[0] != vectors[1]
            report["cpu_probe"] = {"input_count": 2, "vector_count": len(vectors), "finite": True,
                "distinct_vectors": True, "vectors": vectors, "token_counts": [len(tokenizer.encode(text).ids) for text in texts],
                "vector_sha256": [hashlib.sha256(json.dumps(row, separators=(",", ":")).encode()).hexdigest() for row in vectors],
                "usage": None, "usage_source": "local model; provider billing not applicable"}
        report["status"] = "passed"
    except Exception as error:
        report["error_type"] = type(error).__name__
        raise
    finally:
        report["duration_seconds"] = time.monotonic() - start
        report["ended_at"] = datetime.now(timezone.utc).isoformat()
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")


if __name__ == "__main__":
    main()
