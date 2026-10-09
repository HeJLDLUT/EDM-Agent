import json
import os
import glob
import re
import pandas as pd
from typing import List, Dict, Any, Set
try:
    import tiktoken
    ENCODING = tiktoken.get_encoding('cl100k_base')

    def count_tokens(text: str) -> int:
        return len(ENCODING.encode(text))
    TOKEN_METHOD = 'Exact (tiktoken)'
except ImportError:

    def count_tokens(text: str) -> int:
        return len(text) // 4
    TOKEN_METHOD = 'Estimate (char/4)'
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_DATA_DIR = os.environ.get('TDM_DATA_DIR') or os.path.join(_PROJECT_ROOT, 'data')
INPUT_RAW_DIR = os.path.join(_DATA_DIR, '02_intermediate_json')
INPUT_SOURCE_DIR = os.path.join(_DATA_DIR, '03_tagged_content')
INPUT_LLM_RESPONSE_DIR = os.path.join(_DATA_DIR, '04_relevance_filter')
OUTPUT_CLEAN_MD_DIR = os.path.join(_DATA_DIR, '05_cleaned_markdown')
OUTPUT_REPORT_DIR = os.path.join(_DATA_DIR, '05_reports')
OUTPUT_REJECTED_DIR = os.path.join(_DATA_DIR, '05_rejected_content')

def _failure_marker_path(filename: str) -> str:
    base_name = os.path.splitext(filename)[0]
    return os.path.join(OUTPUT_CLEAN_MD_DIR, f'{base_name}_FAILURE.txt')

def _write_failure_marker(filename: str, detail: str):
    with open(_failure_marker_path(filename), 'w', encoding='utf-8') as f:
        f.write(f'Step 5 failed for {filename}\n')
        f.write(f'Detail: {detail}\n')

def _clear_failure_marker(filename: str):
    marker = _failure_marker_path(filename)
    if os.path.exists(marker):
        os.remove(marker)

def clean_json_string(json_str: str) -> str:
    if '```' in json_str:
        pattern = '```(?:json)?\\s*(\\{.*?\\})\\s*```'
        match = re.search(pattern, json_str, re.DOTALL)
        if match:
            return match.group(1)
    return json_str

def load_json_robust(path: str) -> Any:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
            cleaned_content = clean_json_string(content)
            return json.loads(cleaned_content)
    except Exception:
        return None

def clean_llm_text_overhead(text: str) -> str:
    pattern = '^\\[ID:\\s*\\d+\\]\\s*(?:\\[[^\\]]+\\]\\s*)?'
    return re.sub(pattern, '', text)

def extract_raw_content_tokens(raw_data: List[Dict]) -> int:
    total_text = ''
    for item in raw_data:
        item_type = item.get('type', '')
        content = ''
        if item_type in ['text', 'header', 'footer', 'equation', 'page_number', 'page_footnote']:
            content = item.get('text', '')
        elif item_type == 'image':
            content = ' '.join(item.get('image_caption', []))
        elif item_type == 'table':
            parts = []
            parts.extend(item.get('table_caption', []))
            parts.append(item.get('table_body', ''))
            parts.extend(item.get('table_footnote', []))
            content = ' '.join(filter(None, parts))
        elif item_type == 'list':
            content = ' '.join(item.get('list_items', []))
        if content:
            total_text += content + '\n'
    return count_tokens(total_text)

def analyze_continuity(kept_ids: Set[int]) -> float:
    if not kept_ids:
        return 0.0
    sorted_ids = sorted(list(kept_ids))
    segments = 1
    for i in range(1, len(sorted_ids)):
        if sorted_ids[i] != sorted_ids[i - 1] + 1:
            segments += 1
    return round(len(sorted_ids) / segments, 2)

def augment_table_context(kept_ids: Set[int], source_data: List[Dict[str, Any]]) -> Set[int]:
    available_ids = {int(block['id']) for block in source_data}
    augmented = set(kept_ids)
    for block in source_data:
        bid = int(block['id'])
        text = block.get('llm_text', '')
        is_table = block.get('type') == 'table' or '[TABLE]' in text
        if is_table and bid in kept_ids:
            for neighbor_id in (bid - 1, bid + 1):
                if neighbor_id in available_ids:
                    augmented.add(neighbor_id)
    return augmented

