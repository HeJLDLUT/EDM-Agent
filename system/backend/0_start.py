import sys
if sys.version_info < (3, 9):
    print(f'Python 3.9+ is required for this pipeline. Detected: {sys.version.split()[0]}')
    sys.exit(1)
import os
import subprocess
import time
import logging
import json
import uuid
import shutil
import glob
import argparse
from datetime import datetime
from typing import TypedDict, List, Literal, Optional, Dict, Any, Generator, Tuple, Set
from collections import deque
try:
    from pydantic import BaseModel, Field, field_validator
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn, TaskID
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    from langgraph.graph import StateGraph, END, START
    import jsonpatch
    import tdm_logger
    from tdm_logic import PipelineTracker
    from pipeline_control import PAUSE_ACK_FILENAME, PAUSE_REQUEST_FILENAME, RESUME_READY_STATUS, STEP_SELECTION_FILENAME, cleanup_outputs_from_step, clear_pause, directory_has_payload, normalize_selected_steps, prepare_step8_without_audit, read_selected_steps, selected_steps_to_cli, wait_if_pause_requested, write_selected_steps
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)
except ImportError as e:
    print(f'Critical Dependency Missing: {e}')
    print('Please run: pip install langgraph langchain-core rich pydantic jsonpatch python-dotenv')
    sys.exit(1)

class PipelineStep(BaseModel):
    script: str
    name: str
    input_dir: str = Field(alias='input')
    desc: str
    critical: bool = True

    @field_validator('script')
    @classmethod
    def script_must_exist(cls, v: str) -> str:
        if not os.path.exists(_resolve_script_path(v)):
            raise ValueError(f'Script file not found: {v}')
        return v
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_DATA_DIR = os.path.join(_PROJECT_ROOT, 'data')
PIPELINE_STATE_FILENAME = 'pipeline_phase_state.json'

def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Scientific TDM multi-stage controller')
    parser.add_argument('--mode', choices=['full', 'pre', 'continue', 'rerun'], default='full', help='full: run selected steps; pre: legacy 1-5 pause; continue/rerun: resume an existing run dir')
    parser.add_argument('--resume-run-dir', default='', help='Existing run directory to resume when --mode continue or rerun is used')
    parser.add_argument('--start-step', type=int, default=1, help='1-based step index to start from when continuing or rerunning')
    parser.add_argument('--selected-steps', default='', help='Comma-separated 1-based selected step indices from the bridge (for example: 1,2,3).')
    args = parser.parse_args()
    if args.mode in {'continue', 'rerun'} and (not args.resume_run_dir):
        parser.error('--resume-run-dir is required when --mode continue or rerun is used')
    args.start_step = max(1, min(8, int(args.start_step or 1)))
    return args

def _normalize_run_dir(path: str) -> str:
    return os.path.abspath(os.path.normpath(path))

def init_run_context(args: argparse.Namespace) -> Tuple[str, str]:
    if args.mode in {'continue', 'rerun'}:
        run_dir = _normalize_run_dir(args.resume_run_dir)
        if not os.path.isdir(run_dir):
            raise FileNotFoundError(f'Resume run directory not found: {run_dir}')
        return (os.path.basename(run_dir), run_dir)
    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    run_dir = os.path.join(_DATA_DIR, 'runs', run_id)
    return (run_id, run_dir)
ARGS = parse_cli_args()
PIPELINE_MODE = ARGS.mode
PIPELINE_START_STEP_IDX = ARGS.start_step - 1
SELECTED_STEP_INDICES: Set[int] = normalize_selected_steps(ARGS.selected_steps, index_base='one')
RUN_ID, RUN_DIR = init_run_context(ARGS)

def _resolve_script_path(script_path: str) -> str:
    if os.path.isabs(script_path):
        return script_path
    return os.path.join(_SCRIPT_DIR, script_path)
