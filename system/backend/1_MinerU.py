import os
import sys
import time
import socket
import zipfile
from typing import Any, Dict, List, Optional, Tuple
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from urllib3.util.retry import Retry
from pipeline_control import wait_if_pause_requested
load_dotenv()
API_TOKEN = os.environ.get('MINERU_API_TOKEN', '')
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_RAW_DATA_DIR = os.path.join(_PROJECT_ROOT, 'data')
_DATA_DIR = os.environ.get('TDM_DATA_DIR') or _RAW_DATA_DIR
INPUT_FOLDER = os.path.join(_RAW_DATA_DIR, '00_input_pdfs')
OUTPUT_FOLDER = os.path.join(_DATA_DIR, '01_mineru_raw')
MODEL_VERSION = 'vlm'
EXTRA_FORMATS: List[str] = []
LANGUAGE = 'en'
ENABLE_OCR = True
ENABLE_FORMULA = True
ENABLE_TABLE = True
MINERU_API_FILE_LIMIT = 20
POLL_INTERVAL = 15
TIMEOUT = 3600
REQUEST_TIMEOUT: Tuple[int, int] = (10, 60)
DOWNLOAD_REQUEST_TIMEOUT: Tuple[int, int] = (30, 300)
DOWNLOAD_MAX_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF = 5
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
BASE_HOSTS = ['mineru.net', 'mineru.org.cn']

def _resolve_max_files() -> Optional[int]:
    raw = os.environ.get('MINERU_MAX_FILES', '').strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value
MAX_FILES = _resolve_max_files()

