import os
import sys
import threading
import subprocess
import signal
import webview
from collections import deque
from typing import Dict, Any, Optional
from tdm_logic import PipelineTracker, _find_latest_run_dir, _now_hms
from pipeline_control import PIPELINE_STATE_FILENAME, RESUME_READY_STATUS, analyze_run_resume_state, clear_pause, is_pause_requested, normalize_selected_steps, pause_ack_path, read_selected_steps, request_pause, resolve_run_dir_from_identifier, selected_steps_to_cli, write_resume_ready_state, write_selected_steps
CONTINUE_STEP_IDX = 5

class TdmApi:

    def __init__(self):
        self.tracker = PipelineTracker()
        self.process = None
        self._process_lock = threading.RLock()
        self._log_lock = threading.Lock()
        self.log_buffer = deque(maxlen=1000)
        self.current_run_dir = None
        self.current_log_dir = None
        self.pending_continue_run_dir = None
        self.pending_continue_step_idx = CONTINUE_STEP_IDX
        self._active_run_mode = None
        self._suppress_auto_discover = False
        self._running = True
        self.pipeline_python = self._resolve_pipeline_python()

    def _current_process(self):
        with self._process_lock:
            return self.process

    def _is_process_running(self) -> bool:
        proc = self._current_process()
        return bool(proc and proc.poll() is None)

    def _terminate_process_tree(self, proc: subprocess.Popen) -> None:
        if not proc or proc.poll() is not None:
            return
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    return
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception as e:
            self._append_log('ERROR', f'Failed to terminate process tree cleanly: {e}')
            try:
                proc.kill()
            except Exception:
                pass

    def _append_log(self, level: str, message: str):
        with self._log_lock:
            self.log_buffer.append(f'[{_now_hms()}] {level}: {message}')

    def _read_pipeline_state(self, run_dir: Optional[str]) -> Dict[str, Any]:
        if not run_dir:
            return {}
        state_file = os.path.join(run_dir, PIPELINE_STATE_FILENAME)
        if not os.path.exists(state_file):
            return {}
        try:
            import json
            with open(state_file, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _refresh_current_run_paths(self, latest_run: Optional[str]=None):
        run_dir = latest_run or _find_latest_run_dir()
        if not run_dir:
            return
        cand_log = os.path.join(run_dir, 'logs')
        if not os.path.exists(cand_log):
            return
        if cand_log != self.current_log_dir:
            with self._log_lock:
                self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Logs connected -> {cand_log}')
        self.current_run_dir = run_dir
        self.current_log_dir = cand_log

    def _update_continue_state(self):
        state = self._read_pipeline_state(self.current_run_dir)
        if state.get('status') in {'awaiting_continue', RESUME_READY_STATUS}:
            self.pending_continue_run_dir = self.current_run_dir
            try:
                self.pending_continue_step_idx = int(state.get('next_step_idx', CONTINUE_STEP_IDX))
            except (TypeError, ValueError):
                self.pending_continue_step_idx = CONTINUE_STEP_IDX
            self.pending_continue_step_idx = max(0, min(7, self.pending_continue_step_idx))
        elif state.get('status'):
            self.pending_continue_run_dir = None
            self.pending_continue_step_idx = CONTINUE_STEP_IDX

    def _discover_awaiting_continue_run(self):
        if self.current_run_dir or self._is_process_running():
            return
        latest_run = _find_latest_run_dir()
        state = self._read_pipeline_state(latest_run)
        if state.get('status') in {'awaiting_continue', RESUME_READY_STATUS}:
            self.current_run_dir = latest_run
            self.current_log_dir = os.path.join(latest_run, 'logs')
            self.pending_continue_run_dir = latest_run
            try:
                self.pending_continue_step_idx = int(state.get('next_step_idx', CONTINUE_STEP_IDX))
            except (TypeError, ValueError):
                self.pending_continue_step_idx = CONTINUE_STEP_IDX
            self.pending_continue_step_idx = max(0, min(7, self.pending_continue_step_idx))

    def _can_continue(self) -> bool:
        return bool(self.pending_continue_run_dir and os.path.isdir(self.pending_continue_run_dir))

    def _interpreter_version_supported(self, python_exe: str) -> bool:
        try:
            proc = subprocess.run([python_exe, '-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
            return proc.returncode == 0
        except Exception:
            return False

    def _read_dotenv_python_override(self) -> Optional[str]:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
        if not os.path.exists(env_path):
            return None
        try:
            with open(env_path, 'r', encoding='utf-8', errors='replace') as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    if key.strip() != 'TDM_PYTHON':
                        continue
                    value = value.strip().strip('"').strip("'")
                    return value or None
        except Exception:
            return None
        return None

    def _resolve_pipeline_python(self) -> Optional[str]:
        override = os.environ.get('TDM_PYTHON')
        if override and os.path.exists(override) and self._interpreter_version_supported(override):
            return override
        dotenv_override = self._read_dotenv_python_override()
        if dotenv_override and os.path.exists(dotenv_override) and self._interpreter_version_supported(dotenv_override):
            return dotenv_override
        if sys.version_info >= (3, 9):
            return sys.executable
        for version in ('3.11', '3.10', '3.9'):
            try:
                proc = subprocess.run(['py', f'-{version}', '-c', 'import sys; print(sys.executable)'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10, check=True)
                resolved = proc.stdout.strip()
                if resolved and os.path.exists(resolved) and self._interpreter_version_supported(resolved):
                    return resolved
            except Exception:
                continue
        return None

    def reset_view(self):
        if self._is_process_running():
            return {'status': 'error', 'message': 'Cannot reset while pipeline is running'}
        self.tracker.reset_view_state()
        with self._log_lock:
            self.log_buffer.clear()
        self.current_run_dir = None
        self.current_log_dir = None
        self.pending_continue_run_dir = None
        self.pending_continue_step_idx = CONTINUE_STEP_IDX
        self._active_run_mode = None
        self._suppress_auto_discover = True
        return {'status': 'ok'}

    def identify_resume_run(self, run_identifier: str, selected_steps=None):
        if self._is_process_running():
            return {'status': 'error', 'message': 'Cannot identify a run while pipeline is running'}
        resolved = resolve_run_dir_from_identifier(run_identifier)
        if resolved.get('status') != 'ok':
            message = resolved.get('message', 'Run folder could not be resolved')
            self._append_log('ERROR', message)
            return resolved
        run_dir = resolved['run_dir']
        analysis = analyze_run_resume_state(run_dir, selected_steps)
        if analysis.get('status') != 'ok':
            message = analysis.get('message', 'Run analysis failed')
            self._append_log('ERROR', message)
            return analysis
        if analysis.get('can_continue'):
            write_resume_ready_state(run_dir, analysis, selected_steps)
            self.pending_continue_run_dir = run_dir
            try:
                self.pending_continue_step_idx = int(analysis.get('resume_step_idx', CONTINUE_STEP_IDX))
            except (TypeError, ValueError):
                self.pending_continue_step_idx = CONTINUE_STEP_IDX
            self.pending_continue_step_idx = max(0, min(7, self.pending_continue_step_idx))
        else:
            self.pending_continue_run_dir = None
            self.pending_continue_step_idx = CONTINUE_STEP_IDX
        self.current_run_dir = run_dir
        self.current_log_dir = os.path.join(run_dir, 'logs')
        os.makedirs(self.current_log_dir, exist_ok=True)
        self._active_run_mode = None
        self._suppress_auto_discover = False
        self.tracker.begin_run()
        self.tracker.run_started_at = 0
        self.tracker.end_run()
        self.tracker.refresh(self.current_log_dir, use_live_logs=False)
        level = 'SYSTEM' if analysis.get('can_continue') else 'WARN'
        self._append_log(level, analysis.get('message', 'Run identified.'))
        return analysis

    def start_pipeline(self, selected_steps=None):
        if self._is_process_running():
            if is_pause_requested(self.current_run_dir):
                return self.resume_pipeline()
            self._append_log('ERROR', 'Pipeline is already running.')
            return {'status': 'error', 'message': 'Already running'}
        if not self.pipeline_python:
            message = f'Python 3.9+ is required to run the pipeline, and no compatible interpreter could be resolved automatically. Current interpreter: {sys.version.split()[0]}'
            self._append_log('ERROR', message)
            return {'status': 'error', 'message': message}
        self._suppress_auto_discover = False
        selected = normalize_selected_steps(selected_steps, index_base='zero')
        continue_mode = self._can_continue()
        self._active_run_mode = 'continue' if continue_mode else 'full'
        start_step_idx = self.pending_continue_step_idx if continue_mode else 0
        if continue_mode:
            self.current_run_dir = self.pending_continue_run_dir
            self.current_log_dir = os.path.join(self.current_run_dir, 'logs')
            write_selected_steps(self.current_run_dir, selected)
            clear_pause(self.current_run_dir)
            if not self.tracker.has_started:
                self.tracker.begin_run()
                self.tracker.run_started_at = 0
                self.tracker.end_run()
                self.tracker.refresh(self.current_log_dir)
            self.tracker.continue_run(resume_step_idx=start_step_idx)
            with self._log_lock:
                self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Continuing pipeline from Step {start_step_idx + 1}.')
        else:
            self.current_run_dir = None
            self.current_log_dir = None
            self.pending_continue_run_dir = None
            self.pending_continue_step_idx = CONTINUE_STEP_IDX
            self.tracker.reset_view_state()
            self.tracker.begin_run()
            with self._log_lock:
                self.log_buffer.clear()
                self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Pipeline initialized.')
                if self.pipeline_python != sys.executable:
                    self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Using fallback Python interpreter -> {self.pipeline_python}')
        thread = threading.Thread(target=self._run_script, args=(self._active_run_mode, self.current_run_dir, selected, start_step_idx))
        thread.daemon = True
        thread.start()
        return {'status': 'ok', 'mode': self._active_run_mode}

    def stop_pipeline(self):
        proc = self._current_process()
        if proc:
            self.tracker.aborted = True
            self.pending_continue_run_dir = None
            self.pending_continue_step_idx = CONTINUE_STEP_IDX
            self._active_run_mode = None
            self._suppress_auto_discover = True
            clear_pause(self.current_run_dir)
            self._terminate_process_tree(proc)
            with self._process_lock:
                if self.process is proc:
                    self.process = None
            with self._log_lock:
                self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Process aborted by user.')
            self.tracker.end_run()
        return {'status': 'ok'}

    def pause_pipeline(self):
        if not self._is_process_running():
            return {'status': 'error', 'message': 'Pipeline is not running'}
        self._refresh_current_run_paths()
        if not self.current_run_dir:
            return {'status': 'error', 'message': 'Run directory is not ready yet'}
        request_pause(self.current_run_dir)
        with self._log_lock:
            self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Pause requested. The pipeline will stop at the next safe point.')
        return {'status': 'ok'}

    def resume_pipeline(self):
        self._refresh_current_run_paths()
        if not self.current_run_dir:
            return {'status': 'error', 'message': 'No active run directory'}
        clear_pause(self.current_run_dir)
        with self._log_lock:
            self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Continue signal sent.')
        return {'status': 'ok'}

    def update_step_selection(self, selected_steps=None):
        selected = normalize_selected_steps(selected_steps, index_base='zero')
        if self.current_run_dir and os.path.isdir(self.current_run_dir):
            write_selected_steps(self.current_run_dir, selected)
        return {'status': 'ok', 'selected_step_indices': sorted(selected)}

    def rerun_step(self, step_idx: int, selected_steps=None):
        if self._is_process_running():
            return {'status': 'error', 'message': 'Cannot rerun while pipeline is running'}
        try:
            step_idx = int(step_idx)
        except (TypeError, ValueError):
            return {'status': 'error', 'message': 'Invalid step index'}
        step_idx = max(0, min(7, step_idx))
        run_dir = self.current_run_dir or _find_latest_run_dir()
        if not run_dir or not os.path.isdir(run_dir):
            return {'status': 'error', 'message': 'No run directory available to rerun'}
        selected = normalize_selected_steps(selected_steps, index_base='zero')
        selected.add(step_idx)
        self.current_run_dir = run_dir
        self.current_log_dir = os.path.join(run_dir, 'logs')
        self.pending_continue_run_dir = None
        self.pending_continue_step_idx = CONTINUE_STEP_IDX
        self._active_run_mode = 'rerun'
        self._suppress_auto_discover = False
        write_selected_steps(run_dir, selected)
        clear_pause(run_dir)
        if not self.tracker.has_started:
            self.tracker.begin_run()
            self.tracker.run_started_at = 0
            self.tracker.end_run()
            self.tracker.refresh(self.current_log_dir)
        if hasattr(self.tracker, 'reset_downstream_from'):
            self.tracker.reset_downstream_from(step_idx)
        self.tracker.continue_run(resume_step_idx=step_idx)
        with self._log_lock:
            self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Rerun requested from Step {step_idx + 1}. Downstream outputs will be cleared by controller.')
        thread = threading.Thread(target=self._run_script, args=('rerun', run_dir, selected, step_idx))
        thread.daemon = True
        thread.start()
        return {'status': 'ok', 'mode': 'rerun', 'step_idx': step_idx}

    def open_log_dir(self):
        target = self.current_log_dir
        if not target or not os.path.exists(target):
            script_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(script_dir)
            target = os.path.join(project_root, 'data')
        if os.path.exists(target):
            try:
                if os.name == 'nt':
                    os.startfile(target)
                elif sys.platform == 'darwin':
                    subprocess.call(['open', target])
                else:
                    subprocess.call(['xdg-open', target])
                with self._log_lock:
                    self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Explorer opened at {target}')
            except Exception as e:
                with self._log_lock:
                    self.log_buffer.append(f'[{_now_hms()}] ERROR: Cannot open folder: {str(e)}')
        else:
            with self._log_lock:
                self.log_buffer.append(f'[{_now_hms()}] ERROR: Target directory not found.')

    def _run_script(self, mode: str, run_dir: Optional[str]=None, selected_steps=None, start_step_idx: int=0):
        try:
            if not self.pipeline_python:
                with self._log_lock:
                    self.log_buffer.append(f'[{_now_hms()}] ERROR: Python 3.9+ with required backend dependencies was not found.')
                self.tracker.end_run()
                return
            env = os.environ.copy()
            env['PYTHONUTF8'] = '1'
            env['PYTHONIOENCODING'] = 'utf-8'
            env['RICH_FORCE_TERMINAL'] = '1'
            script_dir = os.path.dirname(os.path.abspath(__file__))
            start_script = os.path.join(script_dir, '0_start.py')
            cmd = [self.pipeline_python, '-u', start_script, '--mode', mode, '--selected-steps', selected_steps_to_cli(selected_steps), '--start-step', str(max(0, min(7, int(start_step_idx or 0))) + 1)]
            if mode in {'continue', 'rerun'} and run_dir:
                cmd.extend(['--resume-run-dir', run_dir])
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True, encoding='utf-8', errors='replace', bufsize=1, cwd=script_dir, start_new_session=os.name != 'nt')
            with self._process_lock:
                self.process = proc
            if proc.stdout is not None:
                for line in iter(proc.stdout.readline, ''):
                    if not line:
                        break
                    clean_line = line.strip()
                    if clean_line:
                        self.tracker.set_active_step_from_text(clean_line)
                        with self._log_lock:
                            self.log_buffer.append(f'[{_now_hms()}] {clean_line}')
                proc.stdout.close()
            ret = proc.wait()
            self.tracker.end_run()
            self._refresh_current_run_paths()
            if self.tracker.aborted:
                self.pending_continue_run_dir = None
                self.pending_continue_step_idx = CONTINUE_STEP_IDX
            else:
                self._update_continue_state()
            if self._can_continue():
                self.tracker.active_step_idx = self.pending_continue_step_idx
            with self._log_lock:
                if ret == 0 and self._can_continue():
                    state = self._read_pipeline_state(self.current_run_dir)
                    blocked_idx = state.get('blocked_step_idx')
                    try:
                        blocked_idx = int(blocked_idx)
                    except (TypeError, ValueError):
                        blocked_idx = None
                    if blocked_idx is not None and 0 <= blocked_idx <= 7:
                        self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Paused before Step {blocked_idx + 1}. Press CONTINUE to skip it and resume from Step {self.pending_continue_step_idx + 1}.')
                    else:
                        self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Paused. Press CONTINUE to resume from Step {self.pending_continue_step_idx + 1}.')
                else:
                    self.log_buffer.append(f'[{_now_hms()}] SYSTEM: Finished (Exit Code {ret})')
            self._active_run_mode = None
        except Exception as e:
            with self._log_lock:
                self.log_buffer.append(f'[{_now_hms()}] ERROR: {str(e)}')
            self.tracker.end_run()
            self.pending_continue_run_dir = None
            self.pending_continue_step_idx = CONTINUE_STEP_IDX
            self._active_run_mode = None
        finally:
            with self._process_lock:
                proc = self.process
                if proc is not None and proc.poll() is not None:
                    self.process = None

    def get_full_state(self) -> Dict[str, Any]:
        is_running = self._is_process_running()
        if is_running:
            self._refresh_current_run_paths()
        elif not self._suppress_auto_discover:
            self._discover_awaiting_continue_run()
        self._update_continue_state()
        pipeline_state = self._read_pipeline_state(self.current_run_dir)
        self.tracker.refresh(self.current_log_dir)
        with self._log_lock:
            logs_snapshot = list(self.log_buffer)
        tasks_data = [{'id': t.stem, 'file': t.filename, 'step': t.step_idx, 'status': t.status, 'detail': t.detail} for t in self.tracker.tasks()]
        selected_step_indices = pipeline_state.get('selected_step_indices')
        if isinstance(selected_step_indices, list):
            fallback_selected = {int(i) for i in selected_step_indices if isinstance(i, int) or str(i).isdigit()}
        else:
            fallback_selected = set(range(len(self.tracker.steps)))
        if self.current_run_dir and os.path.isdir(self.current_run_dir):
            selected_step_indices = sorted(read_selected_steps(self.current_run_dir, fallback_selected))
        else:
            selected_step_indices = sorted(fallback_selected)
        finished_step_indices = pipeline_state.get('finished_step_indices')
        if not isinstance(finished_step_indices, list):
            finished_step_indices = []
        else:
            finished_step_indices = [int(i) for i in finished_step_indices if isinstance(i, int) or str(i).isdigit()]
        finished_step_indices = [i for i in finished_step_indices if 0 <= i < len(self.tracker.steps)]
        skipped_step_indices = pipeline_state.get('skipped_step_indices')
        if not isinstance(skipped_step_indices, list):
            skipped_step_indices = []
        else:
            skipped_step_indices = [int(i) for i in skipped_step_indices if isinstance(i, int) or str(i).isdigit()]
        skipped_step_indices = [i for i in skipped_step_indices if 0 <= i < len(self.tracker.steps)]
        selected_step_indices = [i for i in selected_step_indices if 0 <= i < len(self.tracker.steps)]
        steps_data = [{'name': s.name, 'index': i, 'model_info': getattr(s, 'model_info', ''), 'selected': i in selected_step_indices, 'finished': i in finished_step_indices, 'skipped': i in skipped_step_indices} for i, s in enumerate(self.tracker.steps)]
        can_continue = not is_running and self._can_continue()
        pause_requested = is_running and is_pause_requested(self.current_run_dir)
        ack_file = pause_ack_path(self.current_run_dir)
        is_paused = bool(pause_requested and ack_file and os.path.exists(ack_file))
        active_step_idx = self.pending_continue_step_idx if can_continue else self.tracker.active_step_idx
        pipeline_note = pipeline_state.get('note', '') if can_continue else ''
        start_label = 'CONTINUE' if pause_requested else f'CONTINUE STEP {self.pending_continue_step_idx + 1}' if can_continue else 'START'
        return {'stats': self.tracker.stats(), 'active_step_idx': active_step_idx, 'tasks': tasks_data, 'steps': steps_data, 'logs': logs_snapshot, 'is_running': is_running, 'can_continue': can_continue, 'pause_requested': pause_requested, 'is_paused': is_paused, 'can_resume': pause_requested, 'selected_step_indices': selected_step_indices, 'finished_step_indices': finished_step_indices, 'skipped_step_indices': skipped_step_indices, 'start_button_label': start_label, 'pause_button_label': 'PAUSED' if is_paused else 'PAUSE PENDING' if pause_requested else 'PAUSE', 'pipeline_note': pipeline_note}
if __name__ == '__main__':
    current_dir = os.path.dirname(os.path.abspath(__file__))
    ui_path = os.path.join(current_dir, '..', 'frontend', 'index.html')
    assets_dir = os.path.join(current_dir, '..', 'frontend', 'assets')
    if not os.path.exists(ui_path):
        print(f'ERROR: UI file not found at {ui_path}')
        sys.exit(1)
    if not os.path.exists(assets_dir) or not os.listdir(assets_dir):
        print("WARNING: 'assets' directory seems empty or missing.")
        print("Please run 'python download_deps.py' first to fix black screen issues.")
    icon_path = os.path.abspath(os.path.join(current_dir, '..', 'frontend', 'logo_b.png'))
    print(f'>>> Launching Glass Core Interface from: {ui_path}')
    api = TdmApi()
    window = webview.create_window(title='TDM Multi-Agent Core', url=f'file://{ui_path}', js_api=api, width=1300, height=850, background_color='#020617', resizable=True)

    def set_window_icon():
        if os.name == 'nt' and os.path.exists(icon_path):
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                shell32 = ctypes.windll.shell32
                hwnd = user32.FindWindowW(None, 'TDM Multi-Agent Core')
                if hwnd:
                    from PIL import Image
                    import tempfile
                    img = Image.open(icon_path)
                    img = img.resize((32, 32), Image.Resampling.LANCZOS)
                    ico_path = os.path.join(tempfile.gettempdir(), 'tdm_icon.ico')
                    img.save(ico_path, format='ICO', sizes=[(32, 32)])
                    hicon = user32.LoadImageW(0, ico_path, 1, 32, 32, 16)
                    if hicon:
                        user32.SendMessageW(hwnd, 128, 0, hicon)
                        user32.SendMessageW(hwnd, 128, 1, hicon)
                        print(f'>>> Window icon set successfully!')
            except Exception as e:
                print(f'>>> Could not set window icon: {e}')
    import time

    def delayed_icon_set():
        time.sleep(1)
        set_window_icon()
    icon_thread = threading.Thread(target=delayed_icon_set)
    icon_thread.daemon = True
    icon_thread.start()
    webview.start(debug=False)