PRE_PROCESS_STEPS = [{'script': '1_MinerU.py', 'name': 'Step 1: PDF Parsing', 'input': os.path.join(_DATA_DIR, '00_input_pdfs'), 'desc': 'Convert PDF to Markdown', 'critical': True}, {'script': '2_Json提取.py', 'name': 'Step 2: Data Cleaning', 'input': os.path.join(_DATA_DIR, '01_mineru_raw'), 'desc': 'Extract and Rename JSON', 'critical': True}, {'script': '3_ID.py', 'name': 'Step 3: ID Injection', 'input': os.path.join(_DATA_DIR, '02_intermediate_json'), 'desc': 'Inject Unique Paragraph IDs', 'critical': True}, {'script': '4_step1_final.py', 'name': 'Step 4: Smart Filtering', 'input': os.path.join(_DATA_DIR, '03_tagged_content'), 'desc': 'LLM Recall Filtering', 'critical': True}, {'script': '5_post_process.py', 'name': 'Step 5: Post Processing', 'input': os.path.join(_DATA_DIR, '04_relevance_filter'), 'desc': 'Assemble Clean Markdown', 'critical': True}]
POST_PROCESS_STEP = {'script': '8_postprocess.py', 'name': 'Step 8: Final Summary', 'input': os.path.join(_DATA_DIR, '07_audited_db'), 'desc': 'Generate Final Excel Database', 'critical': True}
LOG_DIR = os.path.join(RUN_DIR, 'logs')
ARTIFACT_DIR = os.path.join(RUN_DIR, 'artifacts')
PIPELINE_STATE_FILE = os.path.join(RUN_DIR, PIPELINE_STATE_FILENAME)
os.makedirs(RUN_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(ARTIFACT_DIR, exist_ok=True)
os.environ['TDM_RUN_ID'] = RUN_ID
os.environ['TDM_LOG_DIR'] = LOG_DIR
os.environ['TDM_ARTIFACT_DIR'] = ARTIFACT_DIR
os.environ['TDM_DATA_DIR'] = RUN_DIR
os.environ['TDM_PAUSE_FILE'] = os.path.join(RUN_DIR, PAUSE_REQUEST_FILENAME)
os.environ['TDM_PAUSE_ACK_FILE'] = os.path.join(RUN_DIR, PAUSE_ACK_FILENAME)
os.environ['TDM_SELECTION_FILE'] = os.path.join(RUN_DIR, STEP_SELECTION_FILENAME)
os.environ['TDM_SELECTED_STEPS'] = selected_steps_to_cli(SELECTED_STEP_INDICES)
FEEDBACK_FILE = os.path.join(RUN_DIR, 'retry_feedback.json')
TARGETS_FILE = os.path.join(RUN_DIR, 'retry_targets.json')
FINISHED_STEP_INDICES: Set[int] = set()
SKIPPED_STEP_INDICES: Set[int] = set()
RERUN_METADATA: Dict[str, Any] = {}
console = Console()
_ui_context: Dict[str, Any] = {'progress': None, 'task_id': None}

def setup_logging():
    tdm_logger.setup_tdm_logging('Controller', RUN_DIR)
    logging.info(f'Pipeline Initialized. Run ID: {RUN_ID}')
    logging.info(f'Log Dir: {LOG_DIR}')

def merge_telemetry_logs():
    logging.info('Merging telemetry logs...')
    output_file = os.path.join(LOG_DIR, 'run_full_telemetry.jsonl')
    all_events = []
    jsonl_files = glob.glob(os.path.join(LOG_DIR, 'telemetry_*.jsonl'))
    for fpath in jsonl_files:
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        try:
                            event = json.loads(line)
                            all_events.append(event)
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logging.error(f'Failed to read {fpath}: {e}')
    all_events.sort(key=lambda x: x.get('ts', ''))
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            for event in all_events:
                f.write(json.dumps(event, ensure_ascii=False) + '\n')
        logging.info(f'Telemetry merged to: {output_file}')
    except Exception as e:
        logging.error(f'Failed to write merged telemetry: {e}')

def merge_debug_logs():
    logging.info('Merging debug logs...')
    output_file = os.path.join(LOG_DIR, 'run_full_debug.log')
    log_files = sorted(glob.glob(os.path.join(LOG_DIR, 'debug_*.log')))
    try:
        with open(output_file, 'w', encoding='utf-8') as outfile:
            for fname in log_files:
                step_name = os.path.basename(fname).replace('debug_', '').replace('.log', '')
                outfile.write(f"\n{'=' * 20} START OF {step_name} {'=' * 20}\n")
                try:
                    with open(fname, 'r', encoding='utf-8') as infile:
                        shutil.copyfileobj(infile, outfile)
                except Exception as e:
                    outfile.write(f'Error reading {fname}: {e}\n')
                outfile.write(f"\n{'=' * 20} END OF {step_name} {'=' * 20}\n")
        logging.info(f'Debug logs merged to: {output_file}')
    except Exception as e:
        logging.error(f'Failed to write merged debug log: {e}')

def read_pipeline_state(run_dir: Optional[str]=None) -> Dict[str, Any]:
    target_run_dir = run_dir or RUN_DIR
    state_file = os.path.join(target_run_dir, PIPELINE_STATE_FILENAME)
    if not os.path.exists(state_file):
        return {}
    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.warning(f'Failed to read pipeline state from {state_file}: {e}')
        return {}

def write_pipeline_state(status: str, next_action: str='', note: str='', **extra: Any):
    payload = {'run_id': RUN_ID, 'run_dir': RUN_DIR, 'mode': PIPELINE_MODE, 'status': status, 'next_action': next_action, 'note': note, 'start_step_idx': PIPELINE_START_STEP_IDX, 'selected_step_indices': sorted(read_selected_steps(RUN_DIR, SELECTED_STEP_INDICES)), 'finished_step_indices': sorted(FINISHED_STEP_INDICES), 'skipped_step_indices': sorted(SKIPPED_STEP_INDICES), 'updated_at': datetime.now().isoformat(timespec='seconds')}
    if RERUN_METADATA:
        payload.update(RERUN_METADATA)
    payload.update(extra)
    try:
        with open(PIPELINE_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f'Failed to write pipeline state: {e}')

def load_pre_steps() -> List[PipelineStep]:
    return [PipelineStep(**step) for step in PRE_PROCESS_STEPS]

def load_post_step() -> PipelineStep:
    return PipelineStep(**POST_PROCESS_STEP)

def current_selected_steps() -> Set[int]:
    return read_selected_steps(RUN_DIR, SELECTED_STEP_INDICES)

def is_step_selected(step_idx: int) -> bool:
    return step_idx in current_selected_steps()

def mark_step_finished(step_idx: int, step_name: str, success: bool):
    if success:
        FINISHED_STEP_INDICES.add(step_idx)
    write_pipeline_state(status=f'running_{PIPELINE_MODE}', next_action='continue', note=f"{step_name} {('finished' if success else 'failed')}")

def selected_steps_after(step_idx: int) -> List[int]:
    return sorted((idx for idx in current_selected_steps() if idx > step_idx))

def next_selected_step_after(step_idx: int) -> Optional[int]:
    later_steps = selected_steps_after(step_idx)
    return later_steps[0] if later_steps else None

def mark_step_skipped(step_idx: int, step_name: str, reason: str, next_step_idx: Optional[int]=None) -> None:
    SKIPPED_STEP_INDICES.add(step_idx)
    logging.info(reason)
    write_pipeline_state(status=f'running_{PIPELINE_MODE}', next_action='continue', note=reason, next_step_idx=min(8, next_step_idx if next_step_idx is not None else step_idx + 1))

def planned_step_pause(state: 'ReflexionState', step_idx: int, step_name: str) -> 'ReflexionState':
    next_selected_idx = next_selected_step_after(step_idx)
    if next_selected_idx is None:
        note = f'{step_name} is unchecked and no downstream step is selected. Skipping remaining unchecked path.'
        mark_step_skipped(step_idx, step_name, note)
        if _ui_context['progress']:
            _ui_context['progress'].console.print(f'[yellow]{note}[/]')
        return {**state, 'current_pre_index': step_idx + 1 if step_idx < len(load_pre_steps()) else state.get('current_pre_index', 0), 'planned_pause': False, 'blocked_step_idx': -1, 'next_step_idx': min(step_idx + 1, 8), 'pause_reason': note}
    note = f'{step_name} is unchecked. Press CONTINUE to skip unchecked steps and resume from Step {next_selected_idx + 1}.'
    if _ui_context['progress']:
        _ui_context['progress'].console.print(f'[bold yellow]{note}[/]')
    return {**state, 'planned_pause': True, 'blocked_step_idx': step_idx, 'next_step_idx': next_selected_idx, 'pause_reason': note}

def wait_after_step(step_name: str):
    wait_if_pause_requested(f'after {step_name}', RUN_DIR)

def upstream_was_skipped(step_idx: int) -> bool:
    return any((idx in SKIPPED_STEP_INDICES for idx in range(max(0, step_idx))))

def build_dependency_skip_state(state: 'ReflexionState', step_idx: int, step_name: str, input_dir: str, expected: str) -> 'ReflexionState':
    reason = f'{step_name} skipped because required input is missing after an upstream skipped step: {input_dir} ({expected})'
    mark_step_skipped(step_idx, step_name, reason)
    if _ui_context['progress']:
        _ui_context['progress'].console.print(f'[yellow]{reason}[/]')
    updated = {**state, 'planned_pause': False, 'blocked_step_idx': -1, 'next_step_idx': min(step_idx + 1, 8), 'pause_reason': reason}
    if step_idx < len(load_pre_steps()):
        updated['current_pre_index'] = step_idx + 1
    if step_idx == 6:
        updated['retry_targets'] = []
        updated['feedback_map'] = {}
        updated['skip_audit'] = True
    return updated

def build_missing_input_state(state: 'ReflexionState', step_name: str, input_dir: str, expected: str) -> 'ReflexionState':
    message = f'{step_name} cannot run because required input is missing: {input_dir} ({expected})'
    logging.error(message)
    if _ui_context['progress']:
        _ui_context['progress'].console.print(f'[bold red]{message}[/]')
    return {**state, 'stop_signal': True, 'failure_reason': message}

def pre_step_input_requirement(step_idx: int) -> Tuple[str, Tuple[str, ...], bool, str]:
    if step_idx == 0:
        return (os.path.join(_DATA_DIR, '00_input_pdfs'), ('.pdf',), False, 'PDF files')
    if step_idx == 1:
        return (os.path.join(RUN_DIR, '01_mineru_raw'), ('_content_list.json',), True, 'MinerU *_content_list.json files')
    if step_idx == 2:
        return (os.path.join(RUN_DIR, '02_intermediate_json'), ('.json',), False, 'intermediate JSON files')
    if step_idx == 3:
        return (os.path.join(RUN_DIR, '03_tagged_content'), ('.md',), False, 'tagged markdown files')
    if step_idx == 4:
        return (os.path.join(RUN_DIR, '04_relevance_filter'), ('.json',), False, 'Step 4 relevance-filter JSON files')
    raise ValueError(f'Unsupported pre-processing step index: {step_idx}')

def check_pre_step_input(state: 'ReflexionState', step_idx: int, step_name: str) -> Optional['ReflexionState']:
    input_dir, suffixes, recursive, expected = pre_step_input_requirement(step_idx)
    if not directory_has_payload(input_dir, suffixes, recursive=recursive):
        return build_missing_input_state(state, step_name, input_dir, expected)
    return None

def check_env():
    env_path = os.path.join(_SCRIPT_DIR, '.env')
    if not os.path.exists(env_path):
        console.print('[yellow]⚠️  Warning: .env file missing. API calls may fail.[/]')
    required_resources = ['PROMPT_STEP2.md', 'PROMPT_STEP3_PATCH.md', 'pydantic_step2.py', 'pydantic_step3_patch.py']
    missing = [f for f in required_resources if not os.path.exists(os.path.join(_SCRIPT_DIR, f))]
    if missing:
        console.print(Panel(f'[bold red]❌ Missing Resource Files:[/]\n' + '\n'.join(missing), title='Integrity Check Failed'))
        sys.exit(1)

def validate_resume_context():
    if PIPELINE_MODE != 'continue':
        return
    state = read_pipeline_state()
    if state.get('status') in {'awaiting_continue', RESUME_READY_STATUS}:
        return
    console.print(Panel(f"[bold red]Resume blocked.[/]\nRun dir: {RUN_DIR}\nExpected status: awaiting_continue or {RESUME_READY_STATUS}\nActual status: {state.get('status', 'missing')}", title='Invalid Continue Context'))
    sys.exit(1)

def run_subprocess_generator(script_path: str, args: List[str]=None) -> Generator[Dict[str, Any], None, Tuple[bool, float, List[str]]]:
    start_time = time.time()
    resolved_script_path = _resolve_script_path(script_path)
    cmd = [sys.executable, '-u', resolved_script_path]
    if args:
        cmd.extend(args)
    logging.info(f'STARTING {resolved_script_path} Args: {args}')
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    output_buffer = deque(maxlen=2000)
    success = False
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, env=env, cwd=_SCRIPT_DIR)
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                clean_line = line.strip()
                if clean_line:
                    output_buffer.append(clean_line)
                    logging.info(f'[{script_path}] {clean_line}')
                    yield {'type': 'log', 'content': clean_line}
        return_code = process.poll()
        success = return_code == 0
    except Exception as e:
        logging.error(f'Execution Error: {e}')
        output_buffer.append(f'Subprocess Exception: {str(e)}')
        yield {'type': 'error', 'content': str(e)}
        success = False
    duration = time.time() - start_time
    return (success, duration, list(output_buffer))

