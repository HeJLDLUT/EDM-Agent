import os
import json
import glob
import time
import logging
import concurrent.futures
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from openai import OpenAI
import tdm_logger
from llm_runtime import build_llm_configs, extract_token_usage, llm_request_options, wait_if_pause_requested
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_DATA_DIR = os.environ.get('TDM_DATA_DIR') or os.path.join(_PROJECT_ROOT, 'data')
INPUT_DIR = os.path.join(_DATA_DIR, '03_tagged_content')
OUTPUT_DIR = os.path.join(_DATA_DIR, '04_relevance_filter')
PROMPT_FILE = os.path.join(_SCRIPT_DIR, 'PROMPT_STEP1.MD')
LOG_FILE = os.path.join(OUTPUT_DIR, 'step1_filtering.log')
load_dotenv(override=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
SELECTED_PROVIDER = os.getenv('STEP1_LLM_PROVIDER', 'deepseek').lower()
_step1_schema = {'type': 'object', 'properties': {'kept_ids': {'type': 'array', 'items': {'type': 'integer'}}}, 'required': ['kept_ids'], 'additionalProperties': False}
LLM_CONFIGS = build_llm_configs('STEP1', 'step1_relevance_filter', _step1_schema)
CURRENT_CONF = LLM_CONFIGS[SELECTED_PROVIDER]
MODEL_NAME = CURRENT_CONF['model']
TEMPERATURE = float(os.getenv('STEP1_TEMPERATURE', '0.0'))
MAX_RETRIES = int(os.getenv('STEP1_MAX_RETRIES', '2'))
MAX_WORKERS = max(1, int(os.getenv('STEP1_MAX_WORKERS', '1')))
tdm_logger.setup_tdm_logging('Step4_Filter')

class Step1FilterAgent:

    def __init__(self):
        api_key = os.getenv(CURRENT_CONF['api_key_env'])
        if not api_key:
            raise ValueError(f"❌ Missing API Key for provider '{SELECTED_PROVIDER}'. Check .env for {CURRENT_CONF['api_key_env']}.")
        default_headers = CURRENT_CONF.get('extra_headers', None)
        logging.info(f'🔌 Initializing Step 1 Client: Provider={SELECTED_PROVIDER.upper()}, Model={MODEL_NAME}')
        self.client = OpenAI(api_key=api_key, base_url=CURRENT_CONF['base_url'], default_headers=default_headers)
        self.system_prompt = self._load_file(PROMPT_FILE)

    def _load_file(self, path: str) -> str:
        if not os.path.exists(path):
            raise FileNotFoundError(f'Critical file missing: {path}')
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def process_file(self, file_path: str):
        filename = os.path.basename(file_path)
        logging.info(f'🚀 Processing: {filename}')
        with tdm_logger.file_context(filename):
            try:
                content = self._load_file(file_path)
            except Exception as e:
                logging.error(f'   ❌ Read Error: {e}')
                self._save_output(filename, None, [{'attempt': 0, 'status': 'read_error', 'error': str(e)}])
                tdm_logger.log_event('doc_finish', {'status': 'failed', 'error': str(e)}, severity='ERROR')
                return
            approx_tokens = len(content) // 4
            logging.info(f'   📐 Size: ~{approx_tokens} tokens')
            tdm_logger.log_event('doc_start', {'file_size': len(content), 'approx_tokens': approx_tokens})
            messages = [{'role': 'system', 'content': self.system_prompt}, {'role': 'user', 'content': content}]
            attempt_history = []
            start_time = time.time()
            final_json = None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    if attempt > 0:
                        logging.info(f'   🔄 Attempt {attempt}/{MAX_RETRIES}...')
                    tdm_logger.log_event('llm_request', {'attempt': attempt, 'model': MODEL_NAME, 'prompt_len_est': len(self.system_prompt) + len(content)})
                    completion = self.client.chat.completions.create(model=MODEL_NAME, messages=messages, temperature=TEMPERATURE, max_tokens=CURRENT_CONF.get('max_tokens', 4096), **llm_request_options(CURRENT_CONF))
                    wait_if_pause_requested(f'after Step 4 LLM response for {filename}')
                    choice = completion.choices[0]
                    raw_content = choice.message.content
                    tdm_logger.log_event('llm_response', {'attempt': attempt, 'finish_reason': getattr(choice, 'finish_reason', None), 'content_len': len(raw_content) if raw_content else 0, 'usage': extract_token_usage(completion)})
                    try:
                        final_json = json.loads(raw_content)
                    except json.JSONDecodeError:
                        import re
                        match = re.search('\\{.*\\}', raw_content, re.DOTALL)
                        if match:
                            final_json = json.loads(match.group(0))
                        else:
                            raise ValueError('Output is not valid JSON')
                    if 'kept_ids' not in final_json:
                        raise ValueError("JSON missing 'kept_ids' key")
                    if not isinstance(final_json['kept_ids'], list):
                        raise ValueError("'kept_ids' must be a list")
                    duration = time.time() - start_time
                    count = len(final_json['kept_ids'])
                    logging.info(f'   ✅ Success: Kept {count} blocks in {duration:.2f}s')
                    attempt_history.append({'attempt': attempt, 'status': 'success', 'duration': duration})
                    break
                except Exception as e:
                    logging.warning(f'   ⚠️ Error (Attempt {attempt}): {str(e)}')
                    attempt_history.append({'attempt': attempt, 'status': 'error', 'error': str(e)})
                    if attempt == MAX_RETRIES:
                        logging.error(f'   💀 Failed to process {filename}')
            tdm_logger.log_event('doc_finish', {'status': 'success' if final_json else 'failed', 'kept_count': len(final_json.get('kept_ids', [])) if final_json else 0, 'total_duration': time.time() - start_time})
            self._save_output(filename, final_json, attempt_history)

    def _save_output(self, filename: str, data: Optional[Dict], history: list):
        output_filename = filename.replace('.md', '.json')
        output_path = os.path.join(OUTPUT_DIR, output_filename)
        if data:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        else:
            fail_path = output_path.replace('.json', '_FAILURE.txt')
            with open(fail_path, 'w', encoding='utf-8') as f:
                f.write(f'Step 1 Failed.\nHistory: {json.dumps(history, indent=2)}')

    def run_batch(self):
        files = glob.glob(os.path.join(INPUT_DIR, '*.md'))
        if not files:
            tdm_logger.warning(f'⚠️ No markdown files found in {INPUT_DIR}')
            return
        tdm_logger.info(f'📦 Step 1 Batch Started. Found {len(files)} files.')
        if MAX_WORKERS == 1:
            for file_path in files:
                self.process_document(file_path)
            return
        tdm_logger.info(f'Step 1 parallel workers configured: {MAX_WORKERS}')

        def _thread_worker(file_path: str):
            filename = os.path.basename(file_path)
            try:
                self.process_document(file_path)
            except Exception as e:
                with tdm_logger.file_context(filename):
                    tdm_logger.error(f'Thread Crash: {filename} - {e}')
                    tdm_logger.log_event('thread_crash', {'error': str(e)}, severity='ERROR')
                raise
        crashed_files = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_file = {executor.submit(_thread_worker, file_path): os.path.basename(file_path) for file_path in files}
            for future in concurrent.futures.as_completed(future_to_file):
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
        tdm_logger.info('All files processed.')

    def process_document(self, file_path):
        self.process_file(file_path)
if __name__ == '__main__':
    if not os.path.exists('.env'):
        print('⚠️ Warning: No .env file found. API keys might be missing.')
    agent = Step1FilterAgent()
    agent.run_batch()
