import os
import re
import json
import glob
import time
import logging
import threading
import shutil
import concurrent.futures
import traceback
import argparse
import copy
from typing import List, Optional, Dict, Any, Tuple
from dotenv import load_dotenv
from openai import OpenAI, LengthFinishReasonError
from pydantic import ValidationError
from tqdm import tqdm
from json_repair import repair_json
import tdm_logger
from pydantic_step2 import ExtractionResult, Experiment
from llm_runtime import apply_llm_runtime_defaults, build_llm_configs, env_float_from_names, env_int_from_names, extract_token_usage, is_truncated_finish_reason, llm_request_options, llm_token_limit_options, wait_if_pause_requested
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_DATA_DIR = os.environ.get('TDM_DATA_DIR') or os.path.join(_PROJECT_ROOT, 'data')
INPUT_DIR = os.path.join(_DATA_DIR, '05_cleaned_markdown')
OUTPUT_DIR = os.path.join(_DATA_DIR, '06_extracted_json')
PROMPT_FILE = os.path.join(_SCRIPT_DIR, 'PROMPT_STEP2.md')
load_dotenv(override=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
SELECTED_PROVIDER = os.getenv('STEP2_LLM_PROVIDER', 'openrouter1').lower()
MAX_WORKERS = int(os.getenv('STEP2_MAX_WORKERS', '2'))
MAX_RETRIES = env_int_from_names(('STEP2_MAX_RETRIES', 'LLM_MAX_RETRIES'), 3)
MAX_CONTINUATIONS = env_int_from_names(('STEP2_MAX_CONTINUATIONS', 'LLM_MAX_CONTINUATIONS'), 10)
_extraction_schema = ExtractionResult.model_json_schema()
LLM_CONFIGS = build_llm_configs('STEP2', 'extraction_result', _extraction_schema)
CURRENT_CONF = LLM_CONFIGS[SELECTED_PROVIDER]
MODEL_NAME = CURRENT_CONF['model']
TEMPERATURE = float(os.getenv('STEP2_TEMPERATURE', '0.0'))
VALIDATION_CATEGORY_RULES: Dict[str, Dict[str, str]] = {'src_id_error': {'label': 'Invalid paragraph ID evidence', 'prompt_hint': 'Every `src` must be a list of integer paragraph IDs that actually exist in the provided markdown. Never invent IDs and never keep IDs that are absent from the source text.'}, 'missing_required_field': {'label': 'Required schema field missing', 'prompt_hint': 'Do not omit required schema fields. Every experiment needs both `setup` and `data`, and required child fields must be present.'}, 'wrapper_object_error': {'label': 'Wrapper object shape error', 'prompt_hint': 'Wrapper fields must use the schema wrapper objects, for example `{ "v": ..., "src": [...] }`, rather than raw strings, raw numbers, or malformed dictionaries.'}, 'enum_error': {'label': 'Enum literal not allowed by schema', 'prompt_hint': 'Use only schema-approved enum literals exactly as defined in the schema, with correct spelling and casing.'}, 'numeric_type_error': {'label': 'Numeric field contains non-numeric content', 'prompt_hint': 'Put pure numeric values in numeric fields only. Units belong in `u`, and approximate or textual expressions belong in `desc` or `val_text` instead of numeric slots.'}, 'list_shape_error': {'label': 'List field shape error', 'prompt_hint': 'Fields defined as lists must remain lists. Keep `experiments`, `data`, `additives`, and each `src` in list form.'}, 'value_logic_error': {'label': 'Step 6 value logic violation', 'prompt_hint': 'Respect the Step 6 result logic: use either `val` or `val_text`, keep `val_kind` consistent, and provide `param_name` whenever `cat` is `other`.'}, 'schema_other': {'label': 'Other schema validation issue', 'prompt_hint': 'Re-read the schema and fix the reported field-level issues exactly as described.'}}
LIST_SCOPE_LABELS = {'experiments': 'experiment', 'data': 'data_point', 'additives': 'additive'}
OBJECT_SCOPE_LABELS = {'setup': 'setup', 'conditions': 'conditions', 'result': 'result', 'chemical': 'chemical', 'light_source': 'light_source', 'reactor': 'reactor', 'water': 'water', 'conc': 'conc', 'pH': 'pH', 'temp': 'temp'}

class ScientificMiningAgent:

    def __init__(self, feedback_map: Dict[str, str]=None, config: Dict=None, provider_name: str=None):
        self.conf = config if config else CURRENT_CONF
        self.provider_name = provider_name if provider_name else SELECTED_PROVIDER
        self.model_name = self.conf['model']
        self.llm_timeout = env_float_from_names(('STEP2_LLM_TIMEOUT', 'LLM_TIMEOUT'), 180.0)
        self.max_continuations = max(0, int(MAX_CONTINUATIONS or 0))
        self.token_limit_options = llm_token_limit_options(self.conf)
        self.timeout_skipped = []
        self.skip_lock = threading.Lock()
        self.summary_lock = threading.Lock()
        self.log_dir = os.getenv('TDM_LOG_DIR', OUTPUT_DIR)
        self.document_summaries: List[Dict[str, Any]] = []
        os.makedirs(self.log_dir, exist_ok=True)
        api_key = os.getenv(self.conf['api_key_env'])
        if not api_key:
            raise ValueError(f"Missing API Key for provider '{self.provider_name}'. Check .env for {self.conf['api_key_env']}.")
        default_headers = self.conf.get('extra_headers', None)
        tdm_logger.info(f'Initializing Client: Provider={self.provider_name.upper()}, Model={self.model_name}')
        tdm_logger.info(f"Step 6 LLM runtime: timeout={self.llm_timeout}s, retries={MAX_RETRIES}, max_continuations={self.max_continuations}, token_limit={self.token_limit_options or 'provider_default'}")
        self.client = OpenAI(api_key=api_key, base_url=self.conf['base_url'], default_headers=default_headers, timeout=self.llm_timeout)
        self.raw_system_prompt = self._load_file(PROMPT_FILE)
        self.feedback_map = feedback_map or {}
        self.schema_json_str = json.dumps(ExtractionResult.model_json_schema(), indent=2)
        self.output_mode = self._resolve_output_mode()

    def _load_file(self, path: str) -> str:
        if not os.path.exists(path):
            raise FileNotFoundError(f'Critical file missing: {path}')
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def _write_terminal_failure_report(self, filename: str, status: str, detail: str):
        output_path = os.path.join(OUTPUT_DIR, filename.replace('.md', '.json'))
        fail_path = output_path.replace('.json', '_FAILURE_REPORT.txt')
        with open(fail_path, 'w', encoding='utf-8') as f:
            f.write(f'Step 6 terminal status: {status}\n')
            f.write(f'Detail: {detail}\n')

    def _resolve_output_mode(self) -> str:
        explicit_mode = self.conf.get('output_mode')
        if explicit_mode:
            return str(explicit_mode)
        return 'strict' if self.conf.get('use_native_parse') else 'json'

    def _get_response_format(self) -> Dict[str, Any]:
        return self.conf.get('response_format') or {'type': 'json_object'}

    def _extract_valid_ids(self, text: str) -> List[int]:
        matches = re.findall('\\[ID:\\s*(\\d+)\\]', text)
        return sorted(list(set((int(m) for m in matches))))

    def _format_error_location(self, loc: Any) -> str:
        return self._loc_to_json_path(loc)

    def _loc_to_json_path(self, loc: Any) -> str:
        if not loc:
            return '<root>'
        parts: List[str] = []
        for token in loc:
            if isinstance(token, int):
                if parts:
                    parts[-1] = f'{parts[-1]}[{token}]'
                else:
                    parts.append(f'[{token}]')
            else:
                parts.append(str(token))
        return '.'.join(parts) if parts else '<root>'

    def _build_scope_frames(self, loc: Tuple[Any, ...]) -> List[Dict[str, Any]]:
        frames: List[Dict[str, Any]] = []
        consumed: List[Any] = []
        idx = 0
        while idx < len(loc):
            token = loc[idx]
            if isinstance(token, int):
                consumed.append(token)
                idx += 1
                continue
            consumed.append(token)
            if token in LIST_SCOPE_LABELS and idx + 1 < len(loc) and isinstance(loc[idx + 1], int):
                item_index = loc[idx + 1]
                consumed.append(item_index)
                label = LIST_SCOPE_LABELS[token]
                frames.append({'kind': label, 'path': self._loc_to_json_path(tuple(consumed)), 'label': f'{label}[{item_index + 1}]', 'index_zero_based': item_index, 'index_one_based': item_index + 1})
                idx += 2
                continue
            if token in OBJECT_SCOPE_LABELS:
                label = OBJECT_SCOPE_LABELS[token]
                frames.append({'kind': label, 'path': self._loc_to_json_path(tuple(consumed)), 'label': label})
            idx += 1
        return frames

    def _summarize_scope_kind(self, frames: List[Dict[str, Any]]) -> str:
        preferred = ['data_point', 'setup', 'experiment', 'additive']
        for kind in preferred:
            if any((frame.get('kind') == kind for frame in frames)):
                return kind
        return frames[-1]['kind'] if frames else 'root'

    def _build_field_target(self, loc: Tuple[Any, ...], category: str, message: str, err_type: str) -> Dict[str, Any]:
        path = self._loc_to_json_path(loc)
        field_name = None
        if loc:
            last = loc[-1]
            if not isinstance(last, int):
                field_name = str(last)
        parent_loc = loc[:-1] if field_name is not None else loc
        parent_path = self._loc_to_json_path(parent_loc) if parent_loc else '<root>'
        frames = self._build_scope_frames(loc)
        experiment_frame = next((frame for frame in frames if frame.get('kind') == 'experiment'), None)
        data_point_frame = next((frame for frame in frames if frame.get('kind') == 'data_point'), None)
        additive_frame = next((frame for frame in frames if frame.get('kind') == 'additive'), None)
        target = {'path': path, 'field_name': field_name, 'parent_path': parent_path, 'parent_object': frames[-1]['kind'] if frames else 'root', 'scope_kind': self._summarize_scope_kind(frames), 'scope_hierarchy': frames, 'scope_hierarchy_text': ' > '.join((frame['label'] for frame in frames)) if frames else '<root>', 'category': category, 'message': message, 'type': err_type, 'experiment_index': experiment_frame.get('index_zero_based') if experiment_frame else None, 'experiment_number': experiment_frame.get('index_one_based') if experiment_frame else None, 'data_point_index': data_point_frame.get('index_zero_based') if data_point_frame else None, 'data_point_number': data_point_frame.get('index_one_based') if data_point_frame else None, 'additive_index': additive_frame.get('index_zero_based') if additive_frame else None, 'additive_number': additive_frame.get('index_one_based') if additive_frame else None}
        return target

    def _field_target_key(self, item: Dict[str, Any]) -> str:
        return '::'.join([str(item.get('category', '')), str(item.get('path', '')), str(item.get('field_name', ''))])

    def _paths_overlap(self, left: str, right: str) -> bool:
        if not left or not right:
            return False
        if left == right:
            return True
        prefixes = ('.', '[')
        return any((left.startswith(f'{right}{marker}') for marker in prefixes)) or any((right.startswith(f'{left}{marker}') for marker in prefixes))

    def _compare_targeted_fields(self, requested_targets: List[Dict[str, Any]], observed_targets: List[Dict[str, Any]]) -> Dict[str, Any]:
        remaining_map: Dict[str, Dict[str, Any]] = {}
        new_issue_map: Dict[str, Dict[str, Any]] = {}
        for observed in observed_targets:
            observed_path = str(observed.get('path', ''))
            overlaps = any((self._paths_overlap(observed_path, str(requested.get('path', ''))) for requested in requested_targets))
            target_map = remaining_map if overlaps else new_issue_map
            target_map[self._field_target_key(observed)] = observed
        remaining_fields = list(remaining_map.values())
        new_fields = list(new_issue_map.values())
        return {'requested_count': len(requested_targets), 'remaining_count': len(remaining_fields), 'resolved_count': max(len(requested_targets) - len(remaining_fields), 0), 'remaining_fields': remaining_fields, 'new_issue_count': len(new_fields), 'new_fields': new_fields}

    def _classify_validation_error(self, err: Dict[str, Any]) -> str:
        loc_parts = [str(x).lower() for x in err.get('loc', ())]
        loc_str = ' '.join(loc_parts)
        msg = str(err.get('msg', '')).lower()
        err_type = str(err.get('type', '')).lower()
        if 'src' in loc_str and ('not found in text' in msg or 'paragraph id' in msg or ' id ' in f' {msg} ' or ('list_type' in err_type) or ('int' in err_type)):
            return 'src_id_error'
        if err_type.endswith('missing') or 'field required' in msg:
            return 'missing_required_field'
        if 'valid dictionary' in msg or 'valid object' in msg or err_type in {'model_type', 'dict_type'}:
            return 'wrapper_object_error'
        if 'enum' in err_type or ('input should be' in msg and any((token in msg for token in ['yes', 'no', 'null', 'pure_substance', 'mixture', 'current_study', 'cited_literature', 'kp', 'hl', 'qy', 'k_oh', 'k_1o2', 'k_3dom', 'other']))):
            return 'enum_error'
        if 'list' in err_type or 'valid list' in msg:
            return 'list_shape_error'
        if any((token in err_type for token in ['float', 'int', 'number'])) or 'valid number' in msg or 'unable to parse string as a number' in msg:
            return 'numeric_type_error'
        if any((token in msg for token in ["requires either 'val'", 'param_name', 'val_text', 'val_kind', "category is 'other'", 'exact'])):
            return 'value_logic_error'
        return 'schema_other'

    def _build_validation_report(self, e: ValidationError) -> Dict[str, Any]:
        entries: List[Dict[str, Any]] = []
        category_counts: Dict[str, int] = {}
        field_targets: List[Dict[str, Any]] = []
        seen_field_targets = set()
        for err in e.errors():
            category = self._classify_validation_error(err)
            message = str(err.get('msg', '')).replace('Value error, ', '').strip()
            loc = tuple(err.get('loc', ()))
            entry = {'path': self._format_error_location(loc), 'category': category, 'message': message, 'type': str(err.get('type', ''))}
            entries.append(entry)
            category_counts[category] = category_counts.get(category, 0) + 1
            field_target = self._build_field_target(loc=loc, category=category, message=message, err_type=str(err.get('type', '')))
            field_key = self._field_target_key(field_target)
            if field_key not in seen_field_targets:
                field_targets.append(field_target)
                seen_field_targets.add(field_key)
        sorted_categories = sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
        category_breakdown = [{'code': code, 'label': VALIDATION_CATEGORY_RULES.get(code, VALIDATION_CATEGORY_RULES['schema_other'])['label'], 'count': count, 'prompt_hint': VALIDATION_CATEGORY_RULES.get(code, VALIDATION_CATEGORY_RULES['schema_other'])['prompt_hint']} for code, count in sorted_categories]
        entry_limit = 12
        sample_entries = entries[:entry_limit]
        missing_fields = [item for item in field_targets if item.get('category') == 'missing_required_field']
        other_field_errors = [item for item in field_targets if item.get('category') != 'missing_required_field']
        summary_lines = [f'Validation error count: {len(entries)}']
        if category_breakdown:
            summary_lines.append('Category breakdown:')
            for item in category_breakdown:
                summary_lines.append(f"- {item['code']} x{item['count']}: {item['label']}")
        if missing_fields:
            summary_lines.append('Missing required fields:')
            for item in missing_fields:
                summary_lines.append(f"- {item['path']} | field={item.get('field_name')} | scope={item['scope_hierarchy_text']}")
        if other_field_errors:
            summary_lines.append('Other targeted field errors:')
            for item in other_field_errors:
                summary_lines.append(f"- {item['path']} | {item['category']} | scope={item['scope_hierarchy_text']}: {item['message']}")
        if sample_entries:
            summary_lines.append('Representative field-level errors:')
            for entry in sample_entries:
                summary_lines.append(f"- {entry['path']} | {entry['category']}: {entry['message']}")
        prompt_lines = ['Validator found field-level schema errors in the previous JSON.', f'Total validation errors: {len(entries)}.']
        if category_breakdown:
            prompt_lines.append('Error categories:')
            for item in category_breakdown:
                prompt_lines.append(f"- {item['code']} x{item['count']}: {item['label']}")
        if missing_fields:
            prompt_lines.append('Missing required fields to add:')
            for item in missing_fields:
                prompt_lines.append(f"- path={item['path']} | field={item.get('field_name')} | scope={item['scope_hierarchy_text']}")
        if other_field_errors:
            prompt_lines.append('Other field-level issues to minimally fix:')
            for item in other_field_errors:
                prompt_lines.append(f"- path={item['path']} | {item['category']} | {item['message']}")
        if category_breakdown:
            prompt_lines.append('Repair constraints:')
            for item in category_breakdown:
                prompt_lines.append(f"- {item['prompt_hint']}")
            prompt_lines.append('- Only touch the listed field paths or their direct child fields when required to satisfy the schema.')
            prompt_lines.append('- Preserve every correct `_id`, list order, and already-valid value.')
        short_summary = ', '.join((f"{item['code']} x{item['count']}" for item in category_breakdown)) or 'unknown_validation_error'
        return {'summary_text': '\n'.join(summary_lines), 'prompt_text': '\n'.join(prompt_lines), 'structured': {'total_errors': len(entries), 'category_count': len(category_breakdown), 'categories': category_breakdown, 'entries': sample_entries, 'entries_truncated': len(entries) > entry_limit, 'field_targets': field_targets, 'field_target_count': len(field_targets), 'missing_fields': missing_fields, 'missing_field_count': len(missing_fields), 'other_field_errors': other_field_errors, 'other_field_error_count': len(other_field_errors), 'short_summary': short_summary}}

    def _build_targeted_repair_system_prompt(self) -> str:
        return f'You are repairing an existing Step 6 extraction JSON after validator feedback.\nThis is targeted field-level repair mode, not a full re-extraction.\nUse the provided CURRENT_JSON as the base object.\nOnly add or minimally edit the exact field paths listed in TARGETED_REPAIR_MANIFEST.\nDo not rewrite, reorder, drop, or regenerate unrelated experiments, setup blocks, data points, or fields.\nPreserve every existing `_id`, list order, grouping structure, and already-correct content.\nFor missing required fields, add only the missing field or the smallest required child structure at that path.\nFor non-missing validation issues, minimally edit only the listed field path or its direct child fields if required by schema.\nUse only evidence from SOURCE_MARKDOWN, and keep every `src` as valid paragraph IDs present in the markdown.\nReturn exactly one complete JSON object that conforms to the ExtractionResult schema.\nSchema:\n```json\n{self.schema_json_str}\n```'

    def _build_targeted_repair_context(self, filename: str, md_content: str, invalid_payload: Dict[str, Any], validation_report: Dict[str, Any], source_attempt: int) -> Dict[str, Any]:
        candidate_json_str = json.dumps(invalid_payload, indent=2, ensure_ascii=False)
        structured = validation_report['structured']
        repair_manifest = {'repair_mode': 'field_targeted_repair', 'document': filename, 'source_attempt': source_attempt, 'repair_goal': 'Fill only validator-reported missing fields and minimally fix listed field-level schema errors.', 'rules': ['Use CURRENT_JSON as the base object.', 'Only touch the listed field paths or their direct child fields when schema requires it.', 'Do not rewrite unrelated content.', 'Preserve every existing `_id`, list order, and already-correct field.'], 'missing_fields': structured.get('missing_fields', []), 'other_field_errors': structured.get('other_field_errors', []), 'all_target_fields': structured.get('field_targets', [])}
        manifest_artifact = tdm_logger.save_artifact(f'{filename}_targeted_repair_manifest_from_try{source_attempt}.json', repair_manifest)
        candidate_artifact = tdm_logger.save_artifact(f'{filename}_invalid_candidate_from_try{source_attempt}.json', invalid_payload)
        user_prompt = f'[TARGETED FIELD REPAIR]\nDocument: {filename}\nSource Attempt: {source_attempt}\nTask: Apply validator-directed field repairs to CURRENT_JSON.\nDo not regenerate the whole answer. Start from CURRENT_JSON and patch only the listed fields.\n\nTARGETED_REPAIR_MANIFEST:\n```json\n{json.dumps(repair_manifest, indent=2, ensure_ascii=False)}\n```\n\nCURRENT_JSON:\n```json\n{candidate_json_str}\n```\n\nSOURCE_MARKDOWN:\n{md_content}'
        system_prompt = self._build_targeted_repair_system_prompt()
        prompt_artifact = tdm_logger.save_artifact(f'{filename}_targeted_repair_prompt_from_try{source_attempt}.txt', {'system': system_prompt, 'user': user_prompt})
        metadata = {'mode': 'field_targeted_repair', 'source_attempt': source_attempt, 'target_field_count': structured.get('field_target_count', 0), 'missing_field_count': structured.get('missing_field_count', 0), 'other_field_error_count': structured.get('other_field_error_count', 0), 'target_field_paths': [item.get('path') for item in structured.get('field_targets', [])], 'missing_field_paths': [item.get('path') for item in structured.get('missing_fields', [])], 'repair_manifest_artifact': manifest_artifact, 'candidate_json_artifact': candidate_artifact, 'prompt_artifact': prompt_artifact, 'target_fields': copy.deepcopy(structured.get('field_targets', [])), 'missing_fields': copy.deepcopy(structured.get('missing_fields', []))}
        return {'messages': [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}], 'metadata': metadata}

    def _finalize_targeted_repair_followup(self, repair_context: Optional[Dict[str, Any]], outcome_status: str, validation_report: Optional[Dict[str, Any]]=None):
        if not repair_context:
            return
        source_attempt_record = repair_context.get('source_attempt_record')
        metadata = repair_context.get('metadata', {})
        if not source_attempt_record:
            return
        followup: Dict[str, Any] = {'status': outcome_status, 'source_attempt': metadata.get('source_attempt'), 'target_field_count': metadata.get('target_field_count', 0), 'missing_field_count': metadata.get('missing_field_count', 0)}
        if validation_report:
            comparison = self._compare_targeted_fields(requested_targets=metadata.get('target_fields', []), observed_targets=validation_report['structured'].get('field_targets', []))
            followup.update({'resolved_count': comparison['resolved_count'], 'remaining_count': comparison['remaining_count'], 'remaining_fields': comparison['remaining_fields'], 'new_issue_count': comparison['new_issue_count'], 'new_fields': comparison['new_fields']})
        elif outcome_status == 'success':
            followup.update({'resolved_count': metadata.get('target_field_count', 0), 'remaining_count': 0, 'remaining_fields': [], 'new_issue_count': 0, 'new_fields': []})
        else:
            followup.update({'resolved_count': None, 'remaining_count': None, 'remaining_fields': [], 'new_issue_count': None, 'new_fields': []})
        followup_artifact = tdm_logger.save_artifact(f"{source_attempt_record.get('attempt', 'unknown')}_{source_attempt_record.get('status', 'validation')}_{outcome_status}_repair_followup.json", followup)
        followup['artifact'] = followup_artifact
        source_attempt_record['after_targeted_repair'] = followup
        tdm_logger.log_event('targeted_repair_followup', {'source_attempt': metadata.get('source_attempt'), 'status': outcome_status, 'resolved_count': followup.get('resolved_count'), 'remaining_count': followup.get('remaining_count'), 'artifact': followup_artifact})

    def _prepare_system_prompt(self, filename: str, mode: str) -> str:
        try:
            base_prompt = self.raw_system_prompt
            base_name = filename.replace('.md', '').replace('.json', '')
            feedback_key = f'{base_name}.json'
            if feedback_key in self.feedback_map:
                tdm_logger.info(f'Injecting Audit Feedback for {filename}')
                feedback_msg = self.feedback_map[feedback_key]
                if isinstance(feedback_msg, (dict, list)):
                    feedback_block = f"Auditor's Structured Feedback JSON:\n```json\n{json.dumps(feedback_msg, ensure_ascii=False, indent=2)}\n```"
                else:
                    feedback_block = f'''Auditor's Feedback: "{feedback_msg}"'''
                base_prompt += f'\n\n[CRITICAL CORRECTION INSTRUCTION]\nYour previous extraction for this document was REJECTED by an expert auditor.\n{feedback_block}\nYou MUST pay special attention to fixing these issues in this new attempt. If the structured feedback lists missing table rows, row/run identifiers, or parameter columns, explicitly re-extract those rows and metrics instead of making a generic correction.'
            if mode == 'strict':
                injection = f"\n\n[FORMAT INSTRUCTION]\nPopulate the data structure strictly based on the text.\nEnsure all 'src' fields contain valid Paragraph IDs from the text."
            else:
                injection = f'\n\n[CRITICAL FORMAT INSTRUCTION]\nYou MUST output valid JSON only, strictly adhering to this Schema:\n```json\n{self.schema_json_str}\n```\nDo not include any markdown formatting (like ```json) in the response if possible, just the raw JSON.'
            return base_prompt + injection
        except Exception as e:
            tdm_logger.warning(f'Prompt preparation failed: {e}')
            return self.raw_system_prompt

    def process_document(self, file_path: str):
        filename = os.path.basename(file_path)
        tdm_logger.info(f'START Processing: {filename}')
        try:
            md_content = self._load_file(file_path)
        except Exception as e:
            tdm_logger.error(f'Read Error: {filename} - {str(e)}')
            self._write_terminal_failure_report(filename, 'read_error', str(e))
            history = [{'attempt': 0, 'status': 'read_error', 'error': str(e), 'elapsed_total': 0.0, 'batches': []}]
            tdm_logger.log_event('doc_finish', {'status': 'failed', 'reason': 'read_error', 'error': str(e)}, severity='ERROR')
            tdm_logger.log_event('fatal_error', {'error': str(e)}, severity='ERROR')
            self._record_document_summary(filename, None, history, 0, 'failed')
            return
        valid_ids = self._extract_valid_ids(md_content)
        if not valid_ids:
            if not md_content.strip():
                tdm_logger.info(f'Skipping {filename}: filtered markdown is empty; treating document as no extractable data.')
                no_data_result = ExtractionResult(experiments=[])
                history = [{'attempt': 0, 'status': 'no_data', 'reason': 'empty_filtered_markdown', 'elapsed_total': 0.0, 'batches': []}]
                self._save_output(filename, no_data_result, history, 0, final_status='no_data')
                tdm_logger.log_event('skipped', {'reason': 'no_data_empty_markdown'})
                tdm_logger.log_event('doc_finish', {'status': 'success', 'final_status': 'no_data', 'reason': 'empty_filtered_markdown', 'experiments_count': 0})
                return
            tdm_logger.warning(f'Skipping {filename}: No [ID: X] tags found.')
            no_id_reason = 'No [ID: X] tags remained after filtering; extraction cannot proceed.'
            self._write_terminal_failure_report(filename, 'no_valid_ids', no_id_reason)
            history = [{'attempt': 0, 'status': 'no_valid_ids', 'error': no_id_reason, 'elapsed_total': 0.0, 'batches': []}]
            tdm_logger.log_event('skipped', {'reason': 'no_valid_ids'})
            tdm_logger.log_event('doc_finish', {'status': 'failed', 'reason': 'no_valid_ids'}, severity='WARN')
            self._record_document_summary(filename, None, history, 0, 'failed')
            return
        tdm_logger.log_event('doc_start', {'file_size': len(md_content), 'valid_ids_count': len(valid_ids)})
        mode = self.output_mode
        base_system_prompt = self._prepare_system_prompt(filename, mode)
        retry_context: Optional[Dict[str, Any]] = None
        attempt_history = []
        start_time = time.time()
        final_obj = None
        doc_finish_emitted = False
        for attempt in range(MAX_RETRIES + 1):
            active_repair_context = retry_context
            retry_context = None
            if attempt > 0:
                attempt_label = 'Field-Targeted Repair' if active_repair_context else 'Self-Correction'
                tdm_logger.info(f'Attempt {attempt}/{MAX_RETRIES} ({attempt_label}) for {filename}...')
            if active_repair_context:
                messages = copy.deepcopy(active_repair_context['messages'])
                prompt_payload = {'system': messages[0]['content'], 'user': messages[1]['content']}
                prompt_len_est = sum((len(msg.get('content', '')) for msg in messages))
            else:
                current_system_prompt = base_system_prompt
                messages = [{'role': 'system', 'content': current_system_prompt}, {'role': 'user', 'content': md_content}]
                prompt_payload = {'system': current_system_prompt, 'user_content_preview': md_content[:500] + '...'}
                prompt_len_est = len(current_system_prompt) + len(md_content)
            prompt_artifact = tdm_logger.save_artifact(f'{filename}_prompt_try{attempt}.txt', prompt_payload)
            attempt_record: Dict[str, Any] = {'attempt': attempt, 'mode': mode, 'prompt_len_est': prompt_len_est, 'prompt_artifact': prompt_artifact, 'status': 'running', 'batches': [], 'truncated_batches': 0}
            if active_repair_context:
                attempt_record['repair_request'] = copy.deepcopy(active_repair_context['metadata'])
                attempt_record['repair_mode'] = active_repair_context['metadata'].get('mode')
            pending_validation_report: Optional[Dict[str, Any]] = None
            pending_invalid_payload: Optional[Dict[str, Any]] = None
            pending_repair_base_payload: Optional[Dict[str, Any]] = None
            try:
                tdm_logger.log_event('llm_request', {'attempt': attempt, 'model': self.model_name, 'mode': mode, 'prompt_len_est': attempt_record['prompt_len_est']})
                req_start = time.time()
                if mode == 'strict':
                    try:
                        completion = self.client.beta.chat.completions.parse(model=self.model_name, messages=messages, response_format=ExtractionResult, temperature=TEMPERATURE, **self.token_limit_options, **llm_request_options(self.conf, include_response_format=False))
                        wait_if_pause_requested(f'after Step 6 strict LLM response for {filename}')
                        req_duration = time.time() - req_start
                        usage = extract_token_usage(completion)
                        attempt_record['llm_latency'] = round(req_duration, 4)
                        attempt_record['response_type'] = 'strict'
                        attempt_record['usage'] = usage
                        tdm_logger.log_event('llm_response', {'latency': req_duration, 'type': 'strict', 'usage': usage})
                        if completion.choices[0].message.refusal:
                            raise ValueError(f'Model Refusal: {completion.choices[0].message.refusal}')
                        parsed_obj = completion.choices[0].message.parsed
                        result_dump = parsed_obj.model_dump(by_alias=True)
                        pending_invalid_payload = result_dump
                        pending_repair_base_payload = result_dump
                        final_obj = ExtractionResult.model_validate(result_dump, context={'valid_ids': valid_ids})
                        pending_invalid_payload = None
                        pending_repair_base_payload = None
                    except LengthFinishReasonError:
                        tdm_logger.error(f'Error: Token limit exceeded in Strict Mode ({filename})')
                        raise ValueError('Token limit exceeded in Strict Mode')
                else:
                    accumulated_experiments: List[Experiment] = []
                    batch_step = 0
                    continuations_used = 0
                    is_completed = False
                    pending_raw_chunks: List[str] = []
                    pending_chunk_continuation = False
                    while not is_completed:
                        batch_step += 1
                        tdm_logger.debug(f'Generating batch {batch_step} for {filename}...')
                        batch_record: Dict[str, Any] = {'batch': batch_step, 'continuations_used': continuations_used, 'status': 'requesting'}
                        attempt_record['batches'].append(batch_record)
                        req_start = time.time()
                        try:
                            completion = self.client.chat.completions.create(model=self.model_name, messages=messages, temperature=TEMPERATURE, **self.token_limit_options, **llm_request_options(self.conf))
                            wait_if_pause_requested(f'after Step 6 LLM response for {filename}, batch {batch_step}')
                        except Exception as api_err:
                            batch_record['status'] = 'api_error'
                            batch_record['error'] = str(api_err)
                            tdm_logger.error(f'API CALL CRASHED: {str(api_err)}')
                            raise api_err
                        req_duration = time.time() - req_start
                        batch_record['latency'] = round(req_duration, 4)
                        raw_completion_debug = str(completion)
                        if not completion.choices:
                            tdm_logger.error(f'CRITICAL: API returned NO CHOICES.')
                            batch_record['status'] = 'empty_choices'
                            batch_record['api_crash_artifact'] = tdm_logger.save_artifact(f'{filename}_api_crash_try{attempt}.txt', raw_completion_debug)
                            raise ValueError('API returned valid object but zero choices.')
                        choice = completion.choices[0]
                        msg = choice.message
                        raw_content = msg.content
                        finish_reason = choice.finish_reason
                        usage = extract_token_usage(completion)
                        batch_record['finish_reason'] = finish_reason
                        batch_record['content_len'] = len(raw_content) if raw_content else 0
                        batch_record['usage'] = usage
                        batch_record['raw_response_artifact'] = tdm_logger.save_artifact(f'{filename}_raw_response_try{attempt}_batch{batch_step}.txt', raw_content)
                        tdm_logger.log_event('llm_response', {'latency': req_duration, 'batch': batch_step, 'finish_reason': finish_reason, 'content_len': len(raw_content) if raw_content else 0, 'usage': usage})
                        if not raw_content:
                            reasoning_content = getattr(msg, 'reasoning', None)
                            if reasoning_content and '```json' in reasoning_content:
                                tdm_logger.warning("Content is empty, but found JSON in 'reasoning'. Attempting recovery...")
                                import re
                                matches = re.findall('```json\\s*(\\{.*?\\})\\s*```', reasoning_content, re.DOTALL)
                                if matches:
                                    raw_content = matches[-1]
                                    batch_record['recovered_from_reasoning'] = True
                                    batch_record['content_len'] = len(raw_content)
                                    batch_record['raw_response_artifact'] = tdm_logger.save_artifact(f'{filename}_raw_response_try{attempt}_batch{batch_step}_recovered.txt', raw_content)
                                    tdm_logger.info(f'SUCCESS: Recovered {len(raw_content)} chars from reasoning field.')
                        if not raw_content:
                            batch_record['status'] = 'empty_response'
                            tdm_logger.error(f'CRITICAL ERROR: Empty response')
                            raise ValueError(f'Empty response from LLM (Reason: {finish_reason})')
                        is_truncated = is_truncated_finish_reason(finish_reason)
                        pending_raw_chunks.append(raw_content)
                        parse_content = ''.join(pending_raw_chunks) if pending_chunk_continuation or len(pending_raw_chunks) > 1 else raw_content
                        repaired_json_str = repair_json(parse_content)
                        batch_record['repair_changed_output'] = repaired_json_str != parse_content
                        if len(pending_raw_chunks) > 1:
                            batch_record['merged_raw_chunks'] = len(pending_raw_chunks)
                        try:
                            parsed_dict = json.loads(repaired_json_str)
                            if isinstance(parsed_dict, list):
                                parsed_dict = {'experiments': parsed_dict}
                            pending_invalid_payload = parsed_dict
                            batch_result = ExtractionResult.model_validate(parsed_dict, context={'valid_ids': valid_ids})
                            pending_invalid_payload = None
                            pending_repair_base_payload = None
                            new_exps = batch_result.experiments
                            accumulated_experiments.extend(new_exps)
                            pending_raw_chunks = []
                            pending_chunk_continuation = False
                            batch_record['parsed_experiments'] = len(new_exps)
                            batch_record['status'] = 'parsed'
                            tdm_logger.info(f'Batch {batch_step}: Extracted {len(new_exps)} experiments. Total so far: {len(accumulated_experiments)}')
                        except ValidationError as ve:
                            if is_truncated:
                                tdm_logger.warning('Truncated Step 6 output is not schema-complete yet; requesting exact JSON continuation...')
                                attempt_record['truncated_batches'] += 1
                                if continuations_used >= self.max_continuations:
                                    batch_record['status'] = 'continuation_limit_reached'
                                    batch_record['max_continuations'] = self.max_continuations
                                    break
                                continuations_used += 1
                                pending_chunk_continuation = True
                                messages.append({'role': 'assistant', 'content': raw_content})
                                continuation_prompt = 'SYSTEM NOTICE: Your previous JSON was cut off by the output token limit.\nContinue from the exact character where it stopped. Do not restart, do not repeat already emitted JSON, and close the JSON only after all remaining content is complete.'
                                batch_record['status'] = 'truncated_json_continuation_requested'
                                batch_record['continuation_prompt_chars'] = len(continuation_prompt)
                                batch_record['continuation_number'] = continuations_used
                                messages.append({'role': 'user', 'content': continuation_prompt})
                                continue
                            pending_validation_report = self._build_validation_report(ve)
                            batch_record['status'] = 'validation_error'
                            batch_record['validation_report'] = pending_validation_report['structured']
                            if accumulated_experiments:
                                pending_repair_base_payload = {'experiments': [exp.model_dump(by_alias=True, exclude_none=True) for exp in accumulated_experiments] + list(parsed_dict.get('experiments', []))}
                            else:
                                pending_repair_base_payload = parsed_dict
                            raise ve
                        except json.JSONDecodeError as je:
                            if is_truncated:
                                tdm_logger.warning('Truncated Step 6 output is not valid JSON yet; requesting exact JSON continuation...')
                                attempt_record['truncated_batches'] += 1
                                if continuations_used >= self.max_continuations:
                                    batch_record['status'] = 'continuation_limit_reached'
                                    batch_record['max_continuations'] = self.max_continuations
                                    break
                                continuations_used += 1
                                pending_chunk_continuation = True
                                messages.append({'role': 'assistant', 'content': raw_content})
                                continuation_prompt = 'SYSTEM NOTICE: Your previous JSON was cut off by the output token limit.\nContinue from the exact character where it stopped. Do not restart, do not repeat already emitted JSON, and close the JSON only after all remaining content is complete.'
                                batch_record['status'] = 'truncated_json_continuation_requested'
                                batch_record['continuation_prompt_chars'] = len(continuation_prompt)
                                batch_record['continuation_number'] = continuations_used
                                messages.append({'role': 'user', 'content': continuation_prompt})
                                continue
                            batch_record['status'] = 'json_parse_error'
                            batch_record['error'] = str(je)
                            tdm_logger.error(f'JSON PARSE ERROR. Raw LLM Output start: {raw_content[:100]}...')
                            tdm_logger.log_event('parse_fail', {'error': str(je)}, severity='ERROR')
                            raise je
                        if is_truncated:
                            tdm_logger.warning(f'Warning: Output truncated ({finish_reason}). Initiating continuation...')
                            attempt_record['truncated_batches'] += 1
                            if continuations_used >= self.max_continuations:
                                batch_record['status'] = 'continuation_limit_reached'
                                batch_record['max_continuations'] = self.max_continuations
                                tdm_logger.error(f'Continuation limit reached before extraction completed ({continuations_used}/{self.max_continuations}).')
                                break
                            continuations_used += 1
                            messages.append({'role': 'assistant', 'content': raw_content})
                            continuation_prompt = f'SYSTEM NOTICE: Your previous output was truncated due to token limits.\nWe have successfully parsed and saved the first {len(accumulated_experiments)} experiments from the total set.\nACTION REQUIRED: Please continue extracting the REMAINING experiments starting immediately after the ones you just listed.\nDo not repeat the ones already extracted.\nOutput a valid JSON object containing the `experiments` list for the rest of the data.'
                            batch_record['status'] = 'truncated_continuation_requested'
                            batch_record['continuation_prompt_chars'] = len(continuation_prompt)
                            batch_record['continuation_number'] = continuations_used
                            messages.append({'role': 'user', 'content': continuation_prompt})
                        else:
                            batch_record['status'] = 'completed'
                            is_completed = True
                    if not is_completed:
                        partial_dump = {'experiments': [exp.model_dump(by_alias=True, exclude_none=True) for exp in accumulated_experiments]}
                        attempt_record['partial_artifact'] = tdm_logger.save_artifact(f'{filename}_partial_truncated_try{attempt}.json', partial_dump)
                        raise ValueError(f'Continuation limit reached before the extraction response completed; captured {len(accumulated_experiments)} partial experiments.')
                    final_obj = ExtractionResult(experiments=accumulated_experiments)
                duration = time.time() - start_time
                exp_count = len(final_obj.experiments)
                if active_repair_context:
                    self._finalize_targeted_repair_followup(active_repair_context, 'success')
                tdm_logger.info(f'Success ({filename}): Extracted {exp_count} experiments in {duration:.2f}s')
                tdm_logger.log_event('doc_finish', {'status': 'success', 'experiments_count': exp_count, 'total_duration': duration})
                doc_finish_emitted = True
                attempt_record['status'] = 'success'
                attempt_record['duration'] = round(duration, 4)
                attempt_record['elapsed_total'] = round(duration, 4)
                attempt_record['experiments_count'] = exp_count
                attempt_history.append(attempt_record)
                break
            except ValidationError as e:
                validation_report = pending_validation_report or self._build_validation_report(e)
                repair_base_payload = pending_repair_base_payload or pending_invalid_payload
                tdm_logger.warning(f'Validation Failed ({filename} - Attempt {attempt})')
                tdm_logger.log_event('validation_error', {'attempt': attempt, 'error_summary': validation_report['structured']['short_summary'], 'error_count': validation_report['structured']['total_errors'], 'field_target_count': validation_report['structured'].get('field_target_count', 0), 'missing_field_count': validation_report['structured'].get('missing_field_count', 0), 'categories': [item['code'] for item in validation_report['structured']['categories']]}, severity='WARN')
                attempt_record['status'] = 'validation_error'
                attempt_record['elapsed_total'] = round(time.time() - start_time, 4)
                attempt_record['error'] = validation_report['summary_text']
                attempt_record['validation_report'] = validation_report['structured']
                if repair_base_payload is not None:
                    attempt_record['invalid_candidate_json_artifact'] = tdm_logger.save_artifact(f'{filename}_invalid_candidate_try{attempt}.json', repair_base_payload)
                attempt_record['validation_summary_artifact'] = tdm_logger.save_artifact(f'{filename}_validation_fail_try{attempt}.txt', validation_report['summary_text'])
                attempt_record['validation_json_artifact'] = tdm_logger.save_artifact(f'{filename}_validation_fail_try{attempt}.json', validation_report['structured'])
                if active_repair_context:
                    self._finalize_targeted_repair_followup(active_repair_context, 'validation_error', validation_report=validation_report)
                if attempt < MAX_RETRIES and repair_base_payload is not None:
                    retry_context = self._build_targeted_repair_context(filename=filename, md_content=md_content, invalid_payload=repair_base_payload, validation_report=validation_report, source_attempt=attempt)
                    retry_context['source_attempt_record'] = attempt_record
                    attempt_record['next_targeted_repair'] = copy.deepcopy(retry_context['metadata'])
                    tdm_logger.log_event('targeted_repair_prepared', {'attempt': attempt, 'source_attempt': attempt, 'target_field_count': retry_context['metadata'].get('target_field_count', 0), 'missing_field_count': retry_context['metadata'].get('missing_field_count', 0), 'prompt_artifact': retry_context['metadata'].get('prompt_artifact'), 'candidate_json_artifact': retry_context['metadata'].get('candidate_json_artifact')})
                attempt_history.append(attempt_record)
            except Exception as e:
                err_str = str(e)
                tb_str = traceback.format_exc()
                tdm_logger.error(f'System Error: {filename} -> {err_str}')
                tdm_logger.log_event('system_error', {'attempt': attempt, 'error': err_str}, severity='ERROR')
                attempt_record['status'] = 'system_error'
                attempt_record['elapsed_total'] = round(time.time() - start_time, 4)
                attempt_record['error'] = f'{err_str}\n{tb_str}'
                attempt_history.append(attempt_record)
                is_retryable = False
                retry_keywords = ['Empty response', '502', '500', '503', 'Connection error', 'timeout', 'JSON', 'refusal', 'truncated', 'continuation limit']
                if any((k.lower() in err_str.lower() for k in retry_keywords)):
                    is_retryable = True
                if is_retryable and attempt < MAX_RETRIES:
                    if active_repair_context:
                        retry_context = active_repair_context
                    tdm_logger.warning(f'Retryable Error detected. Waiting 5s before attempt {attempt + 1}...')
                    time.sleep(5)
                else:
                    if active_repair_context:
                        self._finalize_targeted_repair_followup(active_repair_context, 'system_error')
                    tdm_logger.error(f'Fatal Error or Max Retries reached. Stopping processing for {filename}.')
                    if attempt == MAX_RETRIES and any((kw in err_str.lower() for kw in ['timeout', 'read_timeout'])):
                        with self.skip_lock:
                            self.timeout_skipped.append(filename)
                    break
        if not doc_finish_emitted:
            last_history = attempt_history[-1] if attempt_history else {}
            tdm_logger.log_event('doc_finish', {'status': 'failed', 'final_status': 'failed', 'reason': last_history.get('status', 'unknown_error'), 'error': last_history.get('error'), 'total_duration': time.time() - start_time}, severity='ERROR')
        self._save_output(filename, final_obj, attempt_history, len(valid_ids))

    def _collect_history_metrics(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        validation_error_counts: Dict[str, int] = {}
        validation_attempts = 0
        total_batches = 0
        truncated_batches = 0
        targeted_repair_attempts = 0
        field_target_error_total = 0
        missing_field_error_total = 0
        resolved_targeted_field_total = 0
        remaining_targeted_field_total = 0
        for item in history:
            if not isinstance(item, dict):
                continue
            total_batches += len(item.get('batches', []))
            truncated_batches += int(item.get('truncated_batches', 0) or 0)
            if item.get('repair_request'):
                targeted_repair_attempts += 1
            validation_report = item.get('validation_report') or {}
            categories = validation_report.get('categories', []) if isinstance(validation_report, dict) else []
            if categories:
                validation_attempts += 1
            if isinstance(validation_report, dict):
                field_target_error_total += int(validation_report.get('field_target_count', 0) or 0)
                missing_field_error_total += int(validation_report.get('missing_field_count', 0) or 0)
            for category in categories:
                code = category.get('code')
                count = int(category.get('count', 0) or 0)
                if code:
                    validation_error_counts[code] = validation_error_counts.get(code, 0) + count
            repair_followup = item.get('after_targeted_repair') or {}
            if isinstance(repair_followup, dict):
                resolved_targeted_field_total += int(repair_followup.get('resolved_count', 0) or 0)
                remaining_targeted_field_total += int(repair_followup.get('remaining_count', 0) or 0)
        return {'validation_attempts': validation_attempts, 'validation_error_counts': validation_error_counts, 'validation_error_categories': sorted(validation_error_counts.keys()), 'total_batches': total_batches, 'truncated_batches': truncated_batches, 'targeted_repair_attempts': targeted_repair_attempts, 'field_target_error_total': field_target_error_total, 'missing_field_error_total': missing_field_error_total, 'resolved_targeted_field_total': resolved_targeted_field_total, 'remaining_targeted_field_total': remaining_targeted_field_total, 'had_validation_error': validation_attempts > 0, 'had_truncation': truncated_batches > 0}

    def _record_document_summary(self, filename: str, result: Optional[ExtractionResult], history: List[Dict[str, Any]], valid_count: int, final_status: str):
        output_path = os.path.join(OUTPUT_DIR, filename.replace('.md', '.json'))
        fail_path = output_path.replace('.json', '_FAILURE_REPORT.txt')
        metrics = self._collect_history_metrics(history)
        total_duration = 0.0
        if history:
            last_elapsed = history[-1].get('elapsed_total')
            if isinstance(last_elapsed, (int, float)):
                total_duration = round(float(last_elapsed), 4)
        summary = {'file': filename, 'final_status': final_status, 'attempts': len(history), 'valid_ids_count': valid_count, 'experiments_count': len(result.experiments) if result else 0, 'total_duration': total_duration, 'output_json_exists': os.path.exists(output_path), 'output_json_path': output_path if os.path.exists(output_path) else None, 'failure_report_exists': os.path.exists(fail_path), 'failure_report_path': fail_path if os.path.exists(fail_path) else None, 'timeout_skipped': filename in self.timeout_skipped, **metrics}
        with self.summary_lock:
            self.document_summaries.append(summary)

    def _write_batch_summary(self, total_files: int, targets: Optional[List[str]], crashed_files: List[str]):
        with self.summary_lock:
            doc_summaries = sorted(self.document_summaries, key=lambda item: item['file'].lower())
        final_status_counts: Dict[str, int] = {}
        for item in doc_summaries:
            final_status = item.get('final_status', 'unknown')
            final_status_counts[final_status] = final_status_counts.get(final_status, 0) + 1
        validation_retry_files = [item['file'] for item in doc_summaries if item.get('had_validation_error')]
        targeted_repair_files = [item['file'] for item in doc_summaries if item.get('targeted_repair_attempts')]
        truncation_files = [item['file'] for item in doc_summaries if item.get('had_truncation')]
        slowest_files = sorted(doc_summaries, key=lambda item: item.get('total_duration', 0.0), reverse=True)[:5]
        missing_field_error_total = sum((int(item.get('missing_field_error_total', 0) or 0) for item in doc_summaries))
        remaining_targeted_field_total = sum((int(item.get('remaining_targeted_field_total', 0) or 0) for item in doc_summaries))
        payload = {'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'provider': self.provider_name, 'model': self.model_name, 'mode': self.output_mode, 'targeted_run': bool(targets), 'target_count': len(targets or []), 'total_files': total_files, 'processed_files': len(doc_summaries), 'final_status_counts': final_status_counts, 'validation_retry_files': validation_retry_files, 'targeted_repair_files': targeted_repair_files, 'truncation_files': truncation_files, 'missing_field_error_total': missing_field_error_total, 'remaining_targeted_field_total': remaining_targeted_field_total, 'timeout_skipped': list(self.timeout_skipped), 'worker_crashes': crashed_files, 'slowest_files': slowest_files, 'documents': doc_summaries}
        summary_json_path = os.path.join(self.log_dir, 'step6_batch_summary.json')
        manifest_json_path = os.path.join(self.log_dir, 'step6_doc_manifest.json')
        summary_txt_path = os.path.join(self.log_dir, 'step6_batch_summary.txt')
        with open(summary_json_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        with open(manifest_json_path, 'w', encoding='utf-8') as f:
            json.dump({'documents': doc_summaries}, f, indent=2, ensure_ascii=False)
        summary_lines = ['Step 6 Batch Summary', f"Generated At: {payload['generated_at']}", f'Provider/Model: {self.provider_name} / {self.model_name}', f'Files Targeted: {total_files}', f'Files Processed: {len(doc_summaries)}', f'Final Status Counts: {json.dumps(final_status_counts, ensure_ascii=False)}', f'Files With Validation Retries: {len(validation_retry_files)}', f'Files With Targeted Repairs: {len(targeted_repair_files)}', f'Missing Field Errors Seen: {missing_field_error_total}', f'Remaining Targeted Fields After Repairs: {remaining_targeted_field_total}', f'Files With Truncation: {len(truncation_files)}', f'Timeout-Skipped Files: {len(self.timeout_skipped)}', f'Worker Crashes: {len(crashed_files)}', 'Slowest Files:']
        for item in slowest_files:
            summary_lines.append(f"- {item['file']} | {item['total_duration']}s | status={item['final_status']} | attempts={item['attempts']}")
        with open(summary_txt_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(summary_lines) + '\n')
        tdm_logger.log_event('step6_batch_summary', {'total_files': total_files, 'processed_files': len(doc_summaries), 'final_status_counts': final_status_counts, 'validation_retry_count': len(validation_retry_files), 'targeted_repair_file_count': len(targeted_repair_files), 'missing_field_error_total': missing_field_error_total, 'remaining_targeted_field_total': remaining_targeted_field_total, 'truncation_file_count': len(truncation_files), 'worker_crash_count': len(crashed_files)})
        tdm_logger.info(f'Step 6 batch summary saved to {summary_json_path} and {summary_txt_path}')

    def _save_output(self, filename: str, result: Optional[ExtractionResult], history: List, valid_count: int, final_status: Optional[str]=None):
        output_path = os.path.join(OUTPUT_DIR, filename.replace('.md', '.json'))
        fail_path = output_path.replace('.json', '_FAILURE_REPORT.txt')
        resolved_final_status = final_status or ('success' if result else 'failed')
        history_metrics = self._collect_history_metrics(history)
        extraction_data = None
        if result:
            extraction_data = result.model_dump(by_alias=True, exclude_none=True)
        output_data = {'metadata': {'file': filename, 'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'), 'provider': self.provider_name, 'model': self.model_name, 'mode': self.output_mode, 'valid_ids_count': valid_count, 'attempts': len(history), 'final_status': resolved_final_status, 'manifest_version': 3, 'had_validation_error': history_metrics['had_validation_error'], 'validation_error_categories': history_metrics['validation_error_categories'], 'validation_error_counts': history_metrics['validation_error_counts'], 'targeted_repair_attempts': history_metrics['targeted_repair_attempts'], 'field_target_error_total': history_metrics['field_target_error_total'], 'missing_field_error_total': history_metrics['missing_field_error_total'], 'resolved_targeted_field_total': history_metrics['resolved_targeted_field_total'], 'remaining_targeted_field_total': history_metrics['remaining_targeted_field_total'], 'had_truncation': history_metrics['had_truncation'], 'truncated_batches': history_metrics['truncated_batches'], 'total_batches': history_metrics['total_batches'], 'process_history': history}, 'extraction': extraction_data}
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        if resolved_final_status != 'failed':
            if os.path.exists(fail_path):
                os.remove(fail_path)
        else:
            last_err = history[-1].get('error', 'Unknown Error') if history else 'No history'
            validation_report = history[-1].get('validation_report') if history else None
            with open(fail_path, 'w', encoding='utf-8') as f:
                f.write(f'Extraction Failed for {filename}\n\nProvider: {self.provider_name}\nError:\n{last_err}\n')
                if validation_report:
                    f.write('\nValidation Categories:\n')
                    for item in validation_report.get('categories', []):
                        f.write(f"- {item.get('code')} x{item.get('count')}: {item.get('label')}\n")
                    missing_fields = validation_report.get('missing_fields', [])
                    if missing_fields:
                        f.write('\nMissing Fields:\n')
                        for item in missing_fields:
                            f.write(f"- path={item.get('path')} | field={item.get('field_name')} | scope={item.get('scope_hierarchy_text')}\n")
                next_targeted_repair = history[-1].get('next_targeted_repair') if history else None
                if next_targeted_repair:
                    f.write('\nNext Targeted Repair Prompt Artifact:\n')
                    f.write(f"- {next_targeted_repair.get('prompt_artifact')}\n")
        self._record_document_summary(filename, result, history, valid_count, resolved_final_status)

    def run_batch(self, targets: List[str]=None):
        all_files = glob.glob(os.path.join(INPUT_DIR, '*.md'))
        if targets:
            target_bases = {os.path.basename(t).replace('.json', '') for t in targets}
            files = [f for f in all_files if os.path.basename(f).replace('.md', '') in target_bases]
            tdm_logger.info(f'Reflexion Mode: Processing {len(files)} targeted files.')
        else:
            files = all_files
        total_files = len(files)
        if total_files == 0:
            if targets:
                target_preview = ', '.join(sorted((os.path.basename(t) for t in targets))[:5])
                suffix = '' if len(targets) <= 5 else f' ... (+{len(targets) - 5} more)'
                error_msg = f'Reflexion target set resolved to 0 markdown files: {target_preview}{suffix}'
                tdm_logger.error(error_msg)
                raise RuntimeError(error_msg)
            tdm_logger.warning('No files found to process.')
            return
        tdm_logger.info(f'Batch Job Started. Files: {total_files}, Workers: {MAX_WORKERS}')

        def _thread_worker(file_path):
            filename = os.path.basename(file_path)
            with tdm_logger.file_context(filename):
                try:
                    self.process_document(file_path)
                except Exception as e:
                    tdm_logger.error(f'Thread Crash: {filename} - {e}')
                    tdm_logger.log_event('thread_crash', {'error': str(e)}, severity='ERROR')
                    raise
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_file = {executor.submit(_thread_worker, f): os.path.basename(f) for f in files}
            crashed_files: List[str] = []
            for future in tqdm(concurrent.futures.as_completed(future_to_file), total=total_files, unit='file', desc='Processing', dynamic_ncols=True):
                try:
                    future.result()
                except Exception:
                    crashed_files.append(future_to_file[future])
        self._write_batch_summary(total_files, targets, crashed_files)
        if crashed_files:
            crashed_preview = ', '.join(crashed_files[:5])
            suffix = '' if len(crashed_files) <= 5 else f' ... (+{len(crashed_files) - 5} more)'
            error_msg = f'Worker crashes detected for {len(crashed_files)} file(s): {crashed_preview}{suffix}'
            tdm_logger.error(error_msg)
            raise RuntimeError(error_msg)
        tdm_logger.info('All files processed.')
        if self.timeout_skipped:
            timeout_report_path = os.path.join(OUTPUT_DIR, 'step2_timeout_skipped.json')
            with open(timeout_report_path, 'w', encoding='utf-8') as f:
                json.dump({'timeout_files': self.timeout_skipped}, f, indent=2, ensure_ascii=False)
            tdm_logger.warning(f'Saved {len(self.timeout_skipped)} timeout-skipped files to {timeout_report_path}')
if __name__ == '__main__':
    tdm_logger.setup_tdm_logging('Step6_Extraction')
    parser = argparse.ArgumentParser()
    parser.add_argument('--targets', help='Path to JSON file containing list of filenames to process')
    parser.add_argument('--feedback', help='Path to JSON file containing feedback map')
    parser.add_argument('--provider', help='Override LLM provider (e.g., openai, deepseek)', default=None)
    parser.add_argument('--model', help='Override Model Name', default=None)
    args = parser.parse_args()
    targets_list = None
    feedback_data = None
    active_config = copy.deepcopy(CURRENT_CONF)
    active_provider = SELECTED_PROVIDER
    if args.provider:
        if args.provider in LLM_CONFIGS:
            active_config = copy.deepcopy(LLM_CONFIGS[args.provider])
            active_provider = args.provider
        else:
            tdm_logger.warning(f'Provider {args.provider} not found in config. Using default.')
    if args.model:
        active_config['model'] = args.model
        active_config = apply_llm_runtime_defaults(active_provider, 'STEP2', active_config, 'extraction_result', _extraction_schema)
        tdm_logger.info(f"🔧 Configuration Overridden: Using Model [{active_config['model']}]")
    if args.targets and os.path.exists(args.targets):
        with open(args.targets, 'r', encoding='utf-8') as f:
            targets_list = json.load(f)
    if args.feedback and os.path.exists(args.feedback):
        with open(args.feedback, 'r', encoding='utf-8') as f:
            feedback_data = json.load(f)
        if isinstance(feedback_data, dict) and isinstance(feedback_data.get('feedback'), dict):
            feedback_data = feedback_data['feedback']
    agent = ScientificMiningAgent(feedback_map=feedback_data, config=active_config, provider_name=active_provider)
    agent.run_batch(targets=targets_list)