def run_step_stream(name: str, script: str, args: List[str]=None) -> Tuple[bool, float, List[str]]:
    tdm_logger.log_event('step_start', {'step_name': name, 'script': script, 'args': args or []})
    gen = run_subprocess_generator(script, args)
    logs = []
    try:
        while True:
            msg = next(gen)
            if msg['type'] == 'log':
                logs.append(msg['content'])
                if _ui_context['progress']:
                    desc = f"[bold blue]{name}[/] [dim]{msg['content'][:60]}...[/]"
                    if args:
                        desc += ' [yellow](Reflexion Mode)[/]'
                    _ui_context['progress'].update(_ui_context['task_id'], description=desc)
    except StopIteration as e:
        success, duration, _ = e.value
        tdm_logger.log_event('step_finish', {'step_name': name, 'success': success, 'duration': duration})
        return e.value

class StepRecord(BaseModel):
    step_name: str
    script: str
    duration: str
    status_icon: str
    logs: List[str]

class ReflexionState(TypedDict):
    current_pre_index: int
    results: List[StepRecord]
    stop_signal: bool
    failure_reason: str
    planned_pause: bool
    blocked_step_idx: int
    next_step_idx: int
    pause_reason: str
    start_step_idx: int
    skip_audit: bool
    iteration: int
    retry_targets: List[str]
    feedback_map: Dict[str, str]
    max_iterations: int

