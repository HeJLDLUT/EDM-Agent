import os
import time
import re
import glob
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

@dataclass
class StepDef:
    name: str
    out_dir: str
    model_info: str = ''

@dataclass
class TaskView:
    filename: str
    stem: str
    step_idx: int
    status: str
    detail: str
    done_upto: int
from dotenv import load_dotenv
load_dotenv()

def _get_model_info(step_prefix: str) -> str:
    provider = os.environ.get(f'{step_prefix}_LLM_PROVIDER', '').strip().upper()
    if not provider:
        return ''
    model = os.environ.get(f'{provider}_MODEL_{step_prefix}', '').strip()
    temp = os.environ.get(f'{step_prefix}_TEMPERATURE', '').strip()
    if model:
        temp_str = f' (T={temp})' if temp else ''
        return f'{provider} · {model}{temp_str}'
    return ''

def _now_hms() -> str:
    return time.strftime('%H:%M:%S')

def _safe_listdir(p: str) -> List[str]:
    try:
        return os.listdir(p) if os.path.exists(p) else []
    except Exception:
        return []

def _get_mineru_file_limit() -> Optional[int]:
    raw = os.environ.get('MINERU_MAX_FILES', '').strip()
    if not raw:
        return None
    try:
        limit = int(raw)
    except ValueError:
        return None
    if limit <= 0:
        return None
    return limit

def _find_latest_run_dir(base: str=None) -> Optional[str]:
    if base is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(script_dir)
        base = os.path.join(project_root, 'data', 'runs')
    if not os.path.exists(base):
        return None
    run_dirs = []
    for d in os.listdir(base):
        full = os.path.join(base, d)
        if os.path.isdir(full):
            run_dirs.append(full)
    if not run_dirs:
        return None
    run_dirs.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return run_dirs[0]

def _get_stem(filename: str) -> str:
    return os.path.splitext(os.path.basename(filename))[0]
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_DATA_DIR = os.path.join(_PROJECT_ROOT, 'data')

