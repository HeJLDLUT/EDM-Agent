import os
import json
import glob
import time
import copy
import concurrent.futures
import re
import uuid
import threading
import argparse
import traceback
from typing import Annotated, List, Dict, Any, Tuple, Optional, Type, Union, get_args, get_origin
from dotenv import load_dotenv
load_dotenv(override=True)
from openai import OpenAI
from pydantic import BaseModel, ValidationError
from tqdm import tqdm
import jsonpatch
import tdm_logger
from pydantic_step2 import DataPoint, Experiment, ExtractionResult
from pydantic_step3_patch import AuditReport, AuditStats, PatchAction, IssueTypeEnum
from llm_runtime import build_llm_configs, env_float_from_names, env_int_from_names, extract_token_usage, is_truncated_finish_reason, llm_request_options, llm_token_limit_options, wait_if_pause_requested
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_DATA_DIR = os.environ.get('TDM_DATA_DIR') or os.path.join(_PROJECT_ROOT, 'data')
INPUT_MD_DIR = os.path.join(_DATA_DIR, '05_cleaned_markdown')
INPUT_JSON_DIR = os.path.join(_DATA_DIR, '06_extracted_json')
OUTPUT_DB_DIR = os.path.join(_DATA_DIR, '07_audited_db')
OUTPUT_QUARANTINE_DIR = os.path.join(_DATA_DIR, '07_quarantine')
OUTPUT_LOG_DIR = os.path.join(_DATA_DIR, '07_audit_logs')
DEFAULT_FEEDBACK_FILE = os.path.join(OUTPUT_LOG_DIR, 'step7_retry_feedback_out.json')
PROMPT_FILE = os.path.join(_SCRIPT_DIR, 'PROMPT_STEP3_PATCH.md')
for d in [OUTPUT_DB_DIR, OUTPUT_QUARANTINE_DIR, OUTPUT_LOG_DIR]:
    os.makedirs(d, exist_ok=True)
MAX_WORKERS = int(os.getenv('STEP3_MAX_WORKERS', '2'))
MAX_RETRIES = env_int_from_names(('STEP3_MAX_RETRIES', 'LLM_MAX_RETRIES'), 3)
AUDIT_LOGIC_RETRIES = env_int_from_names(('STEP3_AUDIT_LOGIC_RETRIES', 'LLM_AUDIT_LOGIC_RETRIES'), 2)
MAX_CONTINUATIONS = env_int_from_names(('STEP3_MAX_CONTINUATIONS', 'LLM_MAX_CONTINUATIONS'), 10)
SELECTED_PROVIDER = os.getenv('STEP3_LLM_PROVIDER', 'openrouter1').lower()
TEMPERATURE = float(os.getenv('STEP3_TEMPERATURE', '0.0'))
_audit_schema = AuditReport.model_json_schema()
LLM_CONFIGS = build_llm_configs('STEP3', 'audit_report', _audit_schema, openai_use_native_parse=True)
CURRENT_CONF = LLM_CONFIGS[SELECTED_PROVIDER]
PENALTY_WEIGHTS = {'hallucination': 10.0, 'logic_conflict': 8.0, 'missing_data': 6.0, 'value_error': 5.0, 'unit_mismatch': 4.0, 'exactness_violation': 4.0, 'src_id_error': 1.0, 'format_error': 0.5}
LARGE_MISSING_DATA_RETRY_MIN_COUNT = env_int_from_names(('STEP3_LARGE_MISSING_DATA_RETRY_MIN_COUNT',), 20)
LARGE_MISSING_DATA_RETRY_MIN_RATIO = env_float_from_names(('STEP3_LARGE_MISSING_DATA_RETRY_MIN_RATIO',), 0.5)

def _unwrap_annotation(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        return _unwrap_annotation(args[0]) if args else annotation
    if origin is Union:
        non_none_args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_none_args) == 1:
            return _unwrap_annotation(non_none_args[0])
    return annotation