def render_failure_snippet(step_name: str, logs: List[str]):
    if not _ui_context['progress']:
        return
    _ui_context['progress'].console.print(f'[bold red]鉂?{step_name} Failed![/]')
    error_logs = logs[-10:] if logs else ['No output captured.']
    _ui_context['progress'].console.print(Panel('\n'.join(error_logs), title='Error Snippet', border_style='red'))

def pre_process_node(state: ReflexionState) -> ReflexionState:
    idx = state['current_pre_index']
    steps = load_pre_steps()
    if idx >= len(steps):
        return state
    step = steps[idx]
    if not is_step_selected(idx):
        return planned_step_pause(state, idx, step.name)
    input_dir, suffixes, recursive, expected = pre_step_input_requirement(idx)
    if not directory_has_payload(input_dir, suffixes, recursive=recursive):
        if upstream_was_skipped(idx):
            return build_dependency_skip_state(state, idx, step.name, input_dir, expected)
        return build_missing_input_state(state, step.name, input_dir, expected)
    if _ui_context['progress']:
        _ui_context['progress'].update(_ui_context['task_id'], description=f'[bold yellow]Initializing:[/] {step.desc}')
    success, duration, logs = run_step_stream(step.name, step.script)
    mark_step_finished(idx, step.name, success)
    wait_after_step(step.name)
    record = StepRecord(step_name=step.name, script=step.script, duration=f'{duration:.1f}s', status_icon='✅' if success else '❌', logs=logs)
    stop_signal = False
    if not success:
        render_failure_snippet(step.name, logs)
        if step.critical:
            if _ui_context['progress']:
                _ui_context['progress'].console.print('[bold red]⛔ Critical Failure. Pipeline Aborted.[/]')
            stop_signal = True
    return {**state, 'current_pre_index': idx + 1, 'results': state['results'] + [record], 'stop_signal': stop_signal, 'failure_reason': step.name if stop_signal else state['failure_reason']}

