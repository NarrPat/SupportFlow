# DeepSeek V4.1 text tokenizer

Source: https://github.com/deepseek-ai/deepseek-recipe/tree/935817c66f5d6e64838f419692545f06787e485f/static/tokenizers/v41

Original tokenizer.json bytes, SHA-256 `81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99`. MIT license copied alongside. Chat conversion and special-token ranges use pinned official deepseek-recipe 0.2.0, not a local template approximation. Linux x86_64 is the deployed runtime; an unavailable native encoder fails closed. Windows has no published wheel at verification time.

Six synthetic API probes on 2026-11-15 matched exact prompt_tokens, including tool schema/history, JSON output and literal special-token spellings. This is finite validation, not a guarantee that a mutable provider alias never changes. Every enabled request compares returned usage when present and retains mismatches; unknown usage stays unknown. No model weights, credentials or gold are stored here.