def _annotation_allows_none(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        return _annotation_allows_none(args[0]) if args else False
    if origin is Union:
        return any((arg is type(None) for arg in get_args(annotation)))
    return False

def _extract_nested_model(annotation: Any) -> Optional[Type[BaseModel]]:
    resolved = _unwrap_annotation(annotation)
    origin = get_origin(resolved)
    if origin in (list, List):
        args = get_args(resolved)
        if not args:
            return None
        item_type = _unwrap_annotation(args[0])
        if isinstance(item_type, type) and issubclass(item_type, BaseModel):
            return item_type
        return None
    if isinstance(resolved, type) and issubclass(resolved, BaseModel):
        return resolved
    return None

def _annotation_is_list(annotation: Any) -> bool:
    resolved = _unwrap_annotation(annotation)
    return get_origin(resolved) in (list, List)

def _collect_field_aliases(field_name: str, field_info: Any) -> set:
    aliases = {field_name}
    alias = getattr(field_info, 'alias', None)
    if isinstance(alias, str) and alias:
        aliases.add(alias)
    validation_alias = getattr(field_info, 'validation_alias', None)
    if isinstance(validation_alias, str) and validation_alias:
        aliases.add(validation_alias)
    alias_choices = getattr(validation_alias, 'choices', None)
    if alias_choices:
        for choice in alias_choices:
            if isinstance(choice, str) and choice:
                aliases.add(choice)
            else:
                path = getattr(choice, 'path', None)
                if isinstance(path, (list, tuple)) and len(path) == 1 and isinstance(path[0], str):
                    aliases.add(path[0])
    alias_path = getattr(validation_alias, 'path', None)
    if isinstance(alias_path, (list, tuple)) and len(alias_path) == 1 and isinstance(alias_path[0], str):
        aliases.add(alias_path[0])
    return aliases

def _resolve_model_field(model_cls: Type[BaseModel], segment: str) -> Tuple[Optional[str], Any]:
    for field_name, field_info in model_cls.model_fields.items():
        if segment in _collect_field_aliases(field_name, field_info):
            return (field_name, field_info)
    return (None, None)

def _infer_target_model_from_node(target_node: Any) -> Optional[Type[BaseModel]]:
    if isinstance(target_node, dict):
        if 'setup' in target_node and 'data' in target_node:
            return Experiment
        if 'result' in target_node:
            return DataPoint
    return None

def _collect_non_nullable_bool_defaults(model_cls: Type[BaseModel], prefix: str='') -> Dict[str, bool]:
    collected: Dict[str, bool] = {}
    for field_name, field_info in model_cls.model_fields.items():
        field_path = f'{prefix}.{field_name}' if prefix else field_name
        annotation = field_info.annotation
        if _unwrap_annotation(annotation) is bool and (not _annotation_allows_none(annotation)):
            default_value = field_info.get_default(call_default_factory=True)
            collected[field_path] = default_value if isinstance(default_value, bool) else False
            continue
        nested_model = _extract_nested_model(annotation)
        if nested_model is not None:
            collected.update(_collect_non_nullable_bool_defaults(nested_model, field_path))
    return collected
NON_NULL_BOOL_FIELD_DEFAULTS: Dict[Type[BaseModel], Dict[str, bool]] = {ExtractionResult: _collect_non_nullable_bool_defaults(ExtractionResult), Experiment: _collect_non_nullable_bool_defaults(Experiment), DataPoint: _collect_non_nullable_bool_defaults(DataPoint)}

class PatchExecutor:

    def __init__(self, original_data: Dict):
        self.original_data = original_data
        self.fail_reason = None
        self.fail_context = None

    def _fuzzy_match(self, actual: Any, expected: Any) -> bool:
        if expected is None:
            return True
        if actual == expected:
            return True
        try:
            f_act = float(actual)
            f_exp = float(expected)
            if abs(f_act - f_exp) < 1e-09:
                return True
        except (ValueError, TypeError):
            pass
        try:
            return str(actual).strip().lower() == str(expected).strip().lower()
        except:
            return False

    def _escape_json_pointer(self, key: str) -> str:
        return str(key).replace('~', '~0').replace('/', '~1')

    def _find_pointer_and_node(self, root: Any, target_id: str, current_path: str='') -> Tuple[Optional[str], Any]:
        if isinstance(root, dict):
            if root.get('_id') == target_id:
                return (current_path, root)
            for k, v in root.items():
                escaped_k = self._escape_json_pointer(k)
                pointer, node = self._find_pointer_and_node(v, target_id, f'{current_path}/{escaped_k}')
                if pointer is not None:
                    return (pointer, node)
        elif isinstance(root, list):
            for i, item in enumerate(root):
                pointer, node = self._find_pointer_and_node(item, target_id, f'{current_path}/{i}')
                if pointer is not None:
                    return (pointer, node)
        return (None, None)

    def _resolve_target_path(self, root: Any, p: PatchAction) -> Tuple[str, Any]:
        if p.target_id:
            base_pointer, node_ref = self._find_pointer_and_node(root, p.target_id)
            if base_pointer is None:
                raise KeyError(f"Target ID '{p.target_id}' not found in current document state.")
            if p.target_field:
                if not p.target_field:
                    raise ValueError('target_field cannot be empty for MODIFY/ADD_ITEM.')
                segments = p.target_field.split('.')
                escaped_segments = []
                for seg in segments:
                    if not seg.strip():
                        raise ValueError(f"Invalid target_field '{p.target_field}': contains empty segments.")
                    if seg.isdigit():
                        raise ValueError(f"Invalid target_field '{p.target_field}': numeric index '{seg}' forbidden. Use target_id.")
                    escaped_segments.append(self._escape_json_pointer(seg))
                full_pointer = base_pointer + '/' + '/'.join(escaped_segments)
                current_val = node_ref
                try:
                    for seg in segments:
                        if isinstance(current_val, dict):
                            current_val = current_val.get(seg)
                        elif isinstance(current_val, list):
                            current_val = current_val[int(seg)]
                        else:
                            current_val = None
                except (ValueError, IndexError, AttributeError):
                    current_val = None
                return (full_pointer, current_val)
            return (base_pointer, node_ref)
        if p.target_path:
            if not p.target_path:
                raise ValueError('target_path cannot be empty.')
            segments = p.target_path.split('.')
            escaped_segments = []
            for seg in segments:
                if not seg.strip():
                    raise ValueError(f"Invalid target_path '{p.target_path}': contains empty segments.")
                if seg.isdigit():
                    raise ValueError(f"Invalid target_path '{p.target_path}': numeric index '{seg}' forbidden. Use target_id.")
                escaped_segments.append(self._escape_json_pointer(seg))
            full_pointer = '/' + '/'.join(escaped_segments)
            try:
                current_val = root
                for seg in segments:
                    current_val = current_val[seg] if isinstance(current_val, dict) else current_val[int(seg)]
            except:
                current_val = None
            return (full_pointer, current_val)
        raise ValueError('Patch must have target_id or target_path')

    def _ensure_parent_path(self, root: Dict, full_pointer: str):
        parts = full_pointer.strip('/').split('/')
        current = root
        for i, part in enumerate(parts[:-1]):
            part_key = part.replace('~1', '/').replace('~0', '~')
            if isinstance(current, list):
                try:
                    idx = int(part_key)
                    current = current[idx]
                except (IndexError, ValueError):
                    return
            elif isinstance(current, dict):
                if part_key not in current:
                    current[part_key] = {}
                current = current[part_key]
            else:
                return

    def _infer_patch_target_model(self, root: Dict[str, Any], patch: PatchAction) -> Optional[Type[BaseModel]]:
        if patch.target_id:
            _, target_node = self._find_pointer_and_node(root, patch.target_id)
            return _infer_target_model_from_node(target_node)
        if patch.target_path:
            return ExtractionResult
        return None

    def _resolve_schema_field_path(self, model_cls: Type[BaseModel], field_path: str) -> Optional[List[Dict[str, Any]]]:
        segments = field_path.split('.')
        current_model = model_cls
        resolved_segments: List[Dict[str, Any]] = []
        for idx, segment in enumerate(segments):
            field_name, field_info = _resolve_model_field(current_model, segment)
            if field_name is None:
                return None
            annotation = field_info.annotation
            is_list = _annotation_is_list(annotation)
            nested_model = _extract_nested_model(annotation)
            resolved_segments.append({'segment': segment, 'field_name': field_name, 'allows_none': _annotation_allows_none(annotation), 'is_list': is_list, 'nested_model': nested_model, 'is_object_model': nested_model is not None and (not is_list)})
            if idx < len(segments) - 1:
                if not resolved_segments[-1]['is_object_model']:
                    return None
                current_model = nested_model
        return resolved_segments

    def _normalize_add_item_list_target(self, root: Dict[str, Any], patch: PatchAction, target_pointer: str, current_val: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
        if isinstance(current_val, list):
            return (current_val, None)
        target_model = self._infer_patch_target_model(root, patch)
        target_field_path = patch.target_field if patch.target_id else patch.target_path
        if target_model is None or not target_field_path:
            return (current_val, None)
        resolved_path = self._resolve_schema_field_path(target_model, target_field_path)
        if not resolved_path or not resolved_path[-1]['is_list']:
            return (current_val, None)
        if patch.target_id:
            base_pointer, container = self._find_pointer_and_node(root, patch.target_id)
            if base_pointer is None:
                raise KeyError(f"Target ID '{patch.target_id}' not found in current document state.")
        else:
            base_pointer, container = ('', root)
        current_container = container
        current_pointer = base_pointer
        initialized_parents: List[str] = []
        for segment_info in resolved_path[:-1]:
            field_name = segment_info['field_name']
            next_pointer = f'{current_pointer}/{self._escape_json_pointer(field_name)}' if current_pointer else f'/{self._escape_json_pointer(field_name)}'
            if not isinstance(current_container, dict):
                raise TypeError(f"ADD_ITEM failed: Parent at '{current_pointer or '/'}' is not an object (got {type(current_container).__name__}) while preparing '{target_pointer}'.")
            key_exists = field_name in current_container
            next_val = current_container.get(field_name)
            if next_val is None:
                if not segment_info['allows_none']:
                    if key_exists:
                        raise TypeError(f"ADD_ITEM failed: Parent at '{next_pointer}' is None, but schema expects an existing object before appending to '{target_pointer}'.")
                    raise TypeError(f"ADD_ITEM failed: Missing required parent object '{next_pointer}' before appending to '{target_pointer}'.")
                current_container[field_name] = {}
                next_val = current_container[field_name]
                initialized_parents.append(next_pointer)
            elif not isinstance(next_val, dict):
                raise TypeError(f"ADD_ITEM failed: Parent at '{next_pointer}' is not an object (got {type(next_val).__name__}) while preparing '{target_pointer}'.")
            current_container = next_val
            current_pointer = next_pointer
        final_segment = resolved_path[-1]
        final_field = final_segment['field_name']
        final_pointer = f'{current_pointer}/{self._escape_json_pointer(final_field)}' if current_pointer else f'/{self._escape_json_pointer(final_field)}'
        if not isinstance(current_container, dict):
            raise TypeError(f"ADD_ITEM failed: Parent at '{current_pointer or '/'}' is not an object (got {type(current_container).__name__}) while preparing '{target_pointer}'.")
        key_exists = final_field in current_container
        final_val = current_container.get(final_field)
        if final_val is None:
            current_container[final_field] = []
            return (current_container[final_field], {'target_pointer': final_pointer, 'schema_path': target_field_path, 'target_model': target_model.__name__, 'normalized_from': 'NoneType' if key_exists else 'missing', 'initialized_parents': initialized_parents})
        return (final_val, None)

    def apply_patches(self, patches: List[PatchAction]) -> Optional[Dict]:
        if not patches:
            return copy.deepcopy(self.original_data)
        sandbox = copy.deepcopy(self.original_data)
        priority_map = {'MODIFY': 0, 'ADD_ITEM': 1, 'DELETE': 2}
        sorted_patches = sorted(patches, key=lambda x: priority_map.get(x.action_type, 3))
        current_runtime_context = None
        try:
            for i, p in enumerate(sorted_patches):
                target_pointer, current_val = self._resolve_target_path(sandbox, p)
                current_runtime_context = {'target_id': p.target_id, 'target_path': p.target_path, 'target_field': p.target_field, 'target_pointer': target_pointer, 'current_value_type': type(current_val).__name__}
                if p.action_type == 'MODIFY':
                    self._ensure_parent_path(sandbox, target_pointer)
                if p.original_value_context is not None:
                    if not self._fuzzy_match(current_val, p.original_value_context):
                        raise ValueError(f"Safety Check Failed for Patch ({p.action_type} on {p.target_id or p.target_path}): Expected '{p.original_value_context}', found '{current_val}'.")
                op = None
                if p.action_type == 'MODIFY':
                    if current_val is None:
                        op = {'op': 'add', 'path': target_pointer, 'value': p.new_value}
                    else:
                        if p.original_value_context is None:
                            raise ValueError(f"Overwrite Protection: Target '{target_pointer}' exists (value: {current_val}), but patch implies ADD (original_value_context=null). Aborting to prevent accidental data loss.")
                        op = {'op': 'replace', 'path': target_pointer, 'value': p.new_value}
                elif p.action_type == 'ADD_ITEM':
                    current_val, normalization_context = self._normalize_add_item_list_target(sandbox, p, target_pointer, current_val)
                    current_runtime_context['current_value_type'] = type(current_val).__name__
                    if normalization_context:
                        current_runtime_context['list_normalization'] = normalization_context
                    if not isinstance(current_val, list):
                        raise TypeError(f"ADD_ITEM failed: Target at '{target_pointer}' is not a list (got {type(current_val).__name__}).")
                    if not target_pointer.endswith('/'):
                        target_pointer += '/-'
                    else:
                        target_pointer += '-'
                    if isinstance(p.new_value, dict) and '_id' not in p.new_value:
                        p.new_value['_id'] = uuid.uuid4().hex[:8]
                    op = {'op': 'add', 'path': target_pointer, 'value': p.new_value}
                elif p.action_type == 'DELETE':
                    op = {'op': 'remove', 'path': target_pointer}
                try:
                    sandbox = jsonpatch.apply_patch(sandbox, [op])
                except jsonpatch.JsonPatchException as jpe:
                    raise RuntimeError(f'JSON Patch Library Error: {jpe}')
            return sandbox
        except Exception as e:
            self.fail_reason = str(e)
            self.fail_context = current_runtime_context
            tdm_logger.error(f'❌ Patch Transaction Aborted: {e}')
            current_patch_info = sorted_patches[i].model_dump() if hasattr(sorted_patches[i], 'model_dump') else str(sorted_patches[i])
            tdm_logger.log_event('patch_transaction_failed', {'error': str(e), 'failed_patch_index': i, 'failed_patch_type': sorted_patches[i].action_type, 'runtime_context': current_runtime_context}, severity='ERROR')
            debug_dump = {'error': str(e), 'traceback': traceback.format_exc(), 'failed_patch': current_patch_info, 'runtime_context': current_runtime_context, 'original_data_snapshot': self.original_data}
            tdm_logger.save_artifact('patch_failure_dump.json', debug_dump)
            return None

class AuditAgent:

    def __init__(self, feedback_output_path: str=None, targets_path: str=None):
        api_key = os.getenv(CURRENT_CONF['api_key_env'])
        if not api_key:
            raise ValueError(f"Missing API Key for provider '{SELECTED_PROVIDER}'. Check .env for {CURRENT_CONF['api_key_env']}.")
        default_headers = CURRENT_CONF.get('extra_headers', None)
        tdm_logger.info(f"🔌 Initializing Audit Agent: Provider={SELECTED_PROVIDER.upper()}, Model={CURRENT_CONF['model']}")
        self.llm_timeout = env_float_from_names(('STEP3_LLM_TIMEOUT', 'LLM_TIMEOUT'), 180.0)
        self.max_continuations = max(0, int(MAX_CONTINUATIONS or 0))
        self.token_limit_options = llm_token_limit_options(CURRENT_CONF)
        tdm_logger.info(f"Step 7 LLM runtime: timeout={self.llm_timeout}s, retries={MAX_RETRIES}, audit_logic_retries={AUDIT_LOGIC_RETRIES}, max_continuations={self.max_continuations}, token_limit={self.token_limit_options or 'provider_default'}")
        self.client = OpenAI(api_key=api_key, base_url=CURRENT_CONF['base_url'], default_headers=default_headers, timeout=self.llm_timeout)
        self.system_prompt = self._load_file(PROMPT_FILE)
        self.timeout_skipped = []
        self.skip_lock = threading.Lock()
        self.rejected_map = {}
        self.lock = threading.Lock()
        self.feedback_output_path = feedback_output_path or DEFAULT_FEEDBACK_FILE
        self.targets_path = targets_path

    def _load_file(self, path: str) -> str:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def _extract_message_content(self, message: Any) -> Optional[str]:
        content = getattr(message, 'content', None)
        if content:
            return content
        reasoning_content = getattr(message, 'reasoning', None)
        if reasoning_content and '```json' in reasoning_content:
            matches = re.findall('```json\\s*(\\{.*?\\})\\s*```', reasoning_content, re.DOTALL)
            if matches:
                return matches[-1]
        return content

    def _merge_audit_reports(self, reports: List[AuditReport]) -> AuditReport:
        if not reports:
            raise ValueError('No audit reports to merge')
        if len(reports) == 1:
            return reports[0]
        patches: List[PatchAction] = []
        seen = set()
        comments: List[str] = []
        quality_scores: List[int] = []
        critical_issue_types = {'hallucination', 'logic_conflict', 'missing_data', 'value_error', 'unit_mismatch', 'exactness_violation'}
        for report in reports:
            quality_scores.append(report.stats.quality_score)
            comment = report.comments.strip()
            if comment and comment not in comments:
                comments.append(comment)
            for patch in report.patches:
                payload = patch.model_dump(mode='json')
                key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
                if key in seen:
                    continue
                seen.add(key)
                patches.append(patch)
        critical_count = 0
        minor_count = 0
        for patch in patches:
            issue_type = patch.issue_type.value if hasattr(patch.issue_type, 'value') else str(patch.issue_type)
            if issue_type in critical_issue_types:
                critical_count += 1
            else:
                minor_count += 1
        stats = AuditStats(quality_score=min(quality_scores) if quality_scores else 100, critical_issues_count=critical_count, minor_issues_count=minor_count)
        return AuditReport(stats=stats, comments=' | '.join(comments) if comments else reports[-1].comments, patches=patches)

    def _audit_continuation_prompt(self, merged_patch_count: int) -> str:
        return f'SYSTEM NOTICE: Your previous audit JSON was truncated by the output token limit.\nWe have already parsed and saved {merged_patch_count} patch objects from your prior output.\nContinue auditing the SAME draft and source text. Output ONLY additional remaining patches that were not already listed.\nDo not repeat prior patches. Return one valid JSON object matching the AuditReport schema with stats, comments, and patches.'

    def _request_audit_report(self, filename: str, messages: List[Dict[str, str]], attempt: int, logic_attempt: int) -> AuditReport:
        working_messages = copy.deepcopy(messages)
        reports: List[AuditReport] = []
        continuations_used = 0
        batch_step = 0
        pending_raw_chunks: List[str] = []
        pending_chunk_continuation = False
        while True:
            batch_step += 1
            completion = self.client.chat.completions.create(model=CURRENT_CONF['model'], messages=working_messages, temperature=TEMPERATURE, **self.token_limit_options, **llm_request_options(CURRENT_CONF))
            wait_if_pause_requested(f'after Step 7 audit LLM response for {filename}, batch {batch_step}')
            if not completion.choices:
                raise ValueError('Audit API returned valid object but zero choices')
            choice = completion.choices[0]
            content = self._extract_message_content(choice.message)
            finish_reason = choice.finish_reason
            usage = extract_token_usage(completion)
            if not content:
                raise ValueError(f'Empty response from LLM (Reason: {finish_reason})')
            tdm_logger.log_event('llm_response', {'attempt': attempt, 'logic_attempt': logic_attempt, 'batch': batch_step, 'finish_reason': finish_reason, 'content_len': len(content), 'usage': usage})
            tdm_logger.save_artifact(f'{filename}_audit_raw_resp_try{attempt}_logic{logic_attempt}_batch{batch_step}.txt', content)
            is_truncated = is_truncated_finish_reason(finish_reason)
            pending_raw_chunks.append(content)
            parse_content = ''.join(pending_raw_chunks) if pending_chunk_continuation or len(pending_raw_chunks) > 1 else content
            cleaned_content = parse_content.replace('```json', '').replace('```', '').strip()
            try:
                report = AuditReport.model_validate_json(cleaned_content)
            except Exception:
                if not is_truncated:
                    raise
                if continuations_used >= self.max_continuations:
                    tdm_logger.save_artifact(f'{filename}_audit_partial_unparsed_try{attempt}_logic{logic_attempt}.txt', ''.join(pending_raw_chunks))
                    raise ValueError(f'Audit continuation limit reached before truncated JSON became parseable; captured {sum((len(chunk) for chunk in pending_raw_chunks))} raw chars.')
                continuations_used += 1
                pending_chunk_continuation = True
                tdm_logger.warning(f'Audit JSON truncated before it was parseable; requesting exact JSON continuation {continuations_used}/{self.max_continuations} for {filename}.')
                working_messages.append({'role': 'assistant', 'content': content})
                working_messages.append({'role': 'user', 'content': 'SYSTEM NOTICE: Your previous audit JSON was cut off by the output token limit.\nContinue from the exact character where it stopped. Do not restart, do not repeat already emitted JSON, and close the JSON only after all remaining audit content is complete.'})
                continue
            reports.append(report)
            pending_raw_chunks = []
            pending_chunk_continuation = False
            if not is_truncated:
                return self._merge_audit_reports(reports)
            if continuations_used >= self.max_continuations:
                merged = self._merge_audit_reports(reports)
                tdm_logger.save_artifact(f'{filename}_audit_partial_truncated_try{attempt}_logic{logic_attempt}.json', merged.model_dump(mode='json'))
                raise ValueError(f'Audit continuation limit reached before the response completed; captured {len(merged.patches)} partial patches.')
            continuations_used += 1
            tdm_logger.warning(f'Audit output truncated ({finish_reason}); requesting continuation {continuations_used}/{self.max_continuations} for {filename}.')
            working_messages.append({'role': 'assistant', 'content': content})
            working_messages.append({'role': 'user', 'content': self._audit_continuation_prompt(len(self._merge_audit_reports(reports).patches))})

    def _unique_feedback_terms(self, terms: List[str]) -> List[str]:
        seen = set()
        result = []
        for term in terms:
            cleaned = ' '.join(str(term).split())
            key = cleaned.lower()
            if cleaned and key not in seen:
                seen.add(key)
                result.append(cleaned)
        return result

    def _feedback_issue_from_patch(self, patch: PatchAction) -> Dict[str, Any]:
        payload = patch.model_dump(mode='json') if hasattr(patch, 'model_dump') else dict(patch)
        reason = str(payload.get('reason') or '')
        new_value = payload.get('new_value')
        result = new_value.get('result') if isinstance(new_value, dict) and isinstance(new_value.get('result'), dict) else {}
        table_refs = self._unique_feedback_terms(re.findall('\\bTable\\s+[A-Za-z0-9_.-]+\\b', reason, flags=re.I))
        row_refs = self._unique_feedback_terms(re.findall('\\b(?:row|run|entry|condition|sample)\\s*[:#]?\\s*[A-Za-z0-9_.-]+\\b', reason, flags=re.I))
        metric_refs = self._unique_feedback_terms(re.findall('\\b(?:kp|kobs|k_obs|hl|half[- ]life|t1/2|dt50|qy|quantum yield|k_[A-Za-z0-9]+)\\b', reason, flags=re.I))
        result_cat = result.get('cat')
        result_param = result.get('param_name')
        if result_param:
            metric_refs = self._unique_feedback_terms([*metric_refs, str(result_param)])
        elif result_cat:
            metric_refs = self._unique_feedback_terms([*metric_refs, str(result_cat)])
        return {'issue_type': payload.get('issue_type'), 'action_type': payload.get('action_type'), 'target_field': payload.get('target_field'), 'table_refs': table_refs, 'row_refs': row_refs, 'parameter_refs': metric_refs, 'result_summary': {'cat': result_cat, 'param_name': result_param, 'value': result.get('val'), 'exponent': result.get('e'), 'val_kind': result.get('val_kind'), 'val_text': result.get('val_text'), 'unit': result.get('u'), 'src': result.get('src')}, 'reason': reason}

    def _generate_feedback_str(self, patches: List[PatchAction]) -> Dict[str, Any]:
        summary = []
        issues: Dict[str, List[str]] = {}
        structured_issues = []
        for p in patches:
            itype = p.issue_type.value if hasattr(p.issue_type, 'value') else str(p.issue_type)
            issues.setdefault(itype, []).append(p.reason)
            structured_issues.append(self._feedback_issue_from_patch(p))
        if 'hallucination' in issues:
            summary.append(f"CRITICAL: You hallucinated {len(issues['hallucination'])} data points that do not exist in the text.")
            summary.append(f"Example: {issues['hallucination'][0]}")
        if 'logic_conflict' in issues:
            summary.append(f"LOGIC: Found {len(issues['logic_conflict'])} logical grouping errors.")
            summary.append(f"Example: {issues['logic_conflict'][0]}")
        if 'value_error' in issues:
            summary.append(f"ACCURACY: Found {len(issues['value_error'])} numerical inaccuracies.")
            summary.append(f"Example: {issues['value_error'][0]}")
        if 'unit_mismatch' in issues:
            summary.append(f"UNIT: Found {len(issues['unit_mismatch'])} unit errors.")
            summary.append(f"Example: {issues['unit_mismatch'][0]}")
        if 'missing_data' in issues:
            summary.append(f"RECALL: You missed {len(issues['missing_data'])} valid data rows present in the text/tables.")
            summary.append(f"Example: {issues['missing_data'][0]}")
        if 'exactness_violation' in issues:
            summary.append(f"PRECISION: Found {len(issues['exactness_violation'])} exactness violations (e.g. converting '<0.1' to '0.1').")
            summary.append(f"Example: {issues['exactness_violation'][0]}")
        return {'summary': summary, 'issue_counts': {key: len(value) for key, value in issues.items()}, 'issues': structured_issues[:120], 'truncated_issue_count': max(0, len(structured_issues) - 120), 'instruction': 'On retry, explicitly re-extract the listed missing/incorrect table rows and metrics. For missing table rows, enumerate the concrete rows/conditions rather than responding with a generic summary.'}

    def _record_retry_feedback(self, filename: str, feedback: Any):
        with self.lock:
            self.rejected_map[filename] = feedback

    def _build_recovery_feedback(self, status: str, detail: Optional[str]=None) -> str:
        base_msg = 'Audit stage could not validate or repair this extraction deterministically. Re-extract this document with the stronger model and prioritize exact source IDs, schema compliance, enums, units, and grouping.'
        if status == 'audit_request_retry_input_error':
            extra = 'The audit inputs were missing, unreadable, or structurally corrupted before auditing could begin.'
        elif status == 'audit_failed_empty_draft':
            extra = 'Step 6 produced an empty extraction draft, and the audit model failed to reconstruct it safely.'
        elif status == 'audit_failed_llm_error':
            extra = 'The audit LLM request failed or timed out before a stable patch set was produced.'
        elif status == 'audit_failed_schema_corruption':
            extra = 'The audit-generated patches still produced schema-invalid JSON.'
        elif status == 'audit_failed_patch_transaction':
            extra = 'The audit-generated patches could not be applied safely to the current draft.'
        else:
            extra = 'A recoverable audit-stage error occurred.'
        if detail:
            compact_detail = ' '.join(str(detail).split())
            return f'{base_msg} {extra} Last error: {compact_detail[:600]}'
        return f'{base_msg} {extra}'

    def _quarantine_input_failure(self, filename: str, status: str, detail: str, wrapper: Optional[Dict[str, Any]]=None, raw_payload: Optional[Any]=None, duration: float=0.0):
        if raw_payload is not None:
            tdm_logger.save_artifact(f'{filename}_audit_input_failure.txt', {'status': status, 'detail': detail, 'raw_payload': raw_payload})
        base_wrapper = copy.deepcopy(wrapper) if isinstance(wrapper, dict) else {}
        if not isinstance(base_wrapper.get('metadata'), dict):
            base_wrapper['metadata'] = {}
        metadata = base_wrapper['metadata']
        metadata.setdefault('file', filename)
        metadata['step3_input_error'] = detail
        extraction = None
        if isinstance(wrapper, dict):
            extraction = wrapper.get('extraction')
        self._record_retry_feedback(filename, self._build_recovery_feedback(status, detail))
        tdm_logger.log_event('audit_input_error', {'status': status, 'detail': detail}, severity='ERROR')
        tdm_logger.log_event('audit_scored', {'total_penalty': 0.0, 'breakdown': {'input_error': {'count': 1, 'score': 0.0}}, 'decision': status, 'patch_count': 0}, severity='ERROR')
        self._save_results(filename, base_wrapper, extraction, None, status, duration, quarantine=True)
        tdm_logger.error(f'🔴 {filename} | Status: {status} | Detail: {detail}')

    def _validate_extraction_result(self, data: Dict[str, Any], valid_ids: set) -> ExtractionResult:
        return ExtractionResult.model_validate(data, context={'valid_ids': valid_ids})

    def _infer_patch_target_model(self, draft: Dict[str, Any], patch: PatchAction, resolver: PatchExecutor) -> Optional[Type[BaseModel]]:
        if patch.target_id:
            _, target_node = resolver._find_pointer_and_node(draft, patch.target_id)
            return _infer_target_model_from_node(target_node)
        if patch.target_path:
            return ExtractionResult
        return None

    def _repair_bool_null_patches(self, draft: Dict[str, Any], patches: List[PatchAction]) -> List[Dict[str, Any]]:
        repairs: List[Dict[str, Any]] = []
        resolver = PatchExecutor(draft)
        for patch in patches:
            if patch.action_type != 'MODIFY' or patch.new_value is not None:
                continue
            target_model = self._infer_patch_target_model(draft, patch, resolver)
            if target_model is None:
                continue
            target_field_path = patch.target_field if patch.target_id else patch.target_path
            if not target_field_path:
                continue
            bool_defaults = NON_NULL_BOOL_FIELD_DEFAULTS.get(target_model, {})
            if target_field_path not in bool_defaults:
                continue
            try:
                _, current_val = resolver._resolve_target_path(draft, patch)
            except Exception:
                current_val = None
            if isinstance(current_val, bool):
                repaired_value = current_val
                repair_source = 'pre_patch_value'
            elif isinstance(patch.original_value_context, bool):
                repaired_value = patch.original_value_context
                repair_source = 'original_value_context'
            else:
                repaired_value = bool_defaults[target_field_path]
                repair_source = 'schema_default'
            patch.new_value = repaired_value
            repairs.append({'action_type': patch.action_type, 'target_id': patch.target_id, 'target_path': patch.target_path, 'target_field': target_field_path, 'repaired_to': repaired_value, 'repair_source': repair_source})
        return repairs

    def _prevalidate_patched_result(self, filename: str, draft: Dict[str, Any], patched_data: Dict[str, Any], patches: List[PatchAction], valid_ids: set) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        try:
            validated_obj = self._validate_extraction_result(patched_data, valid_ids)
            return (validated_obj.model_dump(by_alias=True), [])
        except ValidationError:
            repair_details = self._repair_bool_null_patches(draft, patches)
            if not repair_details:
                raise
        tdm_logger.warning(f'Local schema guard repaired {len(repair_details)} bool->None patch(es) for {filename}; re-validating.')
        tdm_logger.log_event('local_patch_schema_repair', {'count': len(repair_details), 'repairs': repair_details}, severity='WARNING')
        repaired_executor = PatchExecutor(draft)
        repaired_data = repaired_executor.apply_patches(patches)
        if repaired_data is None:
            raise RuntimeError(f'Local bool-null patch repair failed to re-apply: {repaired_executor.fail_reason}')
        validated_obj = self._validate_extraction_result(repaired_data, valid_ids)
        return (validated_obj.model_dump(by_alias=True), repair_details)

    def process_document(self, filename: str):
        with tdm_logger.file_context(filename):
            process_started_at = time.time()
            base_name = filename.replace('.json', '')
            md_path = os.path.join(INPUT_MD_DIR, f'{base_name}.md')
            json_path = os.path.join(INPUT_JSON_DIR, filename)
            if not os.path.exists(md_path) or not os.path.exists(json_path):
                missing_parts = []
                if not os.path.exists(md_path):
                    missing_parts.append(f'markdown={md_path}')
                if not os.path.exists(json_path):
                    missing_parts.append(f'extraction_json={json_path}')
                detail = 'Missing audit input files: ' + ', '.join(missing_parts)
                self._quarantine_input_failure(filename, 'audit_request_retry_input_error', detail, duration=time.time() - process_started_at)
                return
            tdm_logger.log_event('audit_start', {'target': filename})
            try:
                md_content = self._load_file(md_path)
            except Exception as e:
                detail = f'Failed to read audit markdown input: {e}'
                self._quarantine_input_failure(filename, 'audit_request_retry_input_error', detail, duration=time.time() - process_started_at)
                return
            raw_json_text = None
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    raw_json_text = f.read()
                full_obj = json.loads(raw_json_text)
            except Exception as e:
                detail = f'Failed to load extraction JSON: {e}'
                self._quarantine_input_failure(filename, 'audit_request_retry_input_error', detail, raw_payload=raw_json_text if raw_json_text is not None else f'Unreadable file: {json_path}', duration=time.time() - process_started_at)
                return
            if not isinstance(full_obj, dict):
                detail = f'Extraction JSON root must be an object, got {type(full_obj).__name__}.'
                self._quarantine_input_failure(filename, 'audit_request_retry_input_error', detail, raw_payload=raw_json_text, duration=time.time() - process_started_at)
                return
            raw_metadata = full_obj.get('metadata')
            if raw_metadata is None:
                metadata = {}
            elif not isinstance(raw_metadata, dict):
                detail = f'Extraction JSON metadata must be an object, got {type(raw_metadata).__name__}.'
                self._quarantine_input_failure(filename, 'audit_request_retry_input_error', detail, wrapper=full_obj, raw_payload=raw_json_text, duration=time.time() - process_started_at)
                return
            else:
                metadata = raw_metadata
            if str(metadata.get('final_status', '')).lower() == 'no_data':
                tdm_logger.info(f'Skipping audit for {filename}: Step 6 marked document as no extractable data.')
                tdm_logger.log_event('audit_scored', {'total_penalty': 0.0, 'breakdown': {}, 'decision': 'audited_no_data', 'patch_count': 0})
                self._save_results(filename, full_obj, full_obj.get('extraction') or {'experiments': []}, None, 'audited_no_data', 0.0, quarantine=False)
                return
            draft = full_obj.get('extraction', {})
            valid_ids = set((int(m) for m in re.findall('\\[ID:\\s*(\\d+)\\]', md_content)))
            structure_instruction = ''
            if draft is None or not isinstance(draft, dict):
                tdm_logger.warning(f'[{os.path.basename(filename)}] ⚠️ 警告：接收到的 draft 为空或非字典结构，将初始化为空字典以尝试让大模型重新补全。')
                draft = {}
            draft_has_experiments = bool(draft.get('experiments'))
            if not draft_has_experiments:
                template_obj = {'experiments': [{'setup': {'chemical': {'name': {'v': 'REQUIRED_CHEMICAL_NAME', 'src': [0]}, 'type': {'v': 'pure_substance', 'src': [0]}}, 'light_source': {'type': {'v': 'Simulated Sunlight', 'src': [0]}, 'filter_used': False}, 'water': {'type': {'v': 'Distilled Water', 'src': [0]}}}, 'data': [{'conditions': {'pH': {'v': 7.0, 'src': [0]}, 'temp': {'v': 25.0, 'u': 'C', 'src': [0]}}, 'result': {'cat': 'kp', 'val': 1.5, 'val_kind': 'exact', 'u': 'h-1', 'src': [0]}, 'special_note': None}]}]}
                template_str = json.dumps(template_obj, indent=2)
                structure_instruction = f"""\n\n=== CRITICAL STRUCTURE REFERENCE ===\nThe Draft JSON is EMPTY. There are no reusable `_id` anchors yet.\nIn this special case, recover the document by using `MODIFY` with `target_path: "experiments"` and set the full experiments list as `new_value`.\nTo prevent validation errors, your `new_value` object MUST strictly follow this structure:\n1. `setup` fields use WRAPPERS: `{{'v': ..., 'src': ...}}`.\n2. `result` fields are FLAT (no wrapper for `val` or `u`).\nReference Template:\n```json\n{template_str}\n```"""
            user_content_base = f'=== SOURCE TEXT ===\n{md_content}\n\n=== DRAFT JSON (With UUIDs) ===\n{json.dumps(draft, ensure_ascii=False)}{structure_instruction}'
            schema_str = json.dumps(AuditReport.model_json_schema(), indent=2)
            final_system_prompt = f'{self.system_prompt}\n\n[CRITICAL INSTRUCTION ON PATCHING]\nThe JSON draft now contains unique `_id` fields for every Experiment and DataPoint.\nWhen creating patches, you MUST use `target_id` to refer to the object you want to modify or delete.\nDO NOT use array indices (e.g., experiments.0) as they are fragile.\nFor MODIFY: provide `target_id`, `target_field`, and `new_value`.\nFor DELETE: provide `target_id` only.\nFor ADD_ITEM: provide `target_id` (parent object) and `target_field` (list name).\nException: if the draft is completely empty and no `_id` exists yet, you MAY use `target_path: "experiments"` with `MODIFY` to create the root experiments list.\nStrictly output valid JSON adhering to this schema:\n```json\n{schema_str}\n```'
            tdm_logger.save_artifact(f'{filename}_audit_prompt.txt', {'system': final_system_prompt, 'user_preview': user_content_base[:500] + '...'})
            audit_report: Optional[AuditReport] = None
            start_time = process_started_at
            final_result = draft
            status = 'audit_failed_unknown'
            is_quarantined = False
            last_logic_error = None
            total_penalty = 0.0
            penalty_breakdown = {}
            for logic_attempt in range(AUDIT_LOGIC_RETRIES + 1):
                current_user_content = user_content_base
                if last_logic_error:
                    hint_msg = 'Please check your JSON format carefully.'
                    err_str = str(last_logic_error).lower()
                    if 'target_id' in err_str or 'must provide either' in err_str:
                        hint_msg = '👉 CRITICAL HINT: You FORGOT the \'target_id\'.\nACTION: Look at the \'Draft JSON\' provided above. Find the `_id` field (e.g., \'a1b2c3d4\') of the object you want to fix.\nRULE: You MUST include `"target_id": "..."` in your patch object.'
                    elif 'overwrite protection' in err_str:
                        hint_msg = "👉 HINT: You are trying to ADD a field that already exists.\nACTION: Use 'MODIFY' action instead of 'ADD_ITEM', or check if you are targeting the correct object."
                    elif 'safety check failed' in err_str:
                        hint_msg = "👉 HINT: Your `original_value_context` does not match the actual data in JSON.\nACTION: You must copy the value EXACTLY from the 'Draft JSON' (case-sensitive)."
                    elif 'schema validation failed' in err_str or 'validation error' in err_str:
                        hint_msg = '👉 HINT: The resulting JSON violates the schema.\nACTION: Did you create a required field but set it to null? Or use a string where a number is required?'
                    last_logic_error = f'{last_logic_error}\n\n{hint_msg}'
                    current_user_content += f'\n\n[SYSTEM FEEDBACK - PREVIOUS ATTEMPT FAILED]\nThe previous patches failed to apply or validate. \nError Details: {last_logic_error}\nPlease re-examine the JSON data and provide CORRECTED patches. Ensure `original_value_context` matches exactly what is in the JSON.'
                    tdm_logger.warning(f'🔄 Logic Retry {logic_attempt}/{AUDIT_LOGIC_RETRIES} for {filename} due to: {last_logic_error}')
                messages = [{'role': 'system', 'content': final_system_prompt}, {'role': 'user', 'content': current_user_content}]
                audit_report = None
                for attempt in range(MAX_RETRIES + 1):
                    try:
                        tdm_logger.log_event('llm_request', {'attempt': attempt, 'logic_attempt': logic_attempt, 'type': 'audit'})
                        audit_report = self._request_audit_report(filename=filename, messages=messages, attempt=attempt, logic_attempt=logic_attempt)
                        break
                    except Exception as e:
                        tdm_logger.warning(f'⚠️ API Attempt {attempt + 1} failed for {filename}: {e}')
                        tdm_logger.log_event('llm_error', {'error': str(e)}, severity='WARN')
                        if attempt == MAX_RETRIES:
                            tdm_logger.error(f'💀 Final API failure for {filename}')
                            last_logic_error = f'Audit LLM request failed: {e}'
                            err_str = str(e).lower()
                            if 'timeout' in err_str or 'read_timeout' in err_str:
                                with self.skip_lock:
                                    self.timeout_skipped.append(filename)
                if not audit_report:
                    status = 'audit_failed_llm_error'
                    is_quarantined = True
                    break
                executor = PatchExecutor(draft)
                total_penalty = 0.0
                has_fatal_error = False
                penalty_breakdown = {}
                for patch in audit_report.patches:
                    itype = patch.issue_type.value if hasattr(patch.issue_type, 'value') else str(patch.issue_type)
                    weight = PENALTY_WEIGHTS.get(itype, 1.0)
                    total_penalty += weight
                    if itype not in penalty_breakdown:
                        penalty_breakdown[itype] = {'count': 0, 'score': 0.0}
                    penalty_breakdown[itype]['count'] += 1
                    penalty_breakdown[itype]['score'] += weight
                RETRY_THRESHOLD = 120
                FATAL_REJECT_THRESHOLD = 800
                missing_data_stats = penalty_breakdown.get('missing_data', {})
                missing_data_count = int(missing_data_stats.get('count', 0) or 0)
                missing_data_score = float(missing_data_stats.get('score', 0.0) or 0.0)
                missing_data_ratio = missing_data_score / total_penalty if total_penalty else 0.0
                large_missing_data_dominant = missing_data_count >= LARGE_MISSING_DATA_RETRY_MIN_COUNT and missing_data_ratio >= LARGE_MISSING_DATA_RETRY_MIN_RATIO
                is_quarantined = False
                if not audit_report.patches:
                    if not draft_has_experiments:
                        last_logic_error = "Audit returned no patches for an empty extraction draft. When the draft has no experiments, you must reconstruct missing data via MODIFY with target_path='experiments' or explicitly report recoverable issues."
                        status = 'audit_failed_empty_draft'
                        is_quarantined = True
                        if logic_attempt < AUDIT_LOGIC_RETRIES:
                            continue
                        break
                    status = 'audited_clean'
                    final_result = draft
                    break
                elif total_penalty >= FATAL_REJECT_THRESHOLD and (not large_missing_data_dominant):
                    status = 'audit_rejected_fatal'
                    is_quarantined = True
                    tdm_logger.error(f'💀 Document {filename} quality too low (Penalty {total_penalty}). Rejecting.')
                    break
                elif total_penalty >= RETRY_THRESHOLD or has_fatal_error or large_missing_data_dominant:
                    status = 'audit_request_retry'
                    is_quarantined = True
                    tdm_logger.warning(f'🔄 Triggering Reflexion for {filename}. Penalty: {total_penalty}')
                    if large_missing_data_dominant:
                        tdm_logger.warning(f'Large missing-data retry detail for {filename}: {missing_data_count} missing_data patches, {missing_data_ratio:.1%} of audit penalty.')
                    feedback_msg = self._generate_feedback_str(audit_report.patches)
                    self._record_retry_feedback(filename, feedback_msg)
                    break
                else:
                    tdm_logger.info(f'🩹 Applying surgical patches for {filename}. Penalty: {total_penalty}')
                    patched_data = executor.apply_patches(audit_report.patches)
                    if patched_data is not None:
                        try:
                            final_result, repair_details = self._prevalidate_patched_result(filename=filename, draft=draft, patched_data=patched_data, patches=audit_report.patches, valid_ids=valid_ids)
                            status = 'audited_patched'
                            patch_log_payload = {'count': len(audit_report.patches)}
                            if repair_details:
                                patch_log_payload['local_schema_repairs'] = repair_details
                            tdm_logger.log_event('patch_applied', patch_log_payload)
                            break
                        except RuntimeError as re_err:
                            tdm_logger.error(f'鉂?Local Patch Repair Failed for {filename}: {re_err}')
                            last_logic_error = str(re_err)
                            status = 'audit_failed_patch_transaction'
                            is_quarantined = True
                            continue
                        except ValidationError as ve:
                            tdm_logger.error(f'🚨 Schema Corruption in {filename} after patching: {ve}')
                            tdm_logger.log_event('schema_corruption', {'error': str(ve)}, severity='ERROR')
                            last_logic_error = f'Schema Validation Failed: {str(ve)}'
                            status = 'audit_failed_schema_corruption'
                            is_quarantined = True
                            continue
                    else:
                        tdm_logger.error(f'❌ Patch Transaction Failed for {filename}: {executor.fail_reason}')
                        last_logic_error = f'Patch Transaction Failed: {executor.fail_reason}'
                        status = 'audit_failed_patch_transaction'
                        is_quarantined = True
                        continue
            if is_quarantined and status in {'audit_failed_unknown', 'audit_failed_empty_draft', 'audit_failed_llm_error', 'audit_failed_patch_transaction', 'audit_failed_schema_corruption'} and (filename not in self.rejected_map):
                self._record_retry_feedback(filename, self._build_recovery_feedback(status, last_logic_error))
            tdm_logger.log_event('audit_scored', {'total_penalty': total_penalty, 'breakdown': penalty_breakdown, 'decision': status, 'patch_count': len(audit_report.patches) if audit_report else 0})
            duration = time.time() - start_time
            self._save_results(filename, full_obj, final_result, audit_report, status, duration, quarantine=is_quarantined)
            icon = '🔴' if is_quarantined else '🟢'
            tdm_logger.info(f'{icon} {filename} | Penalty: {total_penalty:.1f} | Status: {status} | Time: {duration:.1f}s')

    def _save_results(self, filename, wrapper, final_data, report, status, duration, quarantine=False):
        wrapper['extraction'] = final_data
        score = report.stats.quality_score if report else 0
        patches_count = len(report.patches) if report else 0
        metadata = wrapper.setdefault('metadata', {})
        metadata.update({'step3_status': status, 'step3_audit_timestamp': time.strftime('%Y-%m-%d %H:%M:%S'), 'step3_quality_score': score, 'quality_score': score, 'step3_patches_applied': patches_count, 'step3_duration': round(duration, 2)})
        target_dir = OUTPUT_QUARANTINE_DIR if quarantine else OUTPUT_DB_DIR
        with open(os.path.join(target_dir, filename), 'w', encoding='utf-8') as f:
            json.dump(wrapper, f, indent=2, ensure_ascii=False)
        if report:
            log_path = os.path.join(OUTPUT_LOG_DIR, filename.replace('.json', '_audit_report.json'))
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(report.model_dump(), f, indent=2, ensure_ascii=False)

    def dump_rejection_feedback(self):
        if not self.rejected_map:
            if os.path.exists(self.feedback_output_path):
                os.remove(self.feedback_output_path)
            return
        feedback_data = {'targets': list(self.rejected_map.keys()), 'feedback': self.rejected_map}
        with open(self.feedback_output_path, 'w', encoding='utf-8') as f:
            json.dump(feedback_data, f, indent=2, ensure_ascii=False)
        tdm_logger.info(f'💾 Generated rejection feedback for {len(self.rejected_map)} files at: {self.feedback_output_path}')

    def run_batch(self):
        if self.targets_path and os.path.exists(self.targets_path):
            with open(self.targets_path, 'r', encoding='utf-8') as f:
                target_filenames = json.load(f)
            files = []
            for t in target_filenames:
                full_path = os.path.join(INPUT_JSON_DIR, t)
                if os.path.exists(full_path):
                    files.append(full_path)
                else:
                    tdm_logger.warning(f'Target file not found in input dir: {t}')
            tdm_logger.info(f'🚀 Reflexion Mode: Auditing {len(files)} targeted files (Incremental)...')
        else:
            files = glob.glob(os.path.join(INPUT_JSON_DIR, '*.json'))
            tdm_logger.info(f'🚀 Starting Surgical Audit on {len(files)} files (Full Scan)...')
        total_files = len(files)
        if total_files == 0:
            if self.targets_path and os.path.exists(self.targets_path):
                error_msg = 'Reflexion target set resolved to 0 extracted JSON files.'
                tdm_logger.error(error_msg)
                raise RuntimeError(error_msg)
            return

        def _thread_worker(filename):
            try:
                self.process_document(filename)
            except Exception as e:
                with tdm_logger.file_context(filename):
                    tdm_logger.error(f'❌ Thread Crash: {filename} - {e}')
                    tdm_logger.log_event('thread_crash', {'error': str(e)}, severity='ERROR')
                raise
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_file = {executor.submit(_thread_worker, os.path.basename(f)): os.path.basename(f) for f in files}
            crashed_files = []
            for future in tqdm(concurrent.futures.as_completed(future_to_file), total=total_files):
                try:
                    future.result()
                except Exception:
                    crashed_files.append(future_to_file[future])
        if crashed_files:
            crashed_preview = ', '.join(crashed_files[:5])
            suffix = '' if len(crashed_files) <= 5 else f' ... (+{len(crashed_files) - 5} more)'
            error_msg = f'Worker crashes detected for {len(crashed_files)} file(s): {crashed_preview}{suffix}'
            tdm_logger.error(error_msg)
            raise RuntimeError(error_msg)
        if self.timeout_skipped:
            timeout_report_path = os.path.join(OUTPUT_LOG_DIR, 'step3_timeout_skipped.json')
            with open(timeout_report_path, 'w', encoding='utf-8') as f:
                json.dump({'timeout_files': self.timeout_skipped}, f, indent=2, ensure_ascii=False)
            tdm_logger.warning(f'Saved {len(self.timeout_skipped)} timeout-skipped files to {timeout_report_path}')
        self.dump_rejection_feedback()
if __name__ == '__main__':
    tdm_logger.setup_tdm_logging('Step7_Audit')
    parser = argparse.ArgumentParser()
    parser.add_argument('--output_feedback', help='Path to write the rejection feedback JSON', default=None)
    parser.add_argument('--targets', help='Path to JSON file containing list of filenames to audit (Incremental Mode)', default=None)
    args = parser.parse_args()
    agent = AuditAgent(feedback_output_path=args.output_feedback, targets_path=args.targets)
    agent.run_batch()