class MineruAutoProcessor:

    def __init__(self) -> None:
        token = API_TOKEN.strip()
        if token and (not token.lower().startswith('bearer ')):
            token = f'Bearer {token}'
        self.headers = {'Authorization': token, 'Content-Type': 'application/json'}
        os.makedirs(OUTPUT_FOLDER, exist_ok=True)
        self.session = self._build_session()
        self.base_url = self._pick_base_url()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(total=5, connect=5, read=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset(['GET', 'POST', 'PUT']))
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        session.trust_env = True
        return session

    def _dns_ok(self, host: str) -> bool:
        try:
            socket.getaddrinfo(host, 443)
            return True
        except socket.gaierror:
            return False

    def _pick_base_url(self) -> str:
        for host in BASE_HOSTS:
            if self._dns_ok(host):
                return f'https://{host}'
        return 'https://mineru.net'

    def _failure_marker_path(self, source_name: str) -> str:
        base_name = os.path.splitext(source_name)[0]
        return os.path.join(OUTPUT_FOLDER, f'{base_name}_FAILURE.txt')

    def _write_failure_marker(self, source_name: str, detail: str) -> None:
        marker_path = self._failure_marker_path(source_name)
        with open(marker_path, 'w', encoding='utf-8') as f:
            f.write(f'Step 1 MinerU failed for {source_name}\n')
            f.write(f'Detail: {detail}\n')

    def _clear_failure_marker(self, source_name: str) -> None:
        marker_path = self._failure_marker_path(source_name)
        if os.path.exists(marker_path):
            os.remove(marker_path)

    def _build_failed_result(self, source_name: str, detail: str, state: str) -> Dict[str, str]:
        return {'file_name': source_name, 'state': state, 'err_msg': detail}

    def _remove_partial_zip(self, zip_path: str) -> None:
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except OSError:
            pass

    def _download_result_zip(self, download_url: str, zip_path: str) -> Tuple[bool, Optional[str]]:
        last_error = 'Unknown download error'
        for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
            self._remove_partial_zip(zip_path)
            try:
                with self.session.get(download_url, stream=True, timeout=DOWNLOAD_REQUEST_TIMEOUT) as response:
                    if response.status_code != 200:
                        last_error = f'HTTP {response.status_code}'
                    else:
                        with open(zip_path, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                                if chunk:
                                    f.write(chunk)
                        return (True, None)
            except RequestException as e:
                last_error = str(e)
            except Exception as e:
                last_error = str(e)
            if attempt < DOWNLOAD_MAX_RETRIES:
                wait_seconds = DOWNLOAD_RETRY_BACKOFF * attempt
                print(f'retrying download in {wait_seconds}s (attempt {attempt + 1}/{DOWNLOAD_MAX_RETRIES}) after error: {last_error}')
                wait_if_pause_requested('after MinerU download retryable failure')
                time.sleep(wait_seconds)
        self._remove_partial_zip(zip_path)
        return (False, last_error)

    def _scan_pdf_files(self) -> Optional[List[str]]:
        print('\n[Step 1/3] Scanning and uploading PDF files...')
        if not os.path.exists(INPUT_FOLDER):
            print(f'Input folder not found: {INPUT_FOLDER}')
            return None
        all_pdf_files = sorted([name for name in os.listdir(INPUT_FOLDER) if name.lower().endswith('.pdf')], key=str.lower)
        if not all_pdf_files:
            print('No PDF files found.')
            return None
        if MAX_FILES is not None:
            pdf_files = all_pdf_files[:MAX_FILES]
            if len(all_pdf_files) > MAX_FILES:
                print(f'Found {len(all_pdf_files)} PDF files, but MINERU_MAX_FILES is set. Only the first {MAX_FILES} files will be processed.')
        else:
            pdf_files = all_pdf_files
        print(f'Found {len(pdf_files)} PDF file(s). Uploads will be split into batches of at most {MINERU_API_FILE_LIMIT}.')
        return pdf_files

    def upload_pdfs(self) -> Optional[Dict[str, Any]]:
        pdf_files = self._scan_pdf_files()
        if not pdf_files:
            return None
        total_batches = (len(pdf_files) + MINERU_API_FILE_LIMIT - 1) // MINERU_API_FILE_LIMIT
        batch_jobs: List[Dict[str, Any]] = []
        batch_level_failures: List[Dict[str, str]] = []
        for batch_index in range(total_batches):
            start = batch_index * MINERU_API_FILE_LIMIT
            end = start + MINERU_API_FILE_LIMIT
            batch_files = pdf_files[start:end]
            print(f'[Step 1/3] Preparing upload batch {batch_index + 1}/{total_batches} with {len(batch_files)} file(s)...')
            payload = {'model_version': MODEL_VERSION, 'enable_formula': ENABLE_FORMULA, 'enable_table': ENABLE_TABLE, 'language': LANGUAGE, 'extra_formats': EXTRA_FORMATS, 'files': [{'name': name, 'is_ocr': ENABLE_OCR} for name in batch_files]}
            try:
                response = self.session.post(f'{self.base_url}/api/v4/file-urls/batch', headers=self.headers, json=payload, timeout=REQUEST_TIMEOUT)
                data = response.json()
            except Exception as e:
                detail = f'Batch upload request failed: {e}'
                print(detail)
                for filename in batch_files:
                    self._write_failure_marker(filename, detail)
                    batch_level_failures.append(self._build_failed_result(filename, detail, 'batch_request_failed'))
                continue
            if data.get('code') != 0:
                detail = f"Batch API request failed (code={data.get('code')}): {data.get('msg')}"
                print(detail)
                for filename in batch_files:
                    self._write_failure_marker(filename, detail)
                    batch_level_failures.append(self._build_failed_result(filename, detail, 'batch_request_failed'))
                continue
            batch_id = data['data']['batch_id']
            upload_urls = data['data'].get('file_urls') or []
            print(f'Created batch {batch_index + 1}/{total_batches} (Batch ID: {batch_id})')
            success_count = 0
            uploaded_files: List[str] = []
            for idx, (filename, url) in enumerate(zip(batch_files, upload_urls), start=1):
                file_path = os.path.join(INPUT_FOLDER, filename)
                try:
                    with open(file_path, 'rb') as f:
                        put_response = self.session.put(url, data=f, timeout=REQUEST_TIMEOUT)
                    if put_response.status_code == 200:
                        print(f'  - [Batch {batch_index + 1}/{total_batches} | {idx}/{len(batch_files)}] {filename} uploaded')
                        success_count += 1
                        uploaded_files.append(filename)
                    else:
                        detail = f'Upload failed with HTTP {put_response.status_code}'
                        print(f'  - [Batch {batch_index + 1}/{total_batches} | {idx}/{len(batch_files)}] {filename} failed ({detail})')
                        self._write_failure_marker(filename, detail)
                        batch_level_failures.append(self._build_failed_result(filename, detail, 'upload_failed'))
                except Exception as e:
                    detail = f'Upload error: {e}'
                    print(f'  - [Batch {batch_index + 1}/{total_batches} | {idx}/{len(batch_files)}] {filename} error: {e}')
                    self._write_failure_marker(filename, detail)
                    batch_level_failures.append(self._build_failed_result(filename, detail, 'upload_failed'))
                wait_if_pause_requested(f'after MinerU upload attempt: {filename}')
            if len(upload_urls) < len(batch_files):
                detail = 'Batch API returned fewer upload URLs than requested files.'
                for filename in batch_files[len(upload_urls):]:
                    print(f'  - [Batch {batch_index + 1}/{total_batches}] {filename} failed ({detail})')
                    self._write_failure_marker(filename, detail)
                    batch_level_failures.append(self._build_failed_result(filename, detail, 'upload_url_missing'))
            if success_count == 0:
                print('All uploads in this batch failed.')
                continue
            print(f'Batch {batch_index + 1}/{total_batches} uploaded: {success_count}/{len(batch_files)} file(s)')
            batch_jobs.append({'batch_id': batch_id, 'batch_index': batch_index + 1, 'total_batches': total_batches, 'files': uploaded_files})
            wait_if_pause_requested(f'after MinerU upload batch {batch_index + 1}/{total_batches}')
        if not batch_jobs and (not batch_level_failures):
            return None
        return {'batch_jobs': batch_jobs, 'early_failures': batch_level_failures}

    def poll_results(self, batch_id: str, batch_index: Optional[int]=None, total_batches: Optional[int]=None) -> Optional[List[Dict[str, Any]]]:
        batch_label = f'Batch ID: {batch_id}'
        if batch_index is not None and total_batches is not None:
            batch_label = f'batch {batch_index}/{total_batches} | Batch ID: {batch_id}'
        print(f'\n[Step 2/3] Polling extraction results ({batch_label})')
        url = f'{self.base_url}/api/v4/extract-results/batch/{batch_id}'
        poll_headers = {'Authorization': self.headers['Authorization']}
        start_time = time.time()
        while time.time() - start_time < TIMEOUT:
            try:
                response = self.session.get(url, headers=poll_headers, timeout=REQUEST_TIMEOUT)
                if response.status_code != 200:
                    print(f'Polling returned HTTP {response.status_code}; retrying...')
                    wait_if_pause_requested('after MinerU poll HTTP retry')
                    time.sleep(POLL_INTERVAL)
                    continue
                data = response.json()
                if data.get('code') != 0:
                    print(f"Polling failed: {data.get('msg')}")
                    return None
                results = data['data']['extract_result']
                stats = {'done': 0, 'pending': 0, 'running': 0, 'failed': 0, 'waiting-file': 0, 'converting': 0}
                for item in results:
                    state = item.get('state', 'unknown')
                    stats[state] = stats.get(state, 0) + 1
                unfinished = stats['pending'] + stats['running'] + stats['waiting-file'] + stats['converting']
                print(f"Status: done={stats['done']} running={stats['running'] + stats['converting']} queued={stats['pending'] + stats['waiting-file']} failed={stats['failed']}")
                if unfinished == 0:
                    print('All MinerU tasks finished.')
                    return results
                wait_if_pause_requested('after MinerU poll cycle')
                time.sleep(POLL_INTERVAL)
            except Exception as e:
                print(f'Polling error: {e}')
                wait_if_pause_requested('after MinerU poll exception')
                time.sleep(POLL_INTERVAL)
        print('Polling timed out.')
        return None

    def _collect_batch_results(self, upload_summary: Dict[str, Any]) -> List[Dict[str, Any]]:
        all_results = list(upload_summary.get('early_failures', []))
        batch_jobs = upload_summary.get('batch_jobs', [])
        for job in batch_jobs:
            results = self.poll_results(job['batch_id'], batch_index=job['batch_index'], total_batches=job['total_batches'])
            if results:
                all_results.extend(results)
                continue
            detail = f"Batch {job['batch_index']}/{job['total_batches']} result polling failed or timed out."
            print(detail)
            for filename in job['files']:
                self._write_failure_marker(filename, detail)
                all_results.append(self._build_failed_result(filename, detail, 'batch_poll_failed'))
        return all_results

    def _write_process_report(self, results: List[Dict[str, Any]]) -> str:
        report_path = os.path.join(OUTPUT_FOLDER, 'process_report.txt')
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write('Filename | Status | Download URL / Error\n')
            f.write('-' * 50 + '\n')
            for item in results:
                if item.get('state') == 'done':
                    msg = item.get('full_zip_url', 'N/A')
                else:
                    msg = item.get('err_msg', 'Unknown Error')
                f.write(f"{item.get('file_name', 'unknown.pdf')} | {item.get('state')} | {msg}\n")
        return report_path

    def download_and_extract_checked(self, results: List[Dict[str, Any]]) -> Dict[str, int]:
        print('\n[Step 3/3] Downloading and extracting results...')
        report_path = self._write_process_report(results)
        print(f'Process report saved to: {report_path}')
        success_items = [item for item in results if item.get('state') == 'done' and item.get('full_zip_url')]
        failed_items = [item for item in results if item.get('state') != 'done' or not item.get('full_zip_url')]
        for item in failed_items:
            detail = item.get('err_msg') or f"MinerU state={item.get('state', 'unknown')}"
            try:
                self._write_failure_marker(item.get('file_name', 'unknown.pdf'), detail)
            except Exception as e:
                print(f'failed to write failure marker: {e}')
        if not success_items:
            print('No successful Mineru result packages are available for download.')
            return {'success': 0, 'ready': 0, 'extracted': 0, 'failed': len(failed_items)}
        extracted_count = 0
        failed_count = len(failed_items)
        for idx, item in enumerate(success_items, start=1):
            file_name = item['file_name']
            download_url = item['full_zip_url']
            base_name = os.path.splitext(file_name)[0]
            zip_path = os.path.join(OUTPUT_FOLDER, f'{base_name}_result.zip')
            extract_path = os.path.join(OUTPUT_FOLDER, base_name)
            print(f'[{idx}/{len(success_items)}] Processing: {file_name}')
            print('  Downloading...', end='', flush=True)
            ok, error_detail = self._download_result_zip(download_url, zip_path)
            if ok:
                print('done')
                wait_if_pause_requested(f'after MinerU result download: {file_name}')
            else:
                print(f'failed ({error_detail})')
                failed_count += 1
                self._write_failure_marker(file_name, f'Download error: {error_detail}')
                continue
            try:
                print('  Extracting...', end='', flush=True)
                os.makedirs(extract_path, exist_ok=True)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_path)
                print('done')
                extracted_count += 1
                self._clear_failure_marker(file_name)
                wait_if_pause_requested(f'after MinerU result extraction: {file_name}')
            except Exception as e:
                print(f'extract error: {e}')
                failed_count += 1
                self._write_failure_marker(file_name, f'Extract error: {e}')
        print(f'Mineru Summary | ready={len(success_items)} extracted={extracted_count} failed={failed_count}')
        return {'success': 1 if extracted_count > 0 else 0, 'ready': len(success_items), 'extracted': extracted_count, 'failed': failed_count}

    def download_and_extract(self, results: List[Dict[str, Any]]) -> None:
        self.download_and_extract_checked(results)

    def run_checked(self) -> bool:
        print('Starting Mineru processing...')
        print(f'Input folder: {INPUT_FOLDER}')
        print(f'Model version: {MODEL_VERSION}')
        upload_summary = self.upload_pdfs()
        if not upload_summary:
            return False
        results = self._collect_batch_results(upload_summary)
        if not results:
            return False
        summary = self.download_and_extract_checked(results)
        if summary['extracted'] == 0:
            print(f"Mineru output unavailable: ready={summary['ready']} extracted={summary['extracted']} failed={summary['failed']}")
            return False
        if summary['failed'] > 0:
            print(f"Mineru completed with partial failures: ready={summary['ready']} extracted={summary['extracted']} failed={summary['failed']}")
        print('Mineru processing completed. Check output_result for artifacts.')
        return True

    def run(self) -> None:
        self.run_checked()
if __name__ == '__main__':
    processor = MineruAutoProcessor()
    sys.exit(0 if processor.run_checked() else 1)