def process_filtering():
    for d in [OUTPUT_CLEAN_MD_DIR, OUTPUT_REPORT_DIR, OUTPUT_REJECTED_DIR]:
        os.makedirs(d, exist_ok=True)
    source_files = glob.glob(os.path.join(INPUT_SOURCE_DIR, '*.json'))
    stats_list = []
    all_rejected_buffer = []
    print(f'🚀 开始全链路后处理... 目标文件数: {len(source_files)}')
    print(f'📏 Token 计算方法: {TOKEN_METHOD}')
    has_raw_files = os.path.exists(INPUT_RAW_DIR)
    if not has_raw_files:
        print(f'⚠️ 警告: 未找到原始文件目录 {INPUT_RAW_DIR}，无法计算规则节省率。')
    for src_path in source_files:
        filename = os.path.basename(src_path)
        llm_path = os.path.join(INPUT_LLM_RESPONSE_DIR, filename)
        raw_path = os.path.join(INPUT_RAW_DIR, filename)
        file_stat = {'Filename': filename, 'Status': 'Success', 'Raw_Tokens': 0, 'Inter_Tokens': 0, 'Final_Tokens': 0, 'Rule_Savings(%)': 0.0, 'LLM_Savings(%)': 0.0, 'Total_Savings(%)': 0.0, 'Tables_Kept': '0/0', 'Continuity_Score': 0.0, 'Hallucinations': 0}
        if not os.path.exists(llm_path):
            file_stat['Status'] = 'Missing LLM Resp'
            _write_failure_marker(filename, file_stat['Status'])
            stats_list.append(file_stat)
            continue
        llm_data = load_json_robust(llm_path)
        source_data = load_json_robust(src_path)
        if not source_data:
            file_stat['Status'] = 'Bad Source Data'
            _write_failure_marker(filename, file_stat['Status'])
            stats_list.append(file_stat)
            continue
        if has_raw_files and os.path.exists(raw_path):
            raw_data = load_json_robust(raw_path)
            if raw_data:
                file_stat['Raw_Tokens'] = extract_raw_content_tokens(raw_data)
        if not isinstance(llm_data, dict) or 'kept_ids' not in llm_data:
            file_stat['Status'] = 'Bad LLM Format'
            _write_failure_marker(filename, file_stat['Status'])
            stats_list.append(file_stat)
            continue
        try:
            kept_ids_raw = llm_data.get('kept_ids', [])
            kept_ids_set = set((int(x) for x in kept_ids_raw))
        except (ValueError, TypeError):
            file_stat['Status'] = 'ID Value Error'
            _write_failure_marker(filename, file_stat['Status'])
            stats_list.append(file_stat)
            continue
        original_ids_set = {int(b['id']) for b in source_data}
        hallucinations = kept_ids_set - original_ids_set
        file_stat['Hallucinations'] = len(hallucinations)
        valid_kept_ids = kept_ids_set & original_ids_set
        valid_kept_ids = augment_table_context(valid_kept_ids, source_data)
        total_tables = 0
        for b in source_data:
            raw_text_content = clean_llm_text_overhead(b.get('llm_text', ''))
            file_stat['Inter_Tokens'] += count_tokens(raw_text_content)
            if b.get('type') == 'table' or '[TABLE]' in b.get('llm_text', ''):
                total_tables += 1
        filtered_blocks = []
        rejected_blocks_text = []
        kept_tables = 0
        for block in source_data:
            bid = int(block['id'])
            if bid in valid_kept_ids:
                filtered_blocks.append(block)
                raw_text_content = clean_llm_text_overhead(block.get('llm_text', ''))
                file_stat['Final_Tokens'] += count_tokens(raw_text_content)
                if block.get('type') == 'table' or '[TABLE]' in block.get('llm_text', ''):
                    kept_tables += 1
            else:
                rejected_blocks_text.append(block.get('llm_text', ''))
        file_stat['Tables_Kept'] = f'{kept_tables}/{total_tables}'
        file_stat['Continuity_Score'] = analyze_continuity(valid_kept_ids)
        if file_stat['Inter_Tokens'] > 0:
            llm_savings = (1 - file_stat['Final_Tokens'] / file_stat['Inter_Tokens']) * 100
            file_stat['LLM_Savings(%)'] = round(llm_savings, 2)
        if file_stat['Raw_Tokens'] > 0:
            total_savings = (1 - file_stat['Final_Tokens'] / file_stat['Raw_Tokens']) * 100
            file_stat['Total_Savings(%)'] = round(total_savings, 2)
            rule_savings = (1 - file_stat['Inter_Tokens'] / file_stat['Raw_Tokens']) * 100
            file_stat['Rule_Savings(%)'] = round(rule_savings, 2)
        stats_list.append(file_stat)
        md_content = '\n\n'.join([b['llm_text'] for b in filtered_blocks])
        md_filename = filename.replace('.json', '.md')
        with open(os.path.join(OUTPUT_CLEAN_MD_DIR, md_filename), 'w', encoding='utf-8') as f:
            f.write(md_content)
        _clear_failure_marker(filename)
        if rejected_blocks_text:
            rejected_filename = filename.replace('.json', '_rejected.md')
            header_info = f'Original Filename: {filename}\nTotal Rejected Blocks: {len(rejected_blocks_text)}'
            rejected_header = f'# Rejected Content Report\n{header_info}\n\n' + '=' * 40 + '\n\n'
            file_rejected_content = rejected_header + '\n\n'.join(rejected_blocks_text)
            with open(os.path.join(OUTPUT_REJECTED_DIR, rejected_filename), 'w', encoding='utf-8') as f:
                f.write(file_rejected_content)
            summary_entry = f"## File: {filename}\n**Rejected Count:** {len(rejected_blocks_text)}\n\n{'=' * 20} Content Start {'=' * 20}\n\n{chr(10).join(rejected_blocks_text)}\n\n{'=' * 20} Content End {'=' * 20}\n\n{'-' * 60}\n\n"
            all_rejected_buffer.append(summary_entry)
    if all_rejected_buffer:
        summary_path = os.path.join(OUTPUT_REJECTED_DIR, 'ALL_REJECTED_SUMMARY.md')
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(f'# Global Rejected Content Summary\nTotal Files with Rejections: {len(all_rejected_buffer)}\n\n' + '=' * 60 + '\n\n')
            f.write(''.join(all_rejected_buffer))
        print(f'📦 汇总的被剔除内容已保存至: {summary_path}')
    if not stats_list:
        print('未处理任何文件。')
        return
    df = pd.DataFrame(stats_list)
    df_success = df[df['Status'] == 'Success']
    sum_raw = df['Raw_Tokens'].sum()
    sum_inter = df['Inter_Tokens'].sum()
    sum_final = df['Final_Tokens'].sum()
    global_total_savings = (1 - sum_final / sum_raw) * 100 if sum_raw > 0 else 0
    avg_rule_savings = (1 - sum_inter / sum_raw) * 100 if sum_raw > 0 else 0
    avg_llm_savings = (1 - sum_final / sum_inter) * 100 if sum_inter > 0 else 0
    print('\n' + '=' * 20 + ' 📊 全链路处理概览 ' + '=' * 20)
    print(f'Total Files Processed: {len(df)}')
    print(f'Successful           : {len(df_success)}')
    print(f'Total Raw Tokens     : {sum_raw:,}')
    print(f'Final Kept Tokens    : {sum_final:,}')
    print('-' * 60)
    print(f'平均规则清洗贡献 (ID.py): {avg_rule_savings:.2f}% (免费清洗 - 参考文献/页眉等)')
    print(f'平均 AI 清洗贡献 (LLM)  : {avg_llm_savings:.2f}% (付费清洗 - 语义筛选)')
    print(f'🔥 全局总 Token 节省率 : {global_total_savings:.2f}% (Raw -> Final)')
    print('-' * 60)
    print(f"Avg Continuity Score : {df_success['Continuity_Score'].mean():.2f}")
    print('\n' + '=' * 20 + ' 📝 详细统计 ' + '=' * 20)
    print_cols = ['Filename', 'Total_Savings(%)', 'Rule_Savings(%)', 'LLM_Savings(%)', 'Final_Tokens', 'Tables_Kept', 'Status']
    df_print = df[print_cols].copy()
    df_print['Filename'] = df_print['Filename'].apply(lambda x: x[:20] + '..' if len(x) > 22 else x)
    try:
        print(df_print.to_markdown(index=False, tablefmt='simple'))
    except ImportError:
        print(df_print.to_string(index=False))
    csv_path = os.path.join(OUTPUT_REPORT_DIR, 'full_pipeline_report.csv')
    df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    print(f'\n✅ 完整报告已保存: {csv_path}')
    print(f'🗑️ 被剔除内容已保存至: {OUTPUT_REJECTED_DIR}')
if __name__ == '__main__':
    process_filtering()
