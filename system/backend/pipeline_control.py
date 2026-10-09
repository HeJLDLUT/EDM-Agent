import json
import os
import shutil
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
PIPELINE_STATE_FILENAME = 'pipeline_phase_state.json'
PAUSE_REQUEST_FILENAME = 'pipeline_pause_request.json'
PAUSE_ACK_FILENAME = 'pipeline_pause_ack.json'
STEP_SELECTION_FILENAME = 'pipeline_step_selection.json'
STEP_COUNT = 8
DEFAULT_SELECTED_STEPS: Set[int] = set(range(STEP_COUNT))
STEP_OUTPUT_PATHS: Dict[int, List[str]] = {0: ['01_mineru_raw'], 1: ['02_intermediate_json'], 2: ['03_tagged_content'], 3: ['04_relevance_filter'], 4: ['05_cleaned_markdown', '05_reports', '05_rejected_content'], 5: ['06_extracted_json'], 6: ['07_audited_db', '07_quarantine', '07_audit_logs', 'retry_feedback.json', 'retry_targets.json'], 7: ['08_final_excel', 'Run_Summary_Report.txt', 'Run_Summary_Report.csv']}
STEP_RUNTIME_NAMES: Dict[int, List[str]] = {3: ['Step4_Filter'], 5: ['Step6_Extraction'], 6: ['Step7_Audit'], 7: ['Step8_Postprocess']}
STEP_DISPLAY_NAMES: Dict[int, str] = {0: 'Step 1: PDF Parsing', 1: 'Step 2: Data Cleaning', 2: 'Step 3: ID Injection', 3: 'Step 4: Smart Filtering', 4: 'Step 5: Post Processing', 5: 'Step 6: Extraction', 6: 'Step 7: Audit', 7: 'Step 8: Final Summary'}
RESUME_READY_STATUS = 'resume_ready'

