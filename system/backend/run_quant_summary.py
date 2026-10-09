import glob
import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
import pandas as pd
STEP_CATALOG = [(1, 'Step 1: PDF Parsing', '00_input_pdfs', '01_mineru_raw', ('.pdf',), None, True), (2, 'Step 2: Data Cleaning', '01_mineru_raw', '02_intermediate_json', None, ('.json',), False), (3, 'Step 3: ID Injection', '02_intermediate_json', '03_tagged_content', ('.json',), ('.json', '.md'), False), (4, 'Step 4: Smart Filtering', '03_tagged_content', '04_relevance_filter', ('.md',), ('.json',), False), (5, 'Step 5: Post Processing', '04_relevance_filter', '05_cleaned_markdown', ('.json',), ('.md',), False), (6, 'Step 6: Extraction', '05_cleaned_markdown', '06_extracted_json', ('.md',), ('.json',), False), (7, 'Step 7: Audit', '06_extracted_json', '07_audited_db', ('.json',), ('.json',), False), (8, 'Step 8: Final Summary', '07_audited_db', '08_final_excel', ('.json',), ('.xlsx',), False)]
TELEMETRY_STEP_TO_INDEX = {'Step4_Filter': 4, 'Step6_Extraction': 6, 'Step7_Audit': 7, 'Step8_Postprocess': 8}
TRUNCATED_FINISH_REASONS = {'length', 'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'token_limit', 'content_filter_length'}
STEP6_SUCCESS_STATUSES = {'success', 'no_data'}
STEP7_PASS_STATUSES = {'audited_clean', 'audited_patched', 'audit_skipped'}
STEP7_PATCH_FAILURE_STATUSES = {'audit_failed_patch_transaction', 'audit_failed_schema_corruption'}

def _read_json(path: str) -> Any:
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
            return json.load(f)
    except Exception:
        return None

def _read_text_len(path: str) -> int:
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
            return len(f.read())
    except Exception:
        return 0

def _iter_telemetry_events(run_dir: str) -> List[Dict[str, Any]]:
    log_dir = os.path.join(run_dir, 'logs')
    events: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(log_dir, 'telemetry_*.jsonl'))):
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    events.append(item)
    return events

def _payload(event: Dict[str, Any]) -> Dict[str, Any]:
    value = event.get('payload')
    return value if isinstance(value, dict) else {}

def _to_float(value: Any, default: float=0.0) -> float:
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def _to_int(value: Any, default: int=0) -> int:
    try:
        if value is None or value == '':
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default

def _rate(numerator: float, denominator: float) -> Optional[float]:
    if not denominator:
        return None
    return round(float(numerator) / float(denominator), 6)

def _pct(numerator: float, denominator: float) -> Optional[float]:
    rate = _rate(numerator, denominator)
    return None if rate is None else round(rate * 100.0, 4)

def _round(value: Any, digits: int=4) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return value

def _is_truncated_finish_reason(value: Any) -> bool:
    if value is None:
        return False
    normalized = str(value).strip().lower().replace('-', '_')
    return normalized in TRUNCATED_FINISH_REASONS or 'max_token' in normalized

def _count_files(path: str, suffixes: Optional[Tuple[str, ...]]=None, recursive: bool=False) -> int:
    if not os.path.isdir(path):
        return 0
    suffixes = tuple((s.lower() for s in suffixes)) if suffixes else None
    count = 0
    iterator: Iterable[str]
    if recursive:
        iterator = (os.path.join(root, name) for root, _, files in os.walk(path) for name in files)
    else:
        iterator = (os.path.join(path, name) for name in os.listdir(path))
    for file_path in iterator:
        if not os.path.isfile(file_path):
            continue
        name = os.path.basename(file_path).lower()
        if '_failure' in name or name.endswith('_failure.txt'):
            continue
        if suffixes is None or name.endswith(suffixes):
            count += 1
    return count

def _normalize_usage(payload: Dict[str, Any]) -> Dict[str, int]:
    usage = payload.get('usage')
    if not isinstance(usage, dict):
        usage = {}
    prompt = _to_int(usage.get('prompt_tokens', usage.get('input_tokens', payload.get('prompt_tokens', payload.get('input_tokens')))))
    completion = _to_int(usage.get('completion_tokens', usage.get('output_tokens', payload.get('completion_tokens', payload.get('output_tokens')))))
    total = _to_int(usage.get('total_tokens', payload.get('total_tokens')))
    if total <= 0 and (prompt > 0 or completion > 0):
        total = prompt + completion
    return {'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': total}