def extraction_node(state: ReflexionState) -> ReflexionState:
    if not is_step_selected(5):
        return planned_step_pause(state, 5, 'Step 6: Extraction')
    script = '6_step2_final.py'
    name = f"Step 6: Extraction (Iter {state['iteration']})"
    step6_input_dir = os.path.join(RUN_DIR, '05_cleaned_markdown')
    if not directory_has_payload(step6_input_dir, ('.md',)):
        if upstream_was_skipped(5):
            return build_dependency_skip_state(state, 5, name, step6_input_dir, 'cleaned markdown files')
        return build_missing_input_state(state, name, step6_input_dir, 'cleaned markdown files')
    args = []
    if state['iteration'] > 0 and state['retry_targets']:
        with open(TARGETS_FILE, 'w', encoding='utf-8') as f:
            json.dump(state['retry_targets'], f)
        with open(FEEDBACK_FILE, 'w', encoding='utf-8') as f:
            json.dump(state['feedback_map'], f)
        args = ['--targets', TARGETS_FILE, '--feedback', FEEDBACK_FILE]
        fallback_provider = os.getenv('STEP2_5_LLM_PROVIDER', 'openrouter1')
        fallback_model = os.getenv('STEP2_5_MODEL', 'google/gemini-3.1-pro-preview')
        args.extend(['--provider', fallback_provider, '--model', fallback_model])
        if _ui_context['progress']:
            _ui_context['progress'].console.print(f"[yellow]🔄 Reflexion Triggered: Retrying {len(state['retry_targets'])} files with STRONG MODEL ({fallback_model})...[/]")
    success, duration, logs = run_step_stream(name, script, args)
    mark_step_finished(5, name, success)
    wait_after_step(name)
    record = StepRecord(step_name=name, script=script, duration=f'{duration:.1f}s', status_icon='✅' if success else '❌', logs=logs)
    if not success:
        render_failure_snippet(name, logs)
        if _ui_context['progress']:
            _ui_context['progress'].console.print('[bold red]⛔ Extraction stage failed. Pipeline Aborted.[/]')
    return {**state, 'results': state['results'] + [record], 'stop_signal': state['stop_signal'] or not success, 'failure_reason': name if not success else state['failure_reason']}

def audit_node(state: ReflexionState) -> ReflexionState:
    if not is_step_selected(6):
        copied = prepare_step8_without_audit(RUN_DIR)
        note = f'Step 7 unchecked. Skipped audit and passed {copied} Step 6 JSON file(s) to Step 8.'
        mark_step_skipped(6, 'Step 7: Audit', note, next_step_idx=7)
        if _ui_context['progress']:
            _ui_context['progress'].console.print(f'[yellow]{note}[/]')
        return {**state, 'retry_targets': [], 'feedback_map': {}, 'skip_audit': True}
    script = '7_step3_audit.py'
    name = f"Step 7: Audit (Iter {state['iteration']})"
    step7_input_dir = os.path.join(RUN_DIR, '06_extracted_json')
    if not directory_has_payload(step7_input_dir, ('.json',)):
        if upstream_was_skipped(6):
            return build_dependency_skip_state(state, 6, name, step7_input_dir, 'Step 6 extracted JSON files')
        return build_missing_input_state(state, name, step7_input_dir, 'Step 6 extracted JSON files')
    if os.path.exists(FEEDBACK_FILE):
        try:
            os.remove(FEEDBACK_FILE)
        except Exception:
            pass
    args = ['--output_feedback', FEEDBACK_FILE]
    if state['iteration'] > 0 and state['retry_targets']:
        args.extend(['--targets', TARGETS_FILE])
    success, duration, logs = run_step_stream(name, script, args)
    mark_step_finished(6, name, success)
    wait_after_step(name)
    record = StepRecord(step_name=name, script=script, duration=f'{duration:.1f}s', status_icon='✅' if success else '❌', logs=logs)
    if not success:
        render_failure_snippet(name, logs)
        if _ui_context['progress']:
            _ui_context['progress'].console.print('[bold red]⛔ Audit stage failed. Pipeline Aborted.[/]')
    retry_targets = []
    feedback_map = {}
    if success and os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                retry_targets = data.get('targets', [])
                feedback_map = data.get('feedback', {})
        except Exception as e:
            logging.error(f'Failed to read audit feedback: {e}')
    return {**state, 'results': state['results'] + [record], 'retry_targets': retry_targets, 'feedback_map': feedback_map, 'stop_signal': state['stop_signal'] or not success, 'failure_reason': name if not success else state['failure_reason'], 'iteration': state['iteration'] + 1}

