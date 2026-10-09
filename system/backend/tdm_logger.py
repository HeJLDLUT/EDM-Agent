import os
import sys
import json
import time
import logging
import gzip
import hashlib
import re
import threading
import contextvars
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Any, Dict, Optional, Union
_file_ctx_var = contextvars.ContextVar('tdm_filename', default=None)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_DATA_DIR = os.path.join(_PROJECT_ROOT, 'data')
_config = {'run_id': os.getenv('TDM_RUN_ID', f'local_{int(time.time())}'), 'log_dir': os.getenv('TDM_LOG_DIR', os.path.join(_DATA_DIR, 'logs')), 'artifact_dir': os.getenv('TDM_ARTIFACT_DIR', os.path.join(_DATA_DIR, 'artifacts')), 'step_name': 'unknown', 'debug_mode': os.getenv('TDM_DEBUG', '0') == '1'}
_SENSITIVE_PATTERN = re.compile('(sk-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20}|sk-proj-[a-zA-Z0-9\\-_]+|Bearer\\s+[a-zA-Z0-9\\-\\._~+/]+=*)')

@contextmanager
def file_context(filename: str):
    token = _file_ctx_var.set(os.path.basename(filename))
    try:
        yield
    finally:
        _file_ctx_var.reset(token)

def _sanitize_content(content: str) -> str:
    return _SENSITIVE_PATTERN.sub('***REDACTED***', content)

def save_artifact(filename: str, content: Union[str, Dict, Any], force_ext: str=None) -> str:
    step_dir = os.path.join(_config['artifact_dir'], _config['step_name'])
    os.makedirs(step_dir, exist_ok=True)
    if not isinstance(content, str):
        try:
            text_content = json.dumps(content, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            text_content = str(content)
    else:
        text_content = content
    safe_content = _sanitize_content(text_content)
    content_bytes = safe_content.encode('utf-8')
    content_hash = hashlib.sha256(content_bytes).hexdigest()[:8]
    base_name = os.path.basename(filename)
    if force_ext:
        final_name = f'{os.path.splitext(base_name)[0]}_{content_hash}{force_ext}'
    else:
        final_name = f'{os.path.splitext(base_name)[0]}_{content_hash}{os.path.splitext(base_name)[1]}'
        if not os.path.splitext(final_name)[1]:
            final_name += '.txt'
    file_path = os.path.join(step_dir, final_name)
    if len(content_bytes) > 1 * 1024 * 1024:
        file_path += '.gz'
        with gzip.open(file_path, 'wb') as f:
            f.write(content_bytes)
    else:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(safe_content)
    return file_path

class TelemetryFormatter(logging.Formatter):

    def format(self, record):
        log_entry = {'ts': datetime.now(timezone.utc).isoformat(), 'run_id': _config['run_id'], 'step': _config['step_name'], 'level': record.levelname, 'pid': os.getpid(), 'tid': threading.get_ident(), 'file': _file_ctx_var.get(), 'event': getattr(record, 'event_name', 'log_message'), 'payload': getattr(record, 'payload', {})}
        if not getattr(record, 'is_telemetry', False):
            log_entry['payload']['message'] = record.getMessage()
            if record.exc_info:
                log_entry['payload']['exception'] = self.formatException(record.exc_info)
        return json.dumps(log_entry, ensure_ascii=False)

class ContextFilter(logging.Filter):

    def filter(self, record):
        record.tdm_file = _file_ctx_var.get() or 'Global'
        return True

class FlushFileHandler(logging.FileHandler):

    def emit(self, record):
        super().emit(record)
        self.flush()

def setup_tdm_logging(step_name: str, run_dir: str=None):
    _config['step_name'] = step_name
    if os.getenv('TDM_RUN_ID'):
        _config['run_id'] = os.getenv('TDM_RUN_ID')
    if run_dir:
        _config['log_dir'] = os.path.join(run_dir, 'logs')
        _config['artifact_dir'] = os.path.join(run_dir, 'artifacts')
    os.makedirs(_config['log_dir'], exist_ok=True)
    os.makedirs(_config['artifact_dir'], exist_ok=True)
    root_logger = logging.getLogger()
    if getattr(root_logger, '_tdm_configured', False):
        return
    root_logger.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console_fmt = logging.Formatter('%(asctime)s | %(levelname)s | [%(tdm_file)s] %(message)s', datefmt='%H:%M:%S')
    console.addFilter(ContextFilter())
    console.setFormatter(console_fmt)
    root_logger.addHandler(console)
    debug_file = os.path.join(_config['log_dir'], f'debug_{step_name}.log')
    file_handler = logging.FileHandler(debug_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter('%(asctime)s - %(levelname)s - [PID:%(process)d|TID:%(thread)d] - [%(tdm_file)s] - %(message)s')
    file_handler.addFilter(ContextFilter())
    file_handler.setFormatter(file_fmt)
    root_logger.addHandler(file_handler)
    telemetry_logger = logging.getLogger('TDM_Telemetry')
    telemetry_logger.propagate = False
    telemetry_logger.setLevel(logging.INFO)
    telemetry_file = os.path.join(_config['log_dir'], f'telemetry_{step_name}.jsonl')
    telemetry_handler = FlushFileHandler(telemetry_file, encoding='utf-8')
    telemetry_handler.setFormatter(TelemetryFormatter())
    telemetry_logger.addHandler(telemetry_handler)
    root_logger._tdm_configured = True
    telemetry_logger._tdm_configured = True

def log_event(event_name: str, payload: Dict[str, Any]=None, severity: str='INFO'):
    if payload is None:
        payload = {}
    logger = logging.getLogger('TDM_Telemetry')
    level_map = {'DEBUG': logging.DEBUG, 'INFO': logging.INFO, 'WARNING': logging.WARNING, 'WARN': logging.WARNING, 'ERROR': logging.ERROR, 'CRITICAL': logging.CRITICAL}
    level = level_map.get(severity.upper(), logging.INFO)
    logger.log(level, '', extra={'event_name': event_name, 'payload': payload, 'is_telemetry': True})

def info(msg: str):
    logging.getLogger().info(msg)

def error(msg: str):
    logging.getLogger().error(msg)

def warning(msg: str):
    logging.getLogger().warning(msg)

def debug(msg: str):
    logging.getLogger().debug(msg)