def _json_dump(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f'{path}.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)

def _json_load(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _run_file(run_dir: Optional[str], filename: str) -> Optional[str]:
    if not run_dir:
        return None
    return os.path.join(run_dir, filename)

def pause_request_path(run_dir: Optional[str]=None) -> Optional[str]:
    if run_dir:
        return _run_file(run_dir, PAUSE_REQUEST_FILENAME)
    return os.getenv('TDM_PAUSE_FILE') or _run_file(os.getenv('TDM_DATA_DIR'), PAUSE_REQUEST_FILENAME)

def pause_ack_path(run_dir: Optional[str]=None) -> Optional[str]:
    if run_dir:
        return _run_file(run_dir, PAUSE_ACK_FILENAME)
    return os.getenv('TDM_PAUSE_ACK_FILE') or _run_file(os.getenv('TDM_DATA_DIR'), PAUSE_ACK_FILENAME)

def selection_file_path(run_dir: Optional[str]=None) -> Optional[str]:
    if run_dir:
        return _run_file(run_dir, STEP_SELECTION_FILENAME)
    return os.getenv('TDM_SELECTION_FILE') or _run_file(os.getenv('TDM_DATA_DIR'), STEP_SELECTION_FILENAME)

def is_pause_requested(run_dir: Optional[str]=None) -> bool:
    path = pause_request_path(run_dir)
    return bool(path and os.path.exists(path))

def request_pause(run_dir: str, reason: str='user_request') -> str:
    path = os.path.join(run_dir, PAUSE_REQUEST_FILENAME)
    _json_dump(path, {'requested': True, 'reason': reason, 'requested_at': datetime.now().isoformat(timespec='seconds')})
    return path

def clear_pause(run_dir: Optional[str]) -> None:
    for path in (pause_request_path(run_dir), pause_ack_path(run_dir)):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

def _write_pause_ack(ack_path: Optional[str], context: str) -> None:
    if not ack_path:
        return
    _json_dump(ack_path, {'paused': True, 'context': context, 'pid': os.getpid(), 'paused_at': datetime.now().isoformat(timespec='seconds'), 'heartbeat_at': datetime.now().isoformat(timespec='seconds')})

def wait_if_pause_requested(context: str='', run_dir: Optional[str]=None, poll_interval: float=0.5, heartbeat_interval: float=5.0) -> bool:
    request_path = pause_request_path(run_dir)
    if not request_path or not os.path.exists(request_path):
        return False
    ack_path = pause_ack_path(run_dir)
    pause_context = context or 'pipeline'
    _write_pause_ack(ack_path, pause_context)
    last_heartbeat = time.time()
    print(f'SYSTEM: Pause active at safe point: {pause_context}. Waiting for CONTINUE...', flush=True)
    while os.path.exists(request_path):
        now = time.time()
        if heartbeat_interval > 0 and now - last_heartbeat >= heartbeat_interval:
            _write_pause_ack(ack_path, pause_context)
            last_heartbeat = now
        time.sleep(poll_interval)
    if ack_path and os.path.exists(ack_path):
        try:
            os.remove(ack_path)
        except OSError:
            pass
    print('SYSTEM: Pause released. Continuing pipeline.', flush=True)
    return True

def normalize_selected_steps(raw: Any, index_base: str='auto') -> Set[int]:
    if raw is None or raw == '':
        return set(DEFAULT_SELECTED_STEPS)
    explicit_empty = False
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped == '[]':
            parts: Iterable[Any] = []
            explicit_empty = True
        else:
            parts = [part.strip() for part in stripped.split(',') if part.strip()]
    elif isinstance(raw, dict):
        value = raw.get('selected_step_indices', raw.get('selected_steps', []))
        parts = value if isinstance(value, list) else []
        explicit_empty = isinstance(value, list) and len(value) == 0
    elif isinstance(raw, (list, tuple, set)):
        parts = raw
        explicit_empty = len(raw) == 0
    else:
        return set(DEFAULT_SELECTED_STEPS)
    values: List[int] = []
    for part in parts:
        try:
            values.append(int(part))
        except (TypeError, ValueError):
            continue
    if not values:
        if explicit_empty:
            return set()
        return set(DEFAULT_SELECTED_STEPS)
    normalized_base = (index_base or 'auto').lower()
    if normalized_base not in {'auto', 'zero', 'one'}:
        normalized_base = 'auto'
    if normalized_base == 'one':
        values = [value - 1 for value in values]
    elif normalized_base == 'auto' and 0 not in values and all((1 <= value <= STEP_COUNT for value in values)):
        values = [value - 1 for value in values]
    return {value for value in values if 0 <= value < STEP_COUNT}

def read_selected_steps(run_dir: Optional[str]=None, fallback: Optional[Set[int]]=None) -> Set[int]:
    path = selection_file_path(run_dir)
    if path and os.path.exists(path):
        return normalize_selected_steps(_json_load(path), index_base='zero')
    return set(DEFAULT_SELECTED_STEPS if fallback is None else fallback)

def write_selected_steps(run_dir: str, selected_steps: Any) -> Set[int]:
    selected = normalize_selected_steps(selected_steps, index_base='zero')
    path = os.path.join(run_dir, STEP_SELECTION_FILENAME)
    _json_dump(path, {'selected_step_indices': sorted(selected), 'index_base': 'zero', 'updated_at': datetime.now().isoformat(timespec='seconds')})
    return selected

def selected_steps_to_cli(selected_steps: Any) -> str:
    selected = normalize_selected_steps(selected_steps, index_base='zero')
    if not selected:
        return '[]'
    return ','.join((str(step + 1) for step in sorted(selected)))

def _assert_inside_run_dir(run_dir: str, target: str) -> None:
    run_abs = os.path.abspath(run_dir)
    target_abs = os.path.abspath(target)
    if os.path.commonpath([run_abs, target_abs]) != run_abs:
        raise ValueError(f'Refusing to delete outside run directory: {target_abs}')

def _remove_path(run_dir: str, relative_path: str) -> bool:
    target = os.path.join(run_dir, relative_path)
    _assert_inside_run_dir(run_dir, target)
    if not os.path.exists(target):
        return False
    if os.path.isdir(target):
        shutil.rmtree(target)
    else:
        os.remove(target)
    return True

def cleanup_outputs_from_step(run_dir: str, step_idx: int) -> List[str]:
    removed: List[str] = []
    for idx in range(max(0, step_idx), STEP_COUNT):
        for relative_path in STEP_OUTPUT_PATHS.get(idx, []):
            if _remove_path(run_dir, relative_path):
                removed.append(relative_path)
    removed.extend(cleanup_runtime_artifacts_from_step(run_dir, step_idx))
    return removed

def cleanup_runtime_artifacts_from_step(run_dir: str, step_idx: int) -> List[str]:
    removed: List[str] = []
    step_names: List[str] = []
    for idx in range(max(0, step_idx), STEP_COUNT):
        step_names.extend(STEP_RUNTIME_NAMES.get(idx, []))
    logs_dir = os.path.join(run_dir, 'logs')
    artifacts_dir = os.path.join(run_dir, 'artifacts')
    for log_name in ('run_full_telemetry.jsonl', 'run_full_debug.log'):
        relative_path = os.path.join('logs', log_name)
        if _remove_path(run_dir, relative_path):
            removed.append(relative_path)
    for step_name in step_names:
        for prefix, suffix in (('debug_', '.log'), ('telemetry_', '.jsonl')):
            relative_path = os.path.join('logs', f'{prefix}{step_name}{suffix}')
            if _remove_path(run_dir, relative_path):
                removed.append(relative_path)
        artifact_path = os.path.join(artifacts_dir, step_name)
        if os.path.exists(artifact_path):
            _assert_inside_run_dir(run_dir, artifact_path)
            shutil.rmtree(artifact_path)
            removed.append(os.path.join('artifacts', step_name))
    if os.path.isdir(logs_dir):
        for name in os.listdir(logs_dir):
            if name.startswith('run_full_') and name not in {'run_full_telemetry.jsonl', 'run_full_debug.log'}:
                relative_path = os.path.join('logs', name)
                if _remove_path(run_dir, relative_path):
                    removed.append(relative_path)
    return removed

def directory_has_payload(path: str, suffixes: Optional[Iterable[str]]=None, recursive: bool=False) -> bool:
    if not path or not os.path.isdir(path):
        return False
    normalized_suffixes = None
    if suffixes is not None:
        normalized_suffixes = tuple((str(s).lower() for s in suffixes))
    try:
        if recursive:
            for root, _, files in os.walk(path):
                for name in files:
                    if normalized_suffixes is None or name.lower().endswith(normalized_suffixes):
                        return True
            return False
        for entry in os.scandir(path):
            if not entry.is_file():
                continue
            if normalized_suffixes is None or entry.name.lower().endswith(normalized_suffixes):
                return True
    except OSError:
        return False
    return False

def default_runs_base() -> str:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, 'data', 'runs')

def _parse_step_idx(value: Any, fallback: Optional[int]=None) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    if 0 <= parsed < STEP_COUNT:
        return parsed
    if parsed == STEP_COUNT:
        return STEP_COUNT
    return fallback

def _step_set(raw: Any) -> Set[int]:
    if not isinstance(raw, list):
        return set()
    result: Set[int] = set()
    for item in raw:
        parsed = _parse_step_idx(item)
        if parsed is not None and parsed < STEP_COUNT:
            result.add(parsed)
    return result

def _latest_contiguous(indices: Set[int]) -> int:
    latest = -1
    for idx in range(STEP_COUNT):
        if idx not in indices:
            break
        latest = idx
    return latest

def _count_payload_files(path: str, suffixes: Optional[Iterable[str]]=None, recursive: bool=False, exclude_prefixes: Tuple[str, ...]=()) -> int:
    if not path or not os.path.exists(path):
        return 0
    normalized_suffixes = None
    if suffixes is not None:
        normalized_suffixes = tuple((str(s).lower() for s in suffixes))

    def include_name(name: str) -> bool:
        lower_name = name.lower()
        if exclude_prefixes and lower_name.startswith(tuple((p.lower() for p in exclude_prefixes))):
            return False
        if normalized_suffixes is None:
            return True
        return lower_name.endswith(normalized_suffixes)
    if os.path.isfile(path):
        return 1 if include_name(os.path.basename(path)) else 0
    count = 0
    try:
        if recursive:
            for _, _, files in os.walk(path):
                count += sum((1 for name in files if include_name(name)))
        else:
            with os.scandir(path) as entries:
                count = sum((1 for entry in entries if entry.is_file() and include_name(entry.name)))
    except OSError:
        return 0
    return count

def _count_project_input_pdfs(run_dir: str) -> int:
    data_root = os.path.dirname(os.path.dirname(os.path.abspath(run_dir)))
    input_dir = os.path.join(data_root, '00_input_pdfs')
    return _count_payload_files(input_dir, ('.pdf',))

def _step_processed_count(run_dir: str, step_idx: int) -> int:
    if step_idx == 0:
        mineru_dir = os.path.join(run_dir, '01_mineru_raw')
        content_count = _count_payload_files(mineru_dir, ('_content_list.json',), recursive=True)
        zip_count = _count_payload_files(mineru_dir, ('.zip',), recursive=False)
        failure_count = _count_payload_files(mineru_dir, ('_FAILURE.txt',), recursive=False)
        return max(content_count, zip_count) + failure_count
    if step_idx == 1:
        return _count_payload_files(os.path.join(run_dir, '02_intermediate_json'), ('.json', '_FAILURE.txt'))
    if step_idx == 2:
        return _count_payload_files(os.path.join(run_dir, '03_tagged_content'), ('.json', '_FAILURE.txt'))
    if step_idx == 3:
        return _count_payload_files(os.path.join(run_dir, '04_relevance_filter'), ('.json', '_FAILURE.txt'))
    if step_idx == 4:
        return _count_payload_files(os.path.join(run_dir, '05_cleaned_markdown'), ('.md', '_FAILURE.txt'))
    if step_idx == 5:
        output_dir = os.path.join(run_dir, '06_extracted_json')
        success_count = _count_payload_files(output_dir, ('.json',), exclude_prefixes=('step2_',))
        failure_count = _count_payload_files(output_dir, ('_FAILURE_REPORT.txt',))
        return success_count + failure_count
    if step_idx == 6:
        audited = _count_payload_files(os.path.join(run_dir, '07_audited_db'), ('.json',))
        quarantined = _count_payload_files(os.path.join(run_dir, '07_quarantine'), ('.json',))
        return audited + quarantined
    if step_idx == 7:
        excel_dir = _count_payload_files(os.path.join(run_dir, '08_final_excel'), ('.xlsx',))
        report_csv = _count_payload_files(os.path.join(run_dir, 'Run_Summary_Report.csv'))
        report_txt = _count_payload_files(os.path.join(run_dir, 'Run_Summary_Report.txt'))
        return excel_dir + report_csv + report_txt
    return 0

def _step_input_count(run_dir: str, step_idx: int) -> int:
    if step_idx == 0:
        return _count_project_input_pdfs(run_dir)
    if step_idx == 1:
        return _step_processed_count(run_dir, 0)
    if step_idx == 2:
        return _count_payload_files(os.path.join(run_dir, '02_intermediate_json'), ('.json',))
    if step_idx == 3:
        return _count_payload_files(os.path.join(run_dir, '03_tagged_content'), ('.md',))
    if step_idx == 4:
        return _count_payload_files(os.path.join(run_dir, '04_relevance_filter'), ('.json',))
    if step_idx == 5:
        return _count_payload_files(os.path.join(run_dir, '05_cleaned_markdown'), ('.md',))
    if step_idx == 6:
        return _count_payload_files(os.path.join(run_dir, '06_extracted_json'), ('.json',), exclude_prefixes=('step2_',))
    if step_idx == 7:
        return _count_payload_files(os.path.join(run_dir, '07_audited_db'), ('.json',))
    return 0

def _filesystem_finished_steps(run_dir: str) -> Set[int]:
    finished: Set[int] = set()
    for idx in range(STEP_COUNT):
        processed = _step_processed_count(run_dir, idx)
        if processed <= 0:
            continue
        expected = _step_input_count(run_dir, idx)
        if idx == 7:
            finished.add(idx)
        elif expected > 0 and processed >= expected:
            finished.add(idx)
    return finished

def _filesystem_observed_steps(run_dir: str) -> Set[int]:
    return {idx for idx in range(STEP_COUNT) if _step_processed_count(run_dir, idx) > 0}

def _coerce_to_run_dir(path: str) -> str:
    current = os.path.abspath(path)
    for _ in range(6):
        if os.path.isdir(current) and os.path.basename(current).lower().startswith('run_'):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.abspath(path)

def resolve_run_dir_from_identifier(identifier: str, runs_base: Optional[str]=None) -> Dict[str, Any]:
    token = str(identifier or '').strip().strip('"').strip("'")
    if not token:
        return {'status': 'error', 'message': 'Run identifier is empty.'}
    base = os.path.abspath(runs_base or default_runs_base())
    candidates: List[str] = []
    expanded = os.path.abspath(os.path.expanduser(token))
    if os.path.isdir(expanded):
        candidates.append(_coerce_to_run_dir(expanded))
    elif os.path.isfile(expanded):
        candidates.append(_coerce_to_run_dir(os.path.dirname(expanded)))
    joined = os.path.abspath(os.path.join(base, token))
    if os.path.isdir(joined):
        candidates.append(_coerce_to_run_dir(joined))
    if candidates:
        run_dir = os.path.abspath(candidates[0])
        return {'status': 'ok', 'run_dir': run_dir, 'run_id': os.path.basename(run_dir), 'matches': [run_dir]}
    if not os.path.isdir(base):
        return {'status': 'error', 'message': f'Runs directory not found: {base}'}
    lower_token = token.lower()
    matches = []
    try:
        for name in os.listdir(base):
            full = os.path.join(base, name)
            if os.path.isdir(full) and lower_token in name.lower():
                matches.append(os.path.abspath(full))
    except OSError as exc:
        return {'status': 'error', 'message': f'Failed to scan runs directory: {exc}'}
    if not matches:
        return {'status': 'error', 'message': f'No run folder matched: {token}'}
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    exact = [p for p in matches if os.path.basename(p).lower() == lower_token]
    if exact:
        run_dir = exact[0]
        return {'status': 'ok', 'run_dir': run_dir, 'run_id': os.path.basename(run_dir), 'matches': exact}
    if len(matches) > 1:
        preview = ', '.join((os.path.basename(p) for p in matches[:5]))
        suffix = '' if len(matches) <= 5 else f' ... (+{len(matches) - 5} more)'
        return {'status': 'error', 'message': f'Run identifier is ambiguous. Matched: {preview}{suffix}', 'matches': matches}
    run_dir = matches[0]
    return {'status': 'ok', 'run_dir': run_dir, 'run_id': os.path.basename(run_dir), 'matches': matches}

def _first_selected_at_or_after(selected_steps: Set[int], resume_step_idx: int) -> Optional[int]:
    if resume_step_idx >= STEP_COUNT:
        return STEP_COUNT
    later = sorted((idx for idx in selected_steps if idx >= resume_step_idx))
    return later[0] if later else None

def _resume_note(run_id: str, latest_progress_idx: int, resume_step_idx: int, status: str) -> str:
    if resume_step_idx >= STEP_COUNT:
        return f'识别成功：{run_id} 已完成到 Step 8，当前没有需要继续执行的步骤。'
    completed = '尚未完成任何步骤' if latest_progress_idx < 0 else f'已完成到 {STEP_DISPLAY_NAMES[latest_progress_idx]}'
    resume = STEP_DISPLAY_NAMES[resume_step_idx]
    state_hint = f' 原状态：{status}。' if status else ''
    return f'识别成功：{run_id} {completed}，将从 {resume} 继续。{state_hint}'.strip()

def analyze_run_resume_state(run_dir: str, selected_steps: Any=None) -> Dict[str, Any]:
    run_dir = os.path.abspath(os.path.normpath(run_dir))
    run_id = os.path.basename(run_dir)
    if not os.path.isdir(run_dir):
        return {'status': 'error', 'message': f'Run directory not found: {run_dir}'}
    state = _json_load(os.path.join(run_dir, PIPELINE_STATE_FILENAME))
    raw_status = str(state.get('status', '') or '').strip()
    status = raw_status.lower()
    state_finished = _step_set(state.get('finished_step_indices'))
    state_skipped = _step_set(state.get('skipped_step_indices'))
    fs_finished = _filesystem_finished_steps(run_dir)
    fs_observed = _filesystem_observed_steps(run_dir)
    finished = set(state_finished)
    skipped = set(state_skipped)
    source = 'state' if state else 'filesystem'
    if not finished and (not skipped):
        finished = fs_finished
        source = 'filesystem'
    progressed = finished | skipped
    latest_progress_idx = _latest_contiguous(progressed)
    if selected_steps is None:
        fallback_selected = _step_set(state.get('selected_step_indices')) or DEFAULT_SELECTED_STEPS
        selected = read_selected_steps(run_dir, fallback_selected)
    else:
        selected = normalize_selected_steps(selected_steps, index_base='zero')
    resume_step_idx: int
    if status == 'completed' or 7 in finished:
        resume_step_idx = STEP_COUNT
    elif status in {'awaiting_continue', RESUME_READY_STATUS}:
        state_next = _parse_step_idx(state.get('next_step_idx'), None)
        resume_step_idx = state_next if state_next is not None else latest_progress_idx + 1
    elif latest_progress_idx >= 0:
        resume_step_idx = latest_progress_idx + 1
    else:
        fs_latest = _latest_contiguous(fs_finished)
        if fs_latest >= 0:
            resume_step_idx = fs_latest + 1
            latest_progress_idx = max(latest_progress_idx, fs_latest)
            source = 'filesystem'
        elif fs_observed:
            resume_step_idx = min(max(fs_observed), STEP_COUNT - 1)
            source = 'filesystem_partial'
        else:
            resume_step_idx = 0
            source = 'empty'
    resume_step_idx = max(0, min(STEP_COUNT, int(resume_step_idx)))
    selected_resume = _first_selected_at_or_after(selected, resume_step_idx)
    if selected_resume is None:
        can_continue = False
        effective_resume_idx = STEP_COUNT
    else:
        effective_resume_idx = selected_resume
        can_continue = effective_resume_idx < STEP_COUNT
    if can_continue:
        latest_for_note = min(max(latest_progress_idx, effective_resume_idx - 1), STEP_COUNT - 1)
    else:
        latest_for_note = STEP_COUNT - 1 if resume_step_idx >= STEP_COUNT else latest_progress_idx
    note = _resume_note(run_id, latest_for_note, effective_resume_idx, raw_status)
    return {'status': 'ok', 'run_id': run_id, 'run_dir': run_dir, 'state_status': raw_status, 'can_continue': can_continue, 'resume_step_idx': effective_resume_idx, 'next_step_idx': effective_resume_idx, 'latest_completed_step_idx': latest_for_note, 'finished_step_indices': sorted(finished), 'skipped_step_indices': sorted(skipped), 'selected_step_indices': sorted(selected), 'filesystem_finished_step_indices': sorted(fs_finished), 'filesystem_observed_step_indices': sorted(fs_observed), 'source': source, 'note': note, 'message': note}

def write_resume_ready_state(run_dir: str, analysis: Dict[str, Any], selected_steps: Any=None) -> Dict[str, Any]:
    if analysis.get('status') != 'ok':
        return analysis
    if not analysis.get('can_continue'):
        return analysis
    selected = normalize_selected_steps(selected_steps, index_base='zero') if selected_steps is not None else set(analysis.get('selected_step_indices', DEFAULT_SELECTED_STEPS))
    resume_step_idx = int(analysis.get('resume_step_idx', 0))
    state_path = os.path.join(run_dir, PIPELINE_STATE_FILENAME)
    existing = _json_load(state_path)
    payload = dict(existing)
    payload.update({'run_id': analysis.get('run_id') or os.path.basename(run_dir), 'run_dir': os.path.abspath(run_dir), 'mode': RESUME_READY_STATUS, 'status': RESUME_READY_STATUS, 'next_action': 'continue', 'note': analysis.get('note', ''), 'start_step_idx': resume_step_idx, 'next_step_idx': resume_step_idx, 'selected_step_indices': sorted(selected), 'finished_step_indices': list(analysis.get('finished_step_indices', [])), 'skipped_step_indices': list(analysis.get('skipped_step_indices', [])), 'resume_detected_at': datetime.now().isoformat(timespec='seconds'), 'resume_detection_source': analysis.get('source', ''), 'updated_at': datetime.now().isoformat(timespec='seconds')})
    _json_dump(state_path, payload)
    return payload

def prepare_step8_without_audit(run_dir: str) -> int:
    source_dir = os.path.join(run_dir, '06_extracted_json')
    audited_dir = os.path.join(run_dir, '07_audited_db')
    for relative_path in ('07_audited_db', '07_quarantine', '07_audit_logs'):
        _remove_path(run_dir, relative_path)
    os.makedirs(audited_dir, exist_ok=True)
    if not os.path.isdir(source_dir):
        return 0
    copied = 0
    for filename in sorted(os.listdir(source_dir)):
        if not filename.lower().endswith('.json'):
            continue
        if filename.startswith('step2_'):
            continue
        source_path = os.path.join(source_dir, filename)
        try:
            with open(source_path, 'r', encoding='utf-8', errors='replace') as f:
                payload = json.load(f)
        except Exception:
            continue
        if not isinstance(payload, dict) or 'extraction' not in payload:
            continue
        metadata = payload.setdefault('metadata', {})
        if isinstance(metadata, dict):
            final_status = str(metadata.get('final_status', '')).lower()
            if final_status and final_status not in {'success', 'no_data'}:
                continue
            metadata['step3_status'] = 'audit_skipped'
            metadata['step3_audit_timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            metadata['audit_bypassed'] = True
            metadata['audit_bypass_reason'] = 'Step 7 unchecked by user'
            metadata['source_stage'] = 'step6_extracted_json'
            metadata.setdefault('quality_score', None)
        dest_path = os.path.join(audited_dir, filename)
        with open(dest_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        copied += 1
    return copied
