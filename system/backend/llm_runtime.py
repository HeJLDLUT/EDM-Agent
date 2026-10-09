import copy
import os
from typing import Any, Dict, Iterable, List, Optional
JSON_OBJECT_FORMAT: Dict[str, str] = {'type': 'json_object'}
STRUCTURED_OUTPUT_MODES = {'auto', 'json_schema', 'json_object', 'off'}
_RUNTIME_EXTRA_BODY_KEYS = {'enable_thinking', 'provider', 'reasoning', 'reasoning_effort', 'thinking', 'thinking_budget'}
_TRUE_VALUES = {'1', 'true', 'yes', 'y', 'on'}
_FALSE_VALUES = {'0', 'false', 'no', 'n', 'off', 'none', 'disabled'}
_INHERIT_VALUES = {'', 'auto', 'inherit', 'default'}
_TOKEN_LIMIT_PARAMS = {'auto', 'max_tokens', 'max_completion_tokens', 'both', 'none', 'off'}
_TRUNCATED_FINISH_REASONS = {'length', 'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'token_limit', 'content_filter_length'}

def _env_value(name: str) -> Optional[str]:
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip()
    return value if value else None

def _parse_bool(raw: str) -> Optional[bool]:
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    if normalized in _INHERIT_VALUES:
        return None
    return None

def _env_bool(name: str, default: bool) -> bool:
    raw = _env_value(name)
    if raw is None:
        return default
    parsed = _parse_bool(raw)
    if parsed is None:
        return default
    return parsed

def _env_bool_from_names(names: Iterable[str], default: bool) -> bool:
    for name in names:
        raw = _env_value(name)
        if raw is None:
            continue
        parsed = _parse_bool(raw)
        if parsed is not None:
            return parsed
    return default

def _env_text_from_names(names: Iterable[str], default: str) -> str:
    for name in names:
        value = _env_value(name)
        if value is not None:
            return value
    return default

def _env_int(name: str, default: Optional[int]=None) -> Optional[int]:
    raw = _env_value(name)
    if raw is None:
        return default
    if raw.strip().lower() in _INHERIT_VALUES | _FALSE_VALUES:
        return default
    return int(raw)

def _env_int_from_names(names: Iterable[str], default: Optional[int]=None) -> Optional[int]:
    for name in names:
        raw = _env_value(name)
        if raw is None:
            continue
        if raw.strip().lower() in _INHERIT_VALUES | _FALSE_VALUES:
            return default
        return int(raw)
    return default

def env_int_from_names(names: Iterable[str], default: Optional[int]=None) -> Optional[int]:
    return _env_int_from_names(names, default)

def env_float_from_names(names: Iterable[str], default: float) -> float:
    for name in names:
        raw = _env_value(name)
        if raw is None:
            continue
        if raw.strip().lower() in _INHERIT_VALUES | _FALSE_VALUES:
            return default
        return float(raw)
    return default

def _provider_env_prefixes(provider: str) -> List[str]:
    provider = provider.upper()
    prefixes = [provider]
    if provider.startswith('OPENROUTER'):
        prefixes.append('OPENROUTER')
    return list(dict.fromkeys(prefixes))

def _policy_names(provider: str, suffix: str, step: str) -> List[str]:
    prefixes = _provider_env_prefixes(provider)
    names: List[str] = []
    for prefix in prefixes:
        names.append(f'{prefix}_{suffix}_{step}')
    for prefix in prefixes:
        names.append(f'{prefix}_{suffix}')
    names.append(f'LLM_{suffix}')
    names.append(f'LLM_{suffix}_{step}')
    return names

def _merge_dicts(*parts: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    merged: Dict[str, Any] = {}
    for part in parts:
        if not part:
            continue
        for key, value in part.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = _merge_dicts(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
    return merged or None

def _strict_json_schema_format(name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    return {'type': 'json_schema', 'json_schema': {'name': name, 'schema': schema, 'strict': True}}

def _supports_strict_json_schema(provider: str, model: str) -> bool:
    normalized = model.lower()
    if provider == 'openai':
        return normalized.startswith(('gpt-4o', 'gpt-4.1', 'gpt-5', 'o1', 'o3', 'o4'))
    if provider in {'openrouter1', 'openrouter2'}:
        return any((marker in normalized for marker in ('openai/gpt-4o', 'openai/gpt-4.1', 'openai/gpt-5', 'openai/o1', 'openai/o3', 'openai/o4', 'google/gemini', 'anthropic/claude-sonnet-4', 'anthropic/claude-opus-4')))
    return False

def _supports_json_object(provider: str, model: str) -> bool:
    return provider in {'openai', 'deepseek', 'qwen', 'openrouter1', 'openrouter2', 'zhipu', 'chatai'}

def _structured_output_enabled(provider: str, step: str) -> bool:
    return _env_bool_from_names(_policy_names(provider, 'STRUCTURED_OUTPUT_ENABLED', step), True)

def _structured_output_mode(provider: str, step: str) -> str:
    raw_mode = _env_text_from_names(_policy_names(provider, 'STRUCTURED_OUTPUT_MODE', step), 'auto')
    mode = raw_mode.strip().lower().replace('-', '_')
    aliases = {'schema': 'json_schema', 'strict': 'json_schema', 'json': 'json_object', 'text': 'off', 'none': 'off', 'disabled': 'off', 'false': 'off'}
    mode = aliases.get(mode, mode)
    return mode if mode in STRUCTURED_OUTPUT_MODES else 'auto'

def _response_format(provider: str, model: str, step: str, schema_name: Optional[str], schema: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not _structured_output_enabled(provider, step):
        return None
    mode = _structured_output_mode(provider, step)
    if mode == 'off':
        return None
    strict_supported = schema_name and schema and _supports_strict_json_schema(provider, model)
    if mode in {'auto', 'json_schema'} and strict_supported:
        return _strict_json_schema_format(schema_name, schema)
    if mode == 'json_schema' and (not strict_supported):
        return copy.deepcopy(JSON_OBJECT_FORMAT) if _supports_json_object(provider, model) else None
    if mode in {'auto', 'json_object'} and _supports_json_object(provider, model):
        return copy.deepcopy(JSON_OBJECT_FORMAT)
    return None

def _is_openai_reasoning_model(model: str) -> bool:
    normalized = model.lower()
    return normalized.startswith(('gpt-5', 'o1', 'o3', 'o4'))

def _is_openrouter_reasoning_model(model: str) -> bool:
    normalized = model.lower()
    reasoning_markers = ('gpt-5', 'o1', 'o3', 'o4', 'gemini-3', 'claude-3.7', 'claude-sonnet-4', 'claude-opus-4', 'deepseek', 'grok', 'qwen', 'glm-4.7', 'glm-4.6')
    return any((marker in normalized for marker in reasoning_markers))

def _default_thinking_enabled(provider: str, step: str, model: str) -> bool:
    if provider in {'deepseek', 'qwen', 'zhipu'}:
        return step != 'STEP1'
    if provider in {'openrouter1', 'openrouter2'}:
        return _is_openrouter_reasoning_model(model)
    if provider == 'openai':
        return _is_openai_reasoning_model(model)
    if provider == 'chatai':
        return bool(_env_value('CHATAI_REASONING_EFFORT'))
    return False

def _thinking_enabled(provider: str, step: str, model: str) -> bool:
    aliases: List[str] = []
    if step != 'STEP1':
        if provider == 'qwen':
            aliases.append('QWEN_ENABLE_THINKING')
        elif provider == 'zhipu':
            aliases.append('ZHIPU_ENABLE_THINKING')
    names = _policy_names(provider, 'THINKING_ENABLED', step)
    if aliases:
        prefixes = _provider_env_prefixes(provider)
        provider_name_count = len(prefixes) * 2
        names = names[:provider_name_count] + aliases + names[provider_name_count:]
    return _env_bool_from_names(names, _default_thinking_enabled(provider, step, model))

def _thinking_body(provider: str, step: str, model: str) -> Optional[Dict[str, Any]]:
    enabled = _thinking_enabled(provider, step, model)
    if provider == 'deepseek':
        body: Dict[str, Any] = {'thinking': {'type': 'enabled' if enabled else 'disabled'}}
        if enabled:
            body['reasoning_effort'] = os.getenv('DEEPSEEK_REASONING_EFFORT', 'high')
        return body
    if provider == 'qwen':
        body = {'enable_thinking': enabled}
        budget = _env_int(f'QWEN_THINKING_BUDGET_{step}', _env_int('QWEN_THINKING_BUDGET'))
        if enabled and budget is not None:
            body['thinking_budget'] = budget
        return body
    if provider in {'openrouter1', 'openrouter2'} and _is_openrouter_reasoning_model(model):
        if not enabled:
            return {'reasoning': {'effort': 'none', 'exclude': True}}
        return {'reasoning': {'effort': os.getenv('OPENROUTER_REASONING_EFFORT', 'xhigh'), 'exclude': _env_bool('OPENROUTER_EXCLUDE_REASONING', True)}}
    if provider == 'openai' and enabled and _is_openai_reasoning_model(model):
        return {'reasoning_effort': os.getenv('OPENAI_REASONING_EFFORT', 'xhigh')}
    if provider == 'zhipu':
        return {'thinking': {'type': 'enabled' if enabled else 'disabled'}}
    if provider == 'chatai' and enabled:
        effort = os.getenv('CHATAI_REASONING_EFFORT')
        if effort:
            return {'reasoning_effort': effort}
    return None

def _default_token_limit_param(provider: str, model: str) -> str:
    if provider == 'openai' and _is_openai_reasoning_model(model):
        return 'max_completion_tokens'
    return 'max_tokens'

def _token_policy_names(provider: str, suffix: str, step: str) -> List[str]:
    prefixes = _provider_env_prefixes(provider)
    names: List[str] = []
    for prefix in prefixes:
        names.append(f'{prefix}_{suffix}_{step}')
    for prefix in prefixes:
        names.append(f'{prefix}_{suffix}')
    names.append(f'{step}_{suffix}')
    names.append(f'LLM_{suffix}_{step}')
    names.append(f'LLM_{suffix}')
    return names

def _token_limit_param(provider: str, step: str, model: str) -> str:
    raw = _env_text_from_names(_token_policy_names(provider, 'TOKEN_LIMIT_PARAM', step), _default_token_limit_param(provider, model))
    normalized = raw.strip().lower().replace('-', '_')
    return normalized if normalized in _TOKEN_LIMIT_PARAMS else _default_token_limit_param(provider, model)

def _attach_token_limits(provider: str, step: str, conf: Dict[str, Any]) -> None:
    max_tokens = _env_int_from_names(_token_policy_names(provider, 'MAX_TOKENS', step))
    max_completion_tokens = _env_int_from_names(_token_policy_names(provider, 'MAX_COMPLETION_TOKENS', step))
    max_output_tokens = _env_int_from_names(_token_policy_names(provider, 'MAX_OUTPUT_TOKENS', step))
    if max_tokens is None and max_output_tokens is not None:
        max_tokens = max_output_tokens
    if max_completion_tokens is None and max_output_tokens is not None:
        max_completion_tokens = max_output_tokens
    if max_tokens is not None and max_tokens > 0:
        conf['max_tokens'] = max_tokens
    else:
        conf.pop('max_tokens', None)
    if max_completion_tokens is not None and max_completion_tokens > 0:
        conf['max_completion_tokens'] = max_completion_tokens
    else:
        conf.pop('max_completion_tokens', None)
    conf['token_limit_param'] = _token_limit_param(provider, step, conf['model'])

def _openrouter_provider_body(response_format: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not response_format or response_format.get('type') != 'json_schema':
        return None
    return {'provider': {'require_parameters': True}}

def _clean_runtime_fields(conf: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = copy.deepcopy(conf)
    cleaned.pop('response_format', None)
    cleaned.pop('structured_output_mode', None)
    extra_body = cleaned.get('extra_body')
    if isinstance(extra_body, dict):
        for key in _RUNTIME_EXTRA_BODY_KEYS:
            extra_body.pop(key, None)
        if not extra_body:
            cleaned.pop('extra_body', None)
    return cleaned

def llm_request_options(conf: Dict[str, Any], include_response_format: bool=True, include_extra_body: bool=True) -> Dict[str, Any]:
    options: Dict[str, Any] = {}
    if include_response_format and conf.get('response_format') is not None:
        options['response_format'] = conf['response_format']
    if include_extra_body and conf.get('extra_body') is not None:
        options['extra_body'] = conf['extra_body']
    return options

def llm_token_limit_options(conf: Dict[str, Any]) -> Dict[str, int]:
    param = str(conf.get('token_limit_param') or 'max_tokens').strip().lower().replace('-', '_')
    max_tokens = conf.get('max_tokens')
    max_completion_tokens = conf.get('max_completion_tokens')
    if param in {'none', 'off'}:
        return {}
    if param == 'both':
        options: Dict[str, int] = {}
        if max_tokens is not None:
            options['max_tokens'] = int(max_tokens)
        if max_completion_tokens is not None:
            options['max_completion_tokens'] = int(max_completion_tokens)
        return options
    if param == 'max_completion_tokens':
        value = max_completion_tokens if max_completion_tokens is not None else max_tokens
        return {'max_completion_tokens': int(value)} if value is not None else {}
    value = max_tokens if max_tokens is not None else max_completion_tokens
    return {'max_tokens': int(value)} if value is not None else {}

def is_truncated_finish_reason(finish_reason: Any) -> bool:
    if finish_reason is None:
        return False
    normalized = str(finish_reason).strip().lower().replace('-', '_')
    return normalized in _TRUNCATED_FINISH_REASONS or 'max_token' in normalized

def _jsonable_usage_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable_usage_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_usage_value(v) for v in value]
    if hasattr(value, 'model_dump'):
        try:
            return _jsonable_usage_value(value.model_dump())
        except Exception:
            pass
    return str(value)

def extract_token_usage(response: Any) -> Dict[str, Any]:
    usage = getattr(response, 'usage', None)
    if usage is None:
        return {}
    if hasattr(usage, 'model_dump'):
        try:
            raw = usage.model_dump()
        except Exception:
            raw = {}
    elif isinstance(usage, dict):
        raw = dict(usage)
    else:
        raw = {}
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens', 'prompt_tokens_details', 'completion_tokens_details'):
            value = getattr(usage, key, None)
            if value is not None:
                raw[key] = value
    return {str(key): _jsonable_usage_value(value) for key, value in raw.items() if value is not None}

def _attach_runtime_defaults(provider: str, step: str, conf: Dict[str, Any], schema_name: Optional[str], schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    conf = _clean_runtime_fields(conf)
    _attach_token_limits(provider, step, conf)
    response_format = _response_format(provider, conf['model'], step, schema_name, schema)
    extra_body = _merge_dicts(conf.get('extra_body'), _thinking_body(provider, step, conf['model']), _openrouter_provider_body(response_format) if provider.startswith('openrouter') else None)
    conf['response_format'] = response_format
    conf['structured_output_mode'] = _structured_output_mode(provider, step)
    if not (response_format and response_format.get('type') == 'json_schema'):
        conf['use_native_parse'] = False
    if extra_body:
        conf['extra_body'] = extra_body
    elif 'extra_body' in conf:
        conf.pop('extra_body', None)
    return conf

def apply_llm_runtime_defaults(provider: str, step: str, conf: Dict[str, Any], schema_name: Optional[str]=None, schema: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    return _attach_runtime_defaults(provider, step.upper(), conf, schema_name, schema)

def build_llm_configs(step: str, schema_name: Optional[str]=None, schema: Optional[Dict[str, Any]]=None, openai_use_native_parse: bool=False) -> Dict[str, Dict[str, Any]]:
    step = step.upper()
    if step not in {'STEP1', 'STEP2', 'STEP3'}:
        raise ValueError(f'Unsupported LLM step: {step}')
    defaults: Dict[str, Dict[str, str]] = {'STEP1': {'openai': 'gpt-4o-2024-08-06', 'deepseek': 'deepseek-v4-flash', 'qwen': 'qwen3.6-plus', 'openrouter1': 'anthropic/claude-3-haiku', 'openrouter2': 'anthropic/claude-3-haiku', 'zhipu': 'glm-4.6'}, 'STEP2': {'openai': 'gpt-4o-mini', 'deepseek': 'deepseek-v4-pro', 'qwen': 'qwen3.6-plus', 'openrouter1': 'google/gemini-3.1-pro-preview', 'openrouter2': 'openai/gpt-5.4', 'chatai': 'openai/gpt-5-mini', 'zhipu': 'glm-4.6'}, 'STEP3': {'openai': 'gpt-4o-2024-08-06', 'deepseek': 'deepseek-v4-pro', 'qwen': 'qwen3.6-plus', 'openrouter1': 'google/gemini-3.1-pro-preview', 'openrouter2': 'openai/gpt-5.4', 'zhipu': 'glm-4.6'}}
    configs: Dict[str, Dict[str, Any]] = {'openai': {'api_key_env': 'OPENAI_API_KEY', 'base_url': os.getenv('OPENAI_BASE_URL') or None, 'model': os.getenv(f'OPENAI_MODEL_{step}', defaults[step]['openai']), 'use_native_parse': openai_use_native_parse}, 'deepseek': {'api_key_env': 'DEEPSEEK_API_KEY', 'base_url': os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com'), 'model': os.getenv(f'DEEPSEEK_MODEL_{step}', defaults[step]['deepseek']), 'use_native_parse': False}, 'qwen': {'api_key_env': 'QWEN_API_KEY', 'base_url': os.getenv('QWEN_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1'), 'model': os.getenv(f'QWEN_MODEL_{step}', defaults[step]['qwen']), 'use_native_parse': False}, 'openrouter1': {'api_key_env': 'OPENROUTER1_API_KEY', 'base_url': os.getenv('OPENROUTER1_BASE_URL', 'https://openrouter.ai/api/v1'), 'model': os.getenv(f'OPENROUTER1_MODEL_{step}', defaults[step]['openrouter1']), 'use_native_parse': False, 'extra_headers': {'HTTP-Referer': 'https://github.com/ScientificDataMining', 'X-Title': 'TDM Extraction Bot'}}, 'openrouter2': {'api_key_env': 'OPENROUTER2_API_KEY', 'base_url': os.getenv('OPENROUTER2_BASE_URL', 'https://openrouter.ai/api/v1'), 'model': os.getenv(f'OPENROUTER2_MODEL_{step}', defaults[step]['openrouter2']), 'use_native_parse': False, 'extra_headers': {'HTTP-Referer': 'https://github.com/ScientificDataMining', 'X-Title': 'TDM Extraction Bot'}}, 'zhipu': {'api_key_env': 'ZHIPU_API_KEY', 'base_url': os.getenv('ZHIPU_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4/'), 'model': os.getenv(f'ZHIPU_MODEL_{step}', defaults[step]['zhipu']), 'use_native_parse': False}}
    if step == 'STEP2':
        configs['chatai'] = {'api_key_env': 'CHATAI_API_KEY', 'base_url': os.getenv('CHATAI_BASE_URL', 'https://api.chataiapi.com/v1'), 'model': os.getenv('CHATAI_MODEL', defaults[step]['chatai']), 'use_native_parse': False, 'extra_headers': {'User-Agent': 'Apifox/1.0.0 (https://apifox.com)'}}
    for provider, conf in list(configs.items()):
        configs[provider] = _attach_runtime_defaults(provider, step, conf, schema_name, schema)
    return configs
try:
    from pipeline_control import wait_if_pause_requested
except Exception:

    def wait_if_pause_requested(context: str='', run_dir=None, poll_interval: float=0.5) -> bool:
        return False