def promote_best_effort(state: ReflexionState):
    quarantine_dir = os.path.join(RUN_DIR, '07_quarantine')
    audited_dir = os.path.join(RUN_DIR, '07_audited_db')
    targets = state.get('retry_targets', [])
    if not targets:
        return
    os.makedirs(audited_dir, exist_ok=True)
    if _ui_context['progress']:
        _ui_context['progress'].console.print(f'[yellow]⚠️ Max Iterations Reached. Executing Best Effort promotion for {len(targets)} files...[/]')
    promoted_count = 0
    skipped_count = 0
    missing_count = 0
    for filename in targets:
        src_path = os.path.join(quarantine_dir, filename)
        dst_path = os.path.join(audited_dir, filename)
        if not os.path.exists(src_path):
            logging.warning(f'[Best Effort] Source missing in quarantine: {filename}')
            missing_count += 1
            continue
        if os.path.exists(dst_path):
            logging.info(f'[Best Effort] SKIP: {filename} already exists in audited DB. Preserving existing version.')
            skipped_count += 1
            continue
        try:
            shutil.copy2(src_path, dst_path)
            logging.info(f'[Best Effort] PROMOTED: {filename} -> audited_db')
            promoted_count += 1
        except Exception as e:
            logging.error(f'[Best Effort] Failed to copy {filename}: {e}')
    summary_msg = f'Best Effort Summary: Promoted {promoted_count}, Skipped {skipped_count} (Existing), Missing {missing_count}.'
    logging.info(summary_msg)
    if _ui_context['progress']:
        _ui_context['progress'].console.print(f'[dim]{summary_msg}[/]')

def post_process_node(state: ReflexionState) -> ReflexionState:
    if not is_step_selected(7):
        note = 'Step 8 unchecked. Final summary generation skipped.'
        mark_step_skipped(7, 'Step 8: Final Summary', note, next_step_idx=8)
        if _ui_context['progress']:
            _ui_context['progress'].console.print(f'[yellow]{note}[/]')
        return state
    if state['iteration'] > state['max_iterations'] and state['retry_targets']:
        promote_best_effort(state)
    step8_input_dir = os.path.join(RUN_DIR, '07_audited_db')
    if not directory_has_payload(step8_input_dir, ('.json',)):
        if upstream_was_skipped(7):
            return build_dependency_skip_state(state, 7, 'Step 8: Final Summary', step8_input_dir, 'audited or audit-skipped JSON files')
        return build_missing_input_state(state, 'Step 8: Final Summary', step8_input_dir, 'audited or audit-skipped JSON files')
    step = load_post_step()
    if _ui_context['progress']:
        _ui_context['progress'].update(_ui_context['task_id'], description=f'[bold yellow]Finalizing:[/] {step.desc}')
    success, duration, logs = run_step_stream(step.name, step.script)
    mark_step_finished(7, step.name, success)
    wait_after_step(step.name)
    record = StepRecord(step_name=step.name, script=step.script, duration=f'{duration:.1f}s', status_icon='✅' if success else '❌', logs=logs)
    if not success:
        render_failure_snippet(step.name, logs)
        if _ui_context['progress']:
            _ui_context['progress'].console.print('[bold red]⛔ Final summary stage failed.[/]')
    return {**state, 'results': state['results'] + [record], 'stop_signal': state['stop_signal'] or not success, 'failure_reason': step.name if not success else state['failure_reason']}

def pre_process_router(state: ReflexionState) -> Literal['pre_process_node', 'extraction_node', 'audit_node', 'post_process_node', '__end__']:
    if state['stop_signal'] or state['planned_pause']:
        return END
    if state['current_pre_index'] < len(load_pre_steps()):
        return 'pre_process_node'
    if PIPELINE_MODE == 'pre':
        return END
    if state['start_step_idx'] >= 7:
        return 'post_process_node'
    if state['start_step_idx'] == 6:
        return 'audit_node'
    return 'extraction_node'

def extraction_router(state: ReflexionState) -> Literal['audit_node', '__end__']:
    if state['stop_signal'] or state['planned_pause']:
        return END
    return 'audit_node'

def reflexion_router(state: ReflexionState) -> Literal['extraction_node', 'post_process_node', '__end__']:
    if state['planned_pause']:
        return END
    if state['stop_signal']:
        if _ui_context['progress']:
            reason = state['failure_reason'] or 'Unknown Stage'
            _ui_context['progress'].console.print(f'[bold red]⛔ Pipeline stopped after failure in {reason}.[/]')
        return END
    if not state['retry_targets']:
        if _ui_context['progress']:
            if state.get('skip_audit'):
                _ui_context['progress'].console.print('[bold yellow]Step 7 skipped. Proceeding to final summary.[/]')
            else:
                _ui_context['progress'].console.print('[bold green]✨ Audit Passed: No files rejected.[/]')
        return 'post_process_node'
    if state['iteration'] > state['max_iterations']:
        if _ui_context['progress']:
            _ui_context['progress'].console.print(f"[bold red]⛔ Max Iterations ({state['max_iterations']}) Reached. Proceeding with best effort.[/]")
        return 'post_process_node'
    return 'extraction_node'