class PipelineTracker:

    def __init__(self):
        self.input_dir = os.path.join(_DATA_DIR, '00_input_pdfs')
        self._current_data_dir = _DATA_DIR
        self.steps: List[StepDef] = self._build_steps_for_dir(_DATA_DIR)
        self.quarantine_dir = os.path.join(_DATA_DIR, '07_quarantine')
        self.active_step_idx: int = 0
        self.process_running: bool = False
        self.aborted: bool = False
        self.has_started: bool = False
        self.run_started_at: float = 0.0
        self._tasks: Dict[str, TaskView] = {}
        self.log_cursors: Dict[str, int] = {}
        self.live_states: Dict[str, Dict[str, Any]] = {}

    def reset_view_state(self):
        self._update_steps_dir(_DATA_DIR)
        self.active_step_idx = 0
        self.process_running = False
        self.aborted = False
        self.has_started = False
        self.run_started_at = 0.0
        self._tasks = {}
        self.log_cursors.clear()
        self.live_states.clear()

    def reset_downstream_from(self, step_idx: int):
        try:
            step_idx = int(step_idx)
        except (TypeError, ValueError):
            step_idx = 0
        step_idx = max(0, min(7, step_idx))
        self.active_step_idx = step_idx
        self.live_states = {stem: state for stem, state in self.live_states.items() if int(state.get('step', -1)) < step_idx}
        self._tasks = {stem: task for stem, task in self._tasks.items() if task.done_upto < step_idx}

    def _build_steps_for_dir(self, data_dir: str) -> List[StepDef]:
        return [StepDef('Step 1: PDF Parsing', os.path.join(data_dir, '01_mineru_raw')), StepDef('Step 2: Data Cleaning', os.path.join(data_dir, '02_intermediate_json')), StepDef('Step 3: ID Injection', os.path.join(data_dir, '03_tagged_content')), StepDef('Step 4: Smart Filtering', os.path.join(data_dir, '04_relevance_filter'), _get_model_info('STEP1')), StepDef('Step 5: Post Processing', os.path.join(data_dir, '05_cleaned_markdown')), StepDef('Step 6: Extraction', os.path.join(data_dir, '06_extracted_json'), _get_model_info('STEP2')), StepDef('Step 7: Audit', os.path.join(data_dir, '07_audited_db'), _get_model_info('STEP3')), StepDef('Step 8: Final Summary', os.path.join(data_dir, '08_final_excel'))]

    def _update_steps_dir(self, data_dir: str):
        if data_dir == self._current_data_dir:
            return
        self._current_data_dir = data_dir
        self.steps = self._build_steps_for_dir(data_dir)
        self.quarantine_dir = os.path.join(data_dir, '07_quarantine')

    def begin_run(self):
        self._current_data_dir = None
        self.has_started = True
        self.aborted = False
        self.process_running = True
        self.active_step_idx = 0
        self.run_started_at = time.time()
        self.log_cursors.clear()
        self.live_states.clear()

    def continue_run(self, resume_step_idx: Optional[int]=None):
        self.has_started = True
        self.aborted = False
        self.process_running = True
        if resume_step_idx is not None:
            self.active_step_idx = resume_step_idx

    def end_run(self):
        self.process_running = False

    def set_active_step_from_text(self, text: str):
        m = re.search('(Step\\s+([1-8]))', text)
        if m:
            try:
                idx = int(m.group(2)) - 1
                if 0 <= idx <= 7:
                    self.active_step_idx = idx
            except Exception:
                pass

    def _scan_inputs(self) -> List[Tuple[str, str]]:
        pdfs = []
        for f in _safe_listdir(self.input_dir):
            if f.lower().endswith('.pdf'):
                stem = os.path.splitext(f)[0]
                pdfs.append((f, stem))
        pdfs.sort(key=lambda x: x[0].lower())
        limit = _get_mineru_file_limit()
        if limit is not None:
            pdfs = pdfs[:limit]
        return pdfs

    def _mtime_ok(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        if not self.has_started:
            return False
        if self.run_started_at <= 0:
            return True
        try:
            return os.path.getmtime(path) >= self.run_started_at
        except Exception:
            return False

    def _any_glob_newer(self, pattern: str) -> bool:
        for p in glob.glob(pattern):
            if self._mtime_ok(p):
                return True
        return False

    def _read_failure_detail(self, path: str, fallback: str) -> str:
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    clean_line = line.strip()
                    if clean_line:
                        return clean_line
        except Exception:
            pass
        return fallback

    def _read_step6_status(self, stem: str) -> Optional[str]:
        step6_output = os.path.join(self.steps[5].out_dir, f'{stem}.json')
        if not self._mtime_ok(step6_output):
            return None
        try:
            with open(step6_output, 'r', encoding='utf-8', errors='ignore') as f:
                data = json.load(f)
            metadata = data.get('metadata') or {}
            status = metadata.get('final_status')
            return str(status).lower() if status else None
        except Exception:
            return None

    def _detect_step_success(self, stem: str, step_idx: int) -> bool:
        if step_idx == 0:
            out = self.steps[0].out_dir
            zip_path = os.path.join(out, f'{stem}_result.zip')
            folder_path = os.path.join(out, stem)
            if self._mtime_ok(zip_path):
                return True
            if os.path.isdir(folder_path) and self._mtime_ok(folder_path):
                return True
            return False
        target_files = [(1, 'json'), (2, 'json'), (4, 'md'), (6, 'json')]
        if step_idx == 3:
            ok_path = os.path.join(self.steps[3].out_dir, f'{stem}.json')
            fail_marker = os.path.join(self.steps[3].out_dir, f'{stem}_FAILURE.txt')
            ok = self._mtime_ok(ok_path)
            failed = self._mtime_ok(fail_marker)
            return ok and (not failed)
        if step_idx == 5:
            return self._read_step6_status(stem) in {'success', 'no_data'}
        for s_idx, ext in target_files:
            if step_idx == s_idx:
                return self._mtime_ok(os.path.join(self.steps[s_idx].out_dir, f'{stem}.{ext}'))
        if step_idx == 7:
            return self._any_glob_newer(os.path.join(self.steps[7].out_dir, '*.xlsx'))
        return False

    def _detect_failure(self, stem: str) -> Optional[str]:
        if not self.has_started:
            return None
        step1_failure_marker = os.path.join(self.steps[0].out_dir, f'{stem}_FAILURE.txt')
        if self._mtime_ok(step1_failure_marker) and (not self._detect_step_success(stem, 0)):
            return self._read_failure_detail(step1_failure_marker, 'Step 1 failed during PDF parsing')
        step2_failure_marker = os.path.join(self.steps[1].out_dir, f'{stem}_FAILURE.txt')
        if self._mtime_ok(step2_failure_marker) and (not self._detect_step_success(stem, 1)):
            return self._read_failure_detail(step2_failure_marker, 'Step 2 failed during JSON collection')
        step3_failure_marker = os.path.join(self.steps[2].out_dir, f'{stem}_FAILURE.txt')
        if self._mtime_ok(step3_failure_marker) and (not self._detect_step_success(stem, 2)):
            return self._read_failure_detail(step3_failure_marker, 'Step 3 failed during ID injection')
        failure_marker = os.path.join(self.steps[3].out_dir, f'{stem}_FAILURE.txt')
        if self._mtime_ok(failure_marker):
            return 'Step 4 failed (Filtered Out)'
        step5_failure_marker = os.path.join(self.steps[4].out_dir, f'{stem}_FAILURE.txt')
        if self._mtime_ok(step5_failure_marker) and (not self._detect_step_success(stem, 4)):
            return self._read_failure_detail(step5_failure_marker, 'Step 5 failed during markdown assembly')
        step6_failure_report = os.path.join(self.steps[5].out_dir, f'{stem}_FAILURE_REPORT.txt')
        if self._mtime_ok(step6_failure_report) and self._read_step6_status(stem) not in {'success', 'no_data'}:
            return self._read_failure_detail(step6_failure_report, 'Step 6 failed before producing extracted JSON')
        audited = os.path.join(self.steps[6].out_dir, f'{stem}.json')
        quarantined = os.path.join(self.quarantine_dir, f'{stem}.json')
        if self._mtime_ok(quarantined) and (not self._mtime_ok(audited)):
            return 'Rejected (Audit Failure)'
        return None

    def _parse_step_idx_from_log_name(self, filename: str) -> int:
        m = re.search('Step\\s*(\\d+)', filename, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1)) - 1
            except ValueError:
                return -1
        return -1

    def process_logs(self, log_dir: str):
        if not log_dir or not os.path.exists(log_dir):
            return
        log_files = glob.glob(os.path.join(log_dir, 'telemetry_*.jsonl'))
        for log_path in log_files:
            log_name = os.path.basename(log_path)
            cursor = self.log_cursors.get(log_name, 0)
            try:
                if os.path.getsize(log_path) < cursor:
                    cursor = 0
            except OSError:
                cursor = 0
            log_step_idx = self._parse_step_idx_from_log_name(log_name)
            try:
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    f.seek(cursor)
                    lines = f.readlines()
                    self.log_cursors[log_name] = f.tell()
                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            event = data.get('event')
                            payload = data.get('payload', {})
                            filename_raw = data.get('file')
                            if event == 'step_start':
                                s_name = payload.get('step_name', '')
                                self.set_active_step_from_text(s_name)
                            if not filename_raw or filename_raw == 'Global':
                                continue
                            stem = _get_stem(filename_raw)
                            if event == 'doc_start':
                                self.live_states[stem] = {'status': 'running', 'step': log_step_idx, 'detail': 'Processing...'}
                            elif event == 'audit_start':
                                self.live_states[stem] = {'status': 'running', 'step': log_step_idx, 'detail': 'Auditing Logic...'}
                            elif event == 'doc_finish':
                                is_success = payload.get('status') == 'success'
                                self.live_states[stem] = {'status': 'success' if is_success else 'failed', 'step': log_step_idx, 'detail': 'Completed' if is_success else 'Error'}
                            elif event == 'audit_scored':
                                decision = payload.get('decision', 'unknown')
                                if decision.startswith('audit_failed') or 'rejected' in decision or 'retry' in decision:
                                    self.live_states[stem] = {'status': 'failed', 'step': log_step_idx, 'detail': f'Audit: {decision}'}
                                else:
                                    self.live_states[stem] = {'status': 'success', 'step': log_step_idx, 'detail': f'Audit: {decision}'}
                        except json.JSONDecodeError:
                            continue
            except Exception:
                pass

    def refresh(self, log_dir: Optional[str]=None, use_live_logs: bool=True):
        if log_dir:
            run_data_dir = os.path.dirname(log_dir)
            self._update_steps_dir(run_data_dir)
            if use_live_logs:
                self.process_logs(log_dir)
        inputs = self._scan_inputs()
        views: Dict[str, TaskView] = {}
        for pdf_name, stem in inputs:
            done_upto = -1
            for sidx in range(0, 8):
                if self._detect_step_success(stem, sidx):
                    done_upto = sidx
                else:
                    break
            if done_upto >= 7:
                step_idx, status, detail = (7, 'success', 'Completed')
            else:
                step_idx = done_upto + 1
                fail_reason = self._detect_failure(stem)
                if fail_reason:
                    status, detail = ('failed', fail_reason)
                elif self.process_running:
                    status, detail = ('queued', 'Pending...') if step_idx <= self.active_step_idx else ('queued', 'Waiting...')
                else:
                    status, detail = ('queued', 'Waiting...')
            if stem in self.live_states:
                ls = self.live_states[stem]
                if ls['step'] >= step_idx - 1:
                    if ls['status'] == 'running':
                        step_idx, status, detail = (ls['step'], 'running', ls['detail'])
                    elif ls['status'] == 'failed':
                        step_idx, status, detail = (ls['step'], 'failed', ls['detail'])
            views[pdf_name] = TaskView(filename=pdf_name, stem=stem, step_idx=max(0, min(step_idx, 7)), status=status, detail=detail, done_upto=done_upto)
        self._tasks = views
        if self.process_running:
            running_steps = [t.step_idx for t in views.values() if t.status == 'running']
            if running_steps:
                self.active_step_idx = max(running_steps)

    def stats(self) -> Dict[str, int]:
        total = len(self._tasks)
        vals = self._tasks.values()
        return {'total': total, 'queued': sum((1 for t in vals if t.status == 'queued')), 'running': sum((1 for t in vals if t.status == 'running')), 'success': sum((1 for t in vals if t.status == 'success')), 'failed': sum((1 for t in vals if t.status == 'failed'))}

    def tasks(self) -> List[TaskView]:
        return list(self._tasks.values())
