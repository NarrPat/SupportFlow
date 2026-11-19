"""Pinned DeepSeek V4.1 chat encoding, including protocol-generated tokens.

No character ratio, arbitrary overhead or fallback tokenizer is permitted.
The optional native dependency is available on the Linux deployment target.
"""
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

TOKENIZER_SHA256 = '81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99'
TOKENIZER = Path(__file__).with_name('assets') / 'deepseek-v41-tokenizer.json'
LIMIT = 8192
VERSION = 'deepseek-v41-chat-0.2.0'


class TokenCounterUnavailable(ValueError):
    pass


@lru_cache(maxsize=1)
def _encoder():
    try:
        if version('deepseek-recipe') != '0.2.0':
            raise ValueError('Unregistered encoder version')
        data = TOKENIZER.read_bytes()
        if sha256(data).hexdigest() != TOKENIZER_SHA256:
            raise ValueError('Tokenizer digest mismatch')
        from deepseek_recipe import DeepseekV41Encoding, Tokenizer
        return DeepseekV41Encoding().with_tokenizer(Tokenizer.from_str(data.decode('utf8')))
    except (ImportError, OSError, ValueError) as error:
        raise TokenCounterUnavailable('Pinned token encoder unavailable') from error


def count_request(payload):
    """Count the entire supported request; unknown shapes fail closed."""
    try:
        if payload.get('model') != 'deepseek-flash' or payload.get('thinking') != {'type': 'disabled'}:
            raise ValueError('Unsupported model or thinking mode')
        allowed = {'model','messages','thinking','max_tokens','stream','stream_options','temperature',
                   'tools','tool_choice','response_format'}
        if set(payload) - allowed:
            raise ValueError('Unregistered request fields')
        if not isinstance(payload.get('messages'), list) or not payload['messages']:
            raise ValueError('Messages required')
        for message in payload['messages']:
            if not isinstance(message.get('content'), (str, type(None))):
                raise ValueError('Only text messages calibrated')
        from deepseek_recipe import ChatCompletionRequest, ConversionOptions, ModelVersion
        options = ConversionOptions().with_model_alias('deepseek-flash', ModelVersion.DeepseekV41Flash)
        converted = ChatCompletionRequest(payload).convert(options)
        return len(_encoder().encode(converted.conversation))
    except (ImportError, OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        raise TokenCounterUnavailable('Request cannot be counted with pinned encoder') from error


def preflight(payload):
    count = count_request(payload)
    return {'version': VERSION, 'tokenizer_sha256': TOKENIZER_SHA256,
            'input_tokens': count, 'limit': LIMIT, 'allowed': count <= LIMIT}