def build_workflow():
    workflow = StateGraph(ReflexionState)
    workflow.add_node('pre_process_node', pre_process_node)
    workflow.add_node('extraction_node', extraction_node)
    workflow.add_node('audit_node', audit_node)
    workflow.add_node('post_process_node', post_process_node)
    workflow.add_edge(START, 'pre_process_node')
    workflow.add_conditional_edges('pre_process_node', pre_process_router)
    workflow.add_conditional_edges('extraction_node', extraction_router)
    workflow.add_conditional_edges('audit_node', reflexion_router)
    workflow.add_edge('post_process_node', END)
    return workflow.compile()

def print_summary(results: List[StepRecord]):
    console.print('\n')
    table = Table(title=f'Reflexion Pipeline Report (ID: {RUN_ID})', box=box.ROUNDED)
    table.add_column('Stage', style='cyan', no_wrap=True)
    table.add_column('Script', style='magenta')
    table.add_column('Duration', justify='right', style='green')
    table.add_column('Status', justify='center')
    for res in results:
        table.add_row(res.step_name, res.script, res.duration, res.status_icon)
    console.print(table)
    console.print(f'\n[dim]Run Artifacts (Logs/Feedback) saved to: {RUN_DIR}[/]')

def generate_run_summary():
    try:
        logging.info('Generating Final Run Summary Report...')
        tracker = PipelineTracker()
        tracker.begin_run()
        tracker.run_started_at = 0
        tracker.refresh(LOG_DIR, use_live_logs=False)
        stats = tracker.stats()
        tasks = tracker.tasks()
        report_txt = os.path.join(RUN_DIR, 'Run_Summary_Report.txt')
        report_csv = os.path.join(RUN_DIR, 'Run_Summary_Report.csv')
        with open(report_txt, 'w', encoding='utf-8') as f:
            f.write(f'==================================================\n')
            f.write(f'             RUN SUMMARY REPORT\n')
            f.write(f'==================================================\n')
            f.write(f'Run ID: {RUN_ID}\n')
            f.write(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(f'--- OVERALL STATISTICS ---\n')
            f.write(f"Total Files Processed : {stats.get('total', 0)}\n")
            f.write(f"Successfully Extracted: {stats.get('success', 0)}\n")
            f.write(f"Failed / Filtered Out : {stats.get('failed', 0)}\n")
            f.write(f"Other (Queued/Running): {stats.get('queued', 0) + stats.get('running', 0)}\n\n")
            f.write(f'--- FAILURE DETAILS ---\n')
            failed_tasks = [t for t in tasks if t.status == 'failed']
            if not failed_tasks:
                f.write('No files failed during this run.\n')
            else:
                for t in failed_tasks:
                    f.write(f'- File  : {t.filename}\n')
                    f.write(f'  Step  : {t.step_idx}\n')
                    f.write(f'  Reason: {t.detail}\n\n')
        import csv
        with open(report_csv, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Filename', 'Stem', 'Final_Step_Reached', 'Status', 'Detail'])
            for t in tasks:
                writer.writerow([t.filename, t.stem, t.step_idx, t.status, t.detail])
        logging.info(f'Run Summary Report saved to {report_txt} and {report_csv}')
    except Exception as e:
        logging.error(f'Failed to generate run summary report: {e}')

def main():
    global FINISHED_STEP_INDICES, SKIPPED_STEP_INDICES, PIPELINE_START_STEP_IDX, RERUN_METADATA
    setup_logging()
    existing_state = read_pipeline_state()
    RERUN_METADATA = {key: existing_state[key] for key in ('rerun_from_step_idx', 'rerun_started_at', 'rerun_epoch') if key in existing_state}
    if PIPELINE_MODE == 'continue' and ARGS.start_step == 1:
        try:
            PIPELINE_START_STEP_IDX = int(existing_state.get('next_step_idx', PIPELINE_START_STEP_IDX))
        except (TypeError, ValueError):
            pass
        PIPELINE_START_STEP_IDX = max(0, min(7, PIPELINE_START_STEP_IDX))
    if PIPELINE_MODE in {'continue', 'rerun'}:
        FINISHED_STEP_INDICES = {int(idx) for idx in existing_state.get('finished_step_indices', []) if isinstance(idx, int) or str(idx).isdigit()}
        SKIPPED_STEP_INDICES = {int(idx) for idx in existing_state.get('skipped_step_indices', []) if isinstance(idx, int) or str(idx).isdigit()}
    if PIPELINE_MODE == 'continue' and existing_state.get('status') == 'awaiting_continue':
        try:
            blocked_idx = int(existing_state.get('blocked_step_idx', -1))
        except (TypeError, ValueError):
            blocked_idx = -1
        if 0 <= blocked_idx < 8:
            if blocked_idx in SELECTED_STEP_INDICES:
                PIPELINE_START_STEP_IDX = blocked_idx
            else:
                later_selected = sorted((idx for idx in SELECTED_STEP_INDICES if idx > blocked_idx))
                if later_selected:
                    PIPELINE_START_STEP_IDX = later_selected[0]
            skip_until = max(blocked_idx + 1, PIPELINE_START_STEP_IDX)
            for skipped_idx in range(blocked_idx, min(skip_until, 8)):
                if skipped_idx == blocked_idx or skipped_idx not in SELECTED_STEP_INDICES:
                    SKIPPED_STEP_INDICES.add(skipped_idx)
    selected_steps = write_selected_steps(RUN_DIR, SELECTED_STEP_INDICES)
    clear_pause(RUN_DIR)
    if PIPELINE_MODE == 'rerun':
        RERUN_METADATA = {'rerun_from_step_idx': PIPELINE_START_STEP_IDX, 'rerun_started_at': datetime.now().isoformat(timespec='seconds'), 'rerun_epoch': int(time.time())}
        removed = cleanup_outputs_from_step(RUN_DIR, PIPELINE_START_STEP_IDX)
        FINISHED_STEP_INDICES = {idx for idx in FINISHED_STEP_INDICES if idx < PIPELINE_START_STEP_IDX}
        SKIPPED_STEP_INDICES = {idx for idx in SKIPPED_STEP_INDICES if idx < PIPELINE_START_STEP_IDX}
        logging.info(f'Rerun cleanup from Step {PIPELINE_START_STEP_IDX + 1}: {removed}')
    validate_resume_context()
    write_pipeline_state(status=f'running_{PIPELINE_MODE}', next_action='continue', note='Pipeline session started', selected_step_indices=sorted(selected_steps), next_step_idx=PIPELINE_START_STEP_IDX)
    console.clear()
    console.print(Panel.fit(f'[bold cyan]🧪 Scientific TDM Reflexion Agent 🧪[/]\n[dim]Run ID: {RUN_ID}[/]\n[dim]Artifacts Dir: {RUN_DIR}[/]\n[dim]Mode: {PIPELINE_MODE.upper()}[/]', subtitle=f"Session Log: {os.path.join(LOG_DIR, 'debug_Controller.log')}"))
    check_env()
    app = build_workflow()
    initial_pre_index = PIPELINE_START_STEP_IDX if PIPELINE_START_STEP_IDX < len(load_pre_steps()) else len(load_pre_steps())
    initial_state: ReflexionState = {'current_pre_index': initial_pre_index, 'results': [], 'stop_signal': False, 'failure_reason': '', 'planned_pause': False, 'blocked_step_idx': -1, 'next_step_idx': PIPELINE_START_STEP_IDX, 'pause_reason': '', 'start_step_idx': PIPELINE_START_STEP_IDX, 'skip_audit': False, 'iteration': 0, 'retry_targets': [], 'feedback_map': {}, 'max_iterations': 2}
    progress = Progress(SpinnerColumn(), TextColumn('[progress.description]{task.description}'), BarColumn(), TimeElapsedColumn(), console=console, expand=True)
    _ui_context['progress'] = progress
    try:
        with progress:
            _ui_context['task_id'] = progress.add_task(f'[bold blue]Pipeline Running ({RUN_ID})...', total=None)
            final_state = app.invoke(initial_state)
            final_desc = '[bold green]Pipeline Finished'
            if final_state['planned_pause']:
                final_desc = '[bold yellow]Paused at Step Checkpoint'
            elif PIPELINE_MODE == 'pre' and (not final_state['stop_signal']):
                final_desc = '[bold yellow]Paused by Design after Step 5'
            progress.update(_ui_context['task_id'], description=final_desc, completed=1, total=1)
        if final_state['stop_signal']:
            write_pipeline_state(status='failed', next_action='', note=final_state['failure_reason'] or 'Pipeline failed')
        elif final_state['planned_pause']:
            next_step_idx = int(final_state['next_step_idx'])
            if next_step_idx >= 8:
                write_pipeline_state(status='completed', next_action='', note='Pipeline finished after skipped final step', next_step_idx=8)
            else:
                write_pipeline_state(status='awaiting_continue', next_action='continue', note=final_state['pause_reason'], blocked_step_idx=final_state['blocked_step_idx'], next_step_idx=next_step_idx)
                console.print(f"[bold yellow]{final_state['pause_reason']}[/]")
        elif PIPELINE_MODE == 'pre':
            write_pipeline_state(status='awaiting_continue', next_action='continue', note='Paused by design after Step 5. Press CONTINUE to run Step 6-8.', next_step_idx=5)
            console.print('[bold yellow]Paused by design after Step 5. Press CONTINUE in the UI to run Step 6-8.[/]')
        else:
            write_pipeline_state(status='completed', next_action='', note='Pipeline finished successfully')
        merge_telemetry_logs()
        merge_debug_logs()
        generate_run_summary()
        print_summary(final_state['results'])
        if final_state['stop_signal']:
            sys.exit(1)
    except KeyboardInterrupt:
        console.print('\n[bold red]Pipeline interrupted by user.[/]')
        sys.exit(0)
if __name__ == '__main__':
    main()