def _load_step6_docs(run_dir: str) -> List[Dict[str, Any]]:
    docs = []
    for path in sorted(glob.glob(os.path.join(run_dir, '06_extracted_json', '*.json'))):
        payload = _read_json(path)
        if not isinstance(payload, dict) or 'metadata' not in payload:
            continue
        metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
        docs.append({'path': path, 'file': os.path.basename(path), 'metadata': metadata, 'payload': payload})
    return docs

def _load_step7_docs(run_dir: str) -> List[Dict[str, Any]]:
    docs = []
    for folder, location in (('07_audited_db', 'audited_db'), ('07_quarantine', 'quarantine')):
        for path in sorted(glob.glob(os.path.join(run_dir, folder, '*.json'))):
            payload = _read_json(path)
            if not isinstance(payload, dict):
                continue
            metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
            docs.append({'path': path, 'file': os.path.basename(path), 'location': location, 'metadata': metadata, 'payload': payload})
    return docs

def _collect_step7_operation_metrics(run_dir: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    action_type_counts: Counter = Counter()
    issue_type_counts: Counter = Counter()
    report_patch_count = 0
    for path in sorted(glob.glob(os.path.join(run_dir, '07_audit_logs', '*_audit_report.json'))):
        payload = _read_json(path)
        if not isinstance(payload, dict):
            continue
        patches = payload.get('patches')
        if not isinstance(patches, list):
            continue
        report_patch_count += len(patches)
        for patch in patches:
            if not isinstance(patch, dict):
                continue
            action_type = patch.get('action_type')
            issue_type = patch.get('issue_type')
            if action_type:
                action_type_counts[str(action_type)] += 1
            if issue_type:
                issue_type_counts[str(issue_type)] += 1
    audit_started_files = set()
    llm_request_count = 0
    llm_response_batch_count = 0
    llm_error_count = 0
    scoring_round_count = 0
    proposed_patch_count = 0
    applied_patch_count = 0
    local_schema_repair_count = 0
    local_schema_repair_fallback = 0
    retry_feedback_handoff_count = 0
    input_error_count = 0
    schema_corruption_count = 0
    patch_transaction_failed_count = 0
    for event in events:
        if event.get('step') != 'Step7_Audit':
            continue
        event_name = event.get('event')
        payload = _payload(event)
        if event_name == 'audit_start':
            target = payload.get('target') or event.get('file')
            if target:
                audit_started_files.add(str(target))
        elif event_name == 'llm_request':
            llm_request_count += 1
        elif event_name == 'llm_response':
            llm_response_batch_count += 1
        elif event_name == 'llm_error':
            llm_error_count += 1
        elif event_name == 'audit_scored':
            scoring_round_count += 1
            proposed_patch_count += _to_int(payload.get('patch_count'))
            if str(payload.get('decision', '')).lower() == 'audit_request_retry':
                retry_feedback_handoff_count += 1
        elif event_name == 'patch_applied':
            applied_patch_count += _to_int(payload.get('count'))
            local_repairs = payload.get('local_schema_repairs')
            if isinstance(local_repairs, list):
                local_schema_repair_fallback += len(local_repairs)
        elif event_name == 'local_patch_schema_repair':
            local_schema_repair_count += _to_int(payload.get('count'))
        elif event_name == 'audit_input_error':
            input_error_count += 1
        elif event_name == 'schema_corruption':
            schema_corruption_count += 1
        elif event_name == 'patch_transaction_failed':
            patch_transaction_failed_count += 1
    if local_schema_repair_count <= 0:
        local_schema_repair_count = local_schema_repair_fallback
    if proposed_patch_count <= 0:
        proposed_patch_count = report_patch_count
    total_operation_count = llm_request_count + scoring_round_count + proposed_patch_count + local_schema_repair_count + retry_feedback_handoff_count
    return {'audit_started_file_count': len(audit_started_files), 'llm_request_count': llm_request_count, 'llm_response_batch_count': llm_response_batch_count, 'llm_error_count': llm_error_count, 'scoring_round_count': scoring_round_count, 'proposed_patch_operation_count': proposed_patch_count, 'applied_patch_operation_count': applied_patch_count, 'local_schema_repair_operation_count': local_schema_repair_count, 'retry_feedback_handoff_count': retry_feedback_handoff_count, 'input_error_count': input_error_count, 'schema_corruption_count': schema_corruption_count, 'patch_transaction_failed_count': patch_transaction_failed_count, 'total_operation_count': total_operation_count, 'action_type_counts': action_type_counts, 'issue_type_counts': issue_type_counts}

def _load_step5_stats(run_dir: str) -> Dict[str, Any]:
    path = os.path.join(run_dir, '05_reports', 'full_pipeline_report.csv')
    if not os.path.exists(path):
        return {'available': False, 'path': path}
    try:
        df = pd.read_csv(path)
    except Exception:
        return {'available': False, 'path': path}

    def col_sum(name: str) -> int:
        if name not in df.columns:
            return 0
        return int(pd.to_numeric(df[name], errors='coerce').fillna(0).sum())
    total_files = len(df)
    success_files = int((df.get('Status', pd.Series(dtype=str)).astype(str) == 'Success').sum()) if 'Status' in df.columns else 0
    raw_tokens = col_sum('Raw_Tokens')
    inter_tokens = col_sum('Inter_Tokens')
    final_tokens = col_sum('Final_Tokens')
    return {'available': True, 'path': path, 'total_files': total_files, 'success_files': success_files, 'raw_tokens': raw_tokens, 'inter_tokens': inter_tokens, 'final_tokens': final_tokens, 'rule_saved_tokens': max(0, raw_tokens - inter_tokens), 'llm_saved_tokens': max(0, inter_tokens - final_tokens), 'total_saved_tokens': max(0, raw_tokens - final_tokens), 'rule_savings_pct': _pct(raw_tokens - inter_tokens, raw_tokens), 'llm_savings_pct': _pct(inter_tokens - final_tokens, inter_tokens), 'total_savings_pct': _pct(raw_tokens - final_tokens, raw_tokens)}

def _collect_controller_steps(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = defaultdict(lambda: {'controller_runs': 0, 'success_count': 0, 'failed_count': 0, 'duration_s': 0.0})
    for event in events:
        if event.get('step') != 'Controller' or event.get('event') != 'step_finish':
            continue
        payload = _payload(event)
        step_name = str(payload.get('step_name') or '')
        if not step_name:
            continue
        item = result[step_name]
        item['controller_runs'] += 1
        if payload.get('success') is True:
            item['success_count'] += 1
        else:
            item['failed_count'] += 1
        item['duration_s'] += _to_float(payload.get('duration'))
    return result

def _collect_llm_metrics(events: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    metrics: Dict[int, Dict[str, Any]] = defaultdict(lambda: {'llm_requests': 0, 'llm_responses': 0, 'llm_errors': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'usage_response_count': 0, 'truncated_responses': 0, 'content_len': 0, 'requests_by_file': Counter(), 'responses_by_file': Counter(), 'logic_attempt_max_by_file': defaultdict(int)})
    for event in events:
        step_idx = TELEMETRY_STEP_TO_INDEX.get(str(event.get('step') or ''))
        if not step_idx:
            continue
        event_name = event.get('event')
        payload = _payload(event)
        filename = event.get('file') or payload.get('target') or 'global'
        item = metrics[step_idx]
        if event_name == 'llm_request':
            item['llm_requests'] += 1
            item['requests_by_file'][filename] += 1
            logic_attempt = _to_int(payload.get('logic_attempt'), 0)
            item['logic_attempt_max_by_file'][filename] = max(item['logic_attempt_max_by_file'][filename], logic_attempt)
        elif event_name == 'llm_response':
            item['llm_responses'] += 1
            item['responses_by_file'][filename] += 1
            item['content_len'] += _to_int(payload.get('content_len'))
            usage = _normalize_usage(payload)
            if usage['total_tokens'] > 0:
                item['usage_response_count'] += 1
                item['prompt_tokens'] += usage['prompt_tokens']
                item['completion_tokens'] += usage['completion_tokens']
                item['total_tokens'] += usage['total_tokens']
            if _is_truncated_finish_reason(payload.get('finish_reason')):
                item['truncated_responses'] += 1
        elif event_name == 'llm_error':
            item['llm_errors'] += 1
    for item in metrics.values():
        item['request_retry_count'] = sum((max(0, count - 1) for count in item['requests_by_file'].values()))
        item['logic_retry_count'] = sum(item['logic_attempt_max_by_file'].values())
    return metrics

def _estimate_step4_tokens(run_dir: str, events: List[Dict[str, Any]]) -> Dict[str, int]:
    prompt_chars = 0
    prompt_tokens = 0
    for event in events:
        if event.get('step') != 'Step4_Filter':
            continue
        payload = _payload(event)
        if event.get('event') == 'llm_request':
            prompt_chars += _to_int(payload.get('prompt_len_est'))
        elif event.get('event') == 'doc_start':
            prompt_tokens += _to_int(payload.get('approx_tokens'))
    prompt_tokens += prompt_chars // 4
    completion_chars = sum((_read_text_len(path) for path in glob.glob(os.path.join(run_dir, '04_relevance_filter', '*.json'))))
    completion_tokens = completion_chars // 4
    return {'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': prompt_tokens + completion_tokens}

def _estimate_step6_tokens(step6_docs: List[Dict[str, Any]]) -> Dict[str, int]:
    prompt_chars = 0
    completion_chars = 0
    for doc in step6_docs:
        metadata = doc['metadata']
        for attempt in metadata.get('process_history') or []:
            if not isinstance(attempt, dict):
                continue
            prompt_chars += _to_int(attempt.get('prompt_len_est'))
            for batch in attempt.get('batches') or []:
                if isinstance(batch, dict):
                    completion_chars += _to_int(batch.get('content_len'))
            if 'usage' in attempt:
                usage = _normalize_usage({'usage': attempt.get('usage')})
                prompt_chars -= min(prompt_chars, usage['prompt_tokens'] * 4)
                completion_chars -= min(completion_chars, usage['completion_tokens'] * 4)
    prompt_tokens = max(0, prompt_chars // 4)
    completion_tokens = max(0, completion_chars // 4)
    return {'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': prompt_tokens + completion_tokens}

def _estimate_step7_tokens(run_dir: str, events: List[Dict[str, Any]]) -> Dict[str, int]:
    prompt_template_len = _read_text_len(os.path.join(os.path.dirname(__file__), 'PROMPT_STEP3_PATCH.md'))
    request_counts: Counter = Counter()
    for event in events:
        if event.get('step') == 'Step7_Audit' and event.get('event') == 'llm_request':
            request_counts[event.get('file') or 'global'] += 1
    prompt_chars = 0
    for filename, count in request_counts.items():
        if filename == 'global':
            continue
        base = os.path.splitext(os.path.basename(filename))[0]
        md_len = _read_text_len(os.path.join(run_dir, '05_cleaned_markdown', f'{base}.md'))
        json_len = _read_text_len(os.path.join(run_dir, '06_extracted_json', filename))
        prompt_chars += max(1, count) * (prompt_template_len + md_len + json_len)
    completion_chars = 0
    for event in events:
        if event.get('step') == 'Step7_Audit' and event.get('event') == 'llm_response':
            completion_chars += _to_int(_payload(event).get('content_len'))
    prompt_tokens = prompt_chars // 4
    completion_tokens = completion_chars // 4
    return {'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': prompt_tokens + completion_tokens}

def _token_metrics_by_step(run_dir: str, events: List[Dict[str, Any]], llm_metrics: Dict[int, Dict[str, Any]], step6_docs: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    estimates = {4: _estimate_step4_tokens(run_dir, events), 6: _estimate_step6_tokens(step6_docs), 7: _estimate_step7_tokens(run_dir, events)}
    result: Dict[int, Dict[str, Any]] = {}
    for step_idx in (4, 6, 7):
        actual = llm_metrics.get(step_idx, {})
        if _to_int(actual.get('total_tokens')) > 0:
            source = 'actual_api_usage'
            prompt_tokens = _to_int(actual.get('prompt_tokens'))
            completion_tokens = _to_int(actual.get('completion_tokens'))
            total_tokens = _to_int(actual.get('total_tokens'))
        elif estimates.get(step_idx, {}).get('total_tokens', 0) > 0:
            source = 'estimated_chars_div_4'
            prompt_tokens = _to_int(estimates[step_idx].get('prompt_tokens'))
            completion_tokens = _to_int(estimates[step_idx].get('completion_tokens'))
            total_tokens = _to_int(estimates[step_idx].get('total_tokens'))
        else:
            source = 'unavailable'
            prompt_tokens = completion_tokens = total_tokens = 0
        result[step_idx] = {'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': total_tokens, 'token_source': source}
    return result

def _file_yield_metrics(step7_docs: List[Dict[str, Any]], final_database_rows: int) -> Dict[str, Any]:
    audited_docs = [doc for doc in step7_docs if doc['location'] == 'audited_db']
    files_with_rows = 0
    for doc in audited_docs:
        extraction = doc['payload'].get('extraction') if isinstance(doc['payload'], dict) else None
        experiments = extraction.get('experiments') if isinstance(extraction, dict) else []
        has_rows = False
        if isinstance(experiments, list):
            for exp in experiments:
                if isinstance(exp, dict) and exp.get('data'):
                    has_rows = True
                    break
        if has_rows:
            files_with_rows += 1
    return {'audited_db_files': len(audited_docs), 'files_with_output_rows': files_with_rows, 'final_database_rows': final_database_rows, 'file_to_row_yield_rate': _rate(files_with_rows, len(audited_docs))}

def _add_metric(rows: List[Dict[str, Any]], section: str, metric: str, value: Any, unit: str='', notes: str='') -> None:
    rows.append({'section': section, 'metric': metric, 'value': value, 'unit': unit, 'notes': notes})

def generate_run_quant_summary(run_dir: str, output_dir: str, timestamp: int, final_database_path: Optional[str]=None, final_database_rows: int=0, step8_duration_s: Optional[float]=None) -> str:
    os.makedirs(output_dir, exist_ok=True)
    events = _iter_telemetry_events(run_dir)
    controller_steps = _collect_controller_steps(events)
    llm_metrics = _collect_llm_metrics(events)
    step5_stats = _load_step5_stats(run_dir)
    step6_docs = _load_step6_docs(run_dir)
    step7_docs = _load_step7_docs(run_dir)
    step7_operations = _collect_step7_operation_metrics(run_dir, events)
    token_by_step = _token_metrics_by_step(run_dir, events, llm_metrics, step6_docs)
    yield_metrics = _file_yield_metrics(step7_docs, int(final_database_rows or 0))
    state = _read_json(os.path.join(run_dir, 'pipeline_phase_state.json'))
    if isinstance(state, dict) and isinstance(state.get('selected_step_indices'), list):
        selected_set = set((_to_int(value, -1) for value in state.get('selected_step_indices', [])))
    else:
        selected_set = set(range(8))
    step6_total = len(step6_docs)
    step6_success = sum((1 for doc in step6_docs if str(doc['metadata'].get('final_status', '')).lower() in STEP6_SUCCESS_STATUSES))
    step6_validation_docs = sum((1 for doc in step6_docs if bool(doc['metadata'].get('had_validation_error'))))
    step6_targeted_docs = sum((1 for doc in step6_docs if _to_int(doc['metadata'].get('targeted_repair_attempts')) > 0))
    step6_targeted_success = sum((1 for doc in step6_docs if _to_int(doc['metadata'].get('targeted_repair_attempts')) > 0 and str(doc['metadata'].get('final_status', '')).lower() in STEP6_SUCCESS_STATUSES))
    step6_retry_count = sum((max(0, _to_int(doc['metadata'].get('attempts')) - 1) for doc in step6_docs))
    step6_truncation_docs = sum((1 for doc in step6_docs if bool(doc['metadata'].get('had_truncation'))))
    step6_truncated_batches = sum((_to_int(doc['metadata'].get('truncated_batches')) for doc in step6_docs))
    step6_resolved_fields = sum((_to_int(doc['metadata'].get('resolved_targeted_field_total')) for doc in step6_docs))
    step6_remaining_fields = sum((_to_int(doc['metadata'].get('remaining_targeted_field_total')) for doc in step6_docs))
    step7_total = len(step7_docs)
    step7_pass = sum((1 for doc in step7_docs if str(doc['metadata'].get('step3_status', '')).lower() in STEP7_PASS_STATUSES))
    step7_audited_db = sum((1 for doc in step7_docs if doc['location'] == 'audited_db'))
    step7_quarantine = sum((1 for doc in step7_docs if doc['location'] == 'quarantine'))
    step7_patched = sum((1 for doc in step7_docs if str(doc['metadata'].get('step3_status', '')).lower() == 'audited_patched'))
    step7_patch_fail = sum((1 for doc in step7_docs if str(doc['metadata'].get('step3_status', '')).lower() in STEP7_PATCH_FAILURE_STATUSES))
    step7_patch_trigger = sum((1 for doc in step7_docs if _to_int(doc['metadata'].get('step3_patches_applied')) > 0))
    step7_quality_scores = [_to_float(doc['metadata'].get('quality_score')) for doc in step7_docs if doc['metadata'].get('quality_score') is not None]
    audit_retry_files = {event.get('file') for event in events if event.get('step') == 'Step7_Audit' and event.get('event') == 'audit_scored' and (str(_payload(event).get('decision', '')).lower() == 'audit_request_retry') and event.get('file')}
    final_success_files = {doc['file'] for doc in step7_docs if doc['location'] == 'audited_db' and str(doc['metadata'].get('step3_status', '')).lower() in STEP7_PASS_STATUSES}
    audit_retry_success = len(audit_retry_files & final_success_files)
    step7_llm = llm_metrics.get(7, {})
    step7_truncated_responses = _to_int(step7_llm.get('truncated_responses'))
    step7_responses = _to_int(step7_llm.get('llm_responses'))
    status_rows: List[Dict[str, Any]] = []
    for source, counter in (('step6_final_status', Counter((str(doc['metadata'].get('final_status', 'unknown')).lower() for doc in step6_docs))), ('step7_status', Counter((str(doc['metadata'].get('step3_status', 'unknown')).lower() for doc in step7_docs))), ('step7_location', Counter((doc['location'] for doc in step7_docs)))):
        for name, count in sorted(counter.items()):
            status_rows.append({'source': source, 'name': name, 'count': count})
    validation_categories: Counter = Counter()
    for doc in step6_docs:
        counts = doc['metadata'].get('validation_error_counts')
        if isinstance(counts, dict):
            for key, value in counts.items():
                validation_categories[str(key)] += _to_int(value)
    for name, count in sorted(validation_categories.items()):
        status_rows.append({'source': 'step6_validation_category', 'name': name, 'count': count})
    audit_decisions = Counter()
    for event in events:
        if event.get('step') == 'Step7_Audit' and event.get('event') == 'audit_scored':
            decision = _payload(event).get('decision')
            if decision:
                audit_decisions[str(decision).lower()] += 1
    for name, count in sorted(audit_decisions.items()):
        status_rows.append({'source': 'step7_audit_decision', 'name': name, 'count': count})
    for name, count in sorted(step7_operations['action_type_counts'].items()):
        status_rows.append({'source': 'step7_patch_action_type', 'name': name, 'count': count})
    for name, count in sorted(step7_operations['issue_type_counts'].items()):
        status_rows.append({'source': 'step7_patch_issue_type', 'name': name, 'count': count})
    step_rows: List[Dict[str, Any]] = []
    for step_idx, step_name, input_rel, output_rel, input_suffixes, output_suffixes, recursive_input in STEP_CATALOG:
        controller = controller_steps.get(step_name, {})
        llm = llm_metrics.get(step_idx, {})
        tokens = token_by_step.get(step_idx, {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'token_source': 'not_llm_step'})
        duration = _to_float(controller.get('duration_s'))
        controller_runs = _to_int(controller.get('controller_runs'))
        if step_idx == 8 and step8_duration_s is not None and (duration <= 0):
            duration = float(step8_duration_s)
            controller_runs = max(1, controller_runs)
        retry_count = _to_int(llm.get('request_retry_count'))
        if step_idx == 6:
            retry_count = max(retry_count, step6_retry_count)
        elif step_idx == 7:
            retry_count = max(retry_count, _to_int(llm.get('request_retry_count')) + _to_int(llm.get('logic_retry_count')))
        audit_operation_count = step7_operations['total_operation_count'] if step_idx == 7 else 0
        audit_patch_operation_count = step7_operations['proposed_patch_operation_count'] if step_idx == 7 else 0
        audit_scoring_round_count = step7_operations['scoring_round_count'] if step_idx == 7 else 0
        step_rows.append({'step_index': step_idx, 'step_name': step_name, 'selected': step_idx - 1 in selected_set, 'controller_runs': controller_runs, 'success_count': _to_int(controller.get('success_count')) if step_idx != 8 else max(1, _to_int(controller.get('success_count'))), 'failed_count': _to_int(controller.get('failed_count')), 'duration_s': _round(duration, 4), 'input_files': _count_files(os.path.join(run_dir, input_rel), input_suffixes, recursive=recursive_input), 'output_files': _count_files(os.path.join(run_dir, output_rel), output_suffixes, recursive=False), 'llm_requests': _to_int(llm.get('llm_requests')), 'llm_responses': _to_int(llm.get('llm_responses')), 'llm_errors': _to_int(llm.get('llm_errors')), 'retry_count': retry_count, 'truncated_responses': _to_int(llm.get('truncated_responses')), 'audit_operation_count': audit_operation_count, 'audit_scoring_round_count': audit_scoring_round_count, 'audit_patch_operation_count': audit_patch_operation_count, 'prompt_tokens': tokens['prompt_tokens'], 'completion_tokens': tokens['completion_tokens'], 'total_tokens': tokens['total_tokens'], 'token_source': tokens['token_source'], 'success_rate': _rate(_to_int(controller.get('success_count')) if step_idx != 8 else 1, controller_runs or (1 if step_idx == 8 else 0))})
    llm_prompt_tokens = sum((row['prompt_tokens'] for row in step_rows))
    llm_completion_tokens = sum((row['completion_tokens'] for row in step_rows))
    llm_total_tokens = sum((row['total_tokens'] for row in step_rows))
    total_step_duration = sum((_to_float(row['duration_s']) for row in step_rows))
    total_retry_count = sum((_to_int(row['retry_count']) for row in step_rows))
    overall_rows: List[Dict[str, Any]] = []
    _add_metric(overall_rows, 'run', 'run_id', os.path.basename(run_dir))
    _add_metric(overall_rows, 'run', 'run_dir', run_dir)
    _add_metric(overall_rows, 'run', 'generated_at', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    _add_metric(overall_rows, 'run', 'final_database_path', final_database_path or '')
    _add_metric(overall_rows, 'files', 'input_pdf_count', _count_files(os.path.join(run_dir, '00_input_pdfs'), ('.pdf',)))
    _add_metric(overall_rows, 'files', 'tagged_source_file_count', _count_files(os.path.join(run_dir, '03_tagged_content'), ('.json',)))
    _add_metric(overall_rows, 'files', 'step6_processed_file_count', step6_total)
    _add_metric(overall_rows, 'files', 'audited_db_file_count', step7_audited_db)
    _add_metric(overall_rows, 'files', 'quarantine_file_count', step7_quarantine)
    _add_metric(overall_rows, 'files', 'final_database_rows', int(final_database_rows or 0))
    _add_metric(overall_rows, 'files', 'file_to_row_yield_rate', yield_metrics['file_to_row_yield_rate'], 'ratio', 'audited DB files that yielded at least one final row')
    _add_metric(overall_rows, 'timing', 'total_recorded_step_duration_s', _round(total_step_duration), 's')
    for row in step_rows:
        _add_metric(overall_rows, 'timing_by_step', f"step{row['step_index']}_duration_s", row['duration_s'], 's', row['step_name'])
    _add_metric(overall_rows, 'llm_tokens', 'prompt_tokens', llm_prompt_tokens, 'tokens')
    _add_metric(overall_rows, 'llm_tokens', 'completion_tokens', llm_completion_tokens, 'tokens')
    _add_metric(overall_rows, 'llm_tokens', 'total_tokens', llm_total_tokens, 'tokens')
    token_sources = Counter((row['token_source'] for row in step_rows if row['total_tokens']))
    _add_metric(overall_rows, 'llm_tokens', 'token_source_mix', json.dumps(dict(token_sources), ensure_ascii=False))
    if step5_stats.get('available'):
        _add_metric(overall_rows, 'cleaning', 'raw_tokens', step5_stats['raw_tokens'], 'tokens')
        _add_metric(overall_rows, 'cleaning', 'inter_tokens_after_rule_clean', step5_stats['inter_tokens'], 'tokens')
        _add_metric(overall_rows, 'cleaning', 'final_tokens_after_llm_filter', step5_stats['final_tokens'], 'tokens')
        _add_metric(overall_rows, 'cleaning', 'rule_saved_tokens', step5_stats['rule_saved_tokens'], 'tokens')
        _add_metric(overall_rows, 'cleaning', 'llm_filter_saved_tokens', step5_stats['llm_saved_tokens'], 'tokens')
        _add_metric(overall_rows, 'cleaning', 'total_saved_tokens', step5_stats['total_saved_tokens'], 'tokens')
        _add_metric(overall_rows, 'cleaning', 'rule_savings_pct', step5_stats['rule_savings_pct'], '%')
        _add_metric(overall_rows, 'cleaning', 'llm_filter_savings_pct', step5_stats['llm_savings_pct'], '%')
        _add_metric(overall_rows, 'cleaning', 'total_savings_pct', step5_stats['total_savings_pct'], '%')
    else:
        _add_metric(overall_rows, 'cleaning', 'step5_report_available', False, '', step5_stats.get('path', ''))
    _add_metric(overall_rows, 'schema', 'step6_schema_compliance_rate', _rate(step6_success, step6_total), 'ratio', 'final_status in success/no_data')
    _add_metric(overall_rows, 'schema', 'step6_validation_retry_rate', _rate(step6_validation_docs, step6_total), 'ratio')
    _add_metric(overall_rows, 'schema', 'step7_audit_pass_rate', _rate(step7_pass, step7_total), 'ratio')
    _add_metric(overall_rows, 'schema', 'step7_quarantine_rate', _rate(step7_quarantine, step7_total), 'ratio')
    _add_metric(overall_rows, 'repair', 'step6_targeted_repair_trigger_rate', _rate(step6_targeted_docs, step6_total), 'ratio')
    _add_metric(overall_rows, 'repair', 'step6_targeted_repair_success_rate', _rate(step6_targeted_success, step6_targeted_docs), 'ratio')
    _add_metric(overall_rows, 'repair', 'step6_targeted_field_resolution_rate', _rate(step6_resolved_fields, step6_resolved_fields + step6_remaining_fields), 'ratio')
    _add_metric(overall_rows, 'repair', 'step7_patch_trigger_rate', _rate(step7_patch_trigger, step7_total), 'ratio')
    _add_metric(overall_rows, 'repair', 'step7_patch_success_rate', _rate(step7_patched, step7_patched + step7_patch_fail), 'ratio')
    _add_metric(overall_rows, 'repair', 'reflexion_retry_trigger_rate', _rate(len(audit_retry_files), step7_total), 'ratio')
    _add_metric(overall_rows, 'repair', 'reflexion_retry_success_rate', _rate(audit_retry_success, len(audit_retry_files)), 'ratio')
    audit_operation_note = 'LLM audit requests + audit scoring rounds + proposed patch operations + local schema repairs + Reflexion feedback handoffs'
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_total_operation_count', step7_operations['total_operation_count'], 'operations', audit_operation_note)
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_started_file_count', step7_operations['audit_started_file_count'], 'files')
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_llm_request_count', step7_operations['llm_request_count'], 'requests')
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_llm_response_batch_count', step7_operations['llm_response_batch_count'], 'batches')
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_scoring_round_count', step7_operations['scoring_round_count'], 'rounds')
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_proposed_patch_operation_count', step7_operations['proposed_patch_operation_count'], 'patches')
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_applied_patch_operation_count', step7_operations['applied_patch_operation_count'], 'patches')
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_local_schema_repair_operation_count', step7_operations['local_schema_repair_operation_count'], 'repairs')
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_reflexion_feedback_handoff_count', step7_operations['retry_feedback_handoff_count'], 'handoffs')
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_llm_error_count', step7_operations['llm_error_count'], 'errors')
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_schema_corruption_count', step7_operations['schema_corruption_count'], 'events')
    _add_metric(overall_rows, 'audit_operations', 'step7_audit_patch_transaction_failed_count', step7_operations['patch_transaction_failed_count'], 'events')
    _add_metric(overall_rows, 'truncation', 'step6_truncation_file_rate', _rate(step6_truncation_docs, step6_total), 'ratio')
    _add_metric(overall_rows, 'truncation', 'step6_truncated_batches', step6_truncated_batches, 'batches')
    _add_metric(overall_rows, 'truncation', 'step7_truncated_response_rate', _rate(step7_truncated_responses, step7_responses), 'ratio')
    _add_metric(overall_rows, 'retry', 'total_retry_count', total_retry_count)
    _add_metric(overall_rows, 'quality', 'avg_step3_quality_score', _round(sum(step7_quality_scores) / len(step7_quality_scores), 4) if step7_quality_scores else None)
    _add_metric(overall_rows, 'quality', 'min_step3_quality_score', _round(min(step7_quality_scores), 4) if step7_quality_scores else None)
    output_path = os.path.join(output_dir, f'TDM_Run_Quantitative_Summary_{timestamp}.xlsx')
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        pd.DataFrame(overall_rows).to_excel(writer, sheet_name='overall_metrics', index=False)
        pd.DataFrame(step_rows).to_excel(writer, sheet_name='step_metrics', index=False)
        pd.DataFrame(status_rows).to_excel(writer, sheet_name='status_counts', index=False)
    return output_path
