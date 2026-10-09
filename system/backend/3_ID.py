import json
import os

def _failure_marker_path(output_json_path):
    base_name = os.path.splitext(os.path.basename(output_json_path))[0]
    return os.path.join(os.path.dirname(output_json_path), f'{base_name}_FAILURE.txt')

def get_llm_input_string(cleaned_blocks):
    return '\n\n'.join([b['llm_text'] for b in cleaned_blocks])

def preprocess_content_list_final(input_file_path, output_json_path):
    with open(input_file_path, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)
    cleaned_blocks = []
    global_id_counter = 0
    ignore_types = {'header', 'footer', 'page_number', 'page_footnote'}
    for item in raw_data:
        item_type = item.get('type', 'unknown')
        if item_type in ignore_types:
            continue
        if item_type == 'list' and item.get('sub_type') == 'ref_text':
            continue
        content = ''
        display_prefix = ''
        if item_type == 'text':
            content = item.get('text', '').strip()
            if not content:
                continue
            if item.get('text_level'):
                content = f'## {content}'
        elif item_type == 'image':
            captions = item.get('image_caption', [])
            if not captions:
                continue
            caption_str = ' '.join(captions)
            content = caption_str
            display_prefix = '[FIGURE CAPTION]'
        elif item_type == 'table':
            caption = ' '.join(item.get('table_caption', []))
            footnotes = ' '.join(item.get('table_footnote', []))
            body = item.get('table_body', '')
            table_parts = []
            if caption:
                table_parts.append(f'Caption: {caption}')
            if body:
                table_parts.append(f'Body: {body}')
            if footnotes:
                table_parts.append(f'Footnotes: {footnotes}')
            content = '\n'.join(table_parts)
            display_prefix = '[TABLE]'
        elif item_type == 'equation':
            content = item.get('text', '')
            display_prefix = '[EQUATION]'
        elif item_type == 'list':
            items = item.get('list_items', [])
            content = '\n- '.join(items)
            display_prefix = '[LIST]'
        if not content.strip():
            continue
        clean_block = {'id': global_id_counter, 'type': item_type, 'llm_text': f'[ID: {global_id_counter}] {display_prefix} {content}'.strip(), 'meta': {'page': item.get('page_idx'), 'bbox': item.get('bbox')}}
        cleaned_blocks.append(clean_block)
        global_id_counter += 1
    with open(output_json_path, 'w', encoding='utf-8') as f:
        json.dump(cleaned_blocks, f, indent=2, ensure_ascii=False)
    output_md_path = os.path.splitext(output_json_path)[0] + '.md'
    llm_text_content = get_llm_input_string(cleaned_blocks)
    with open(output_md_path, 'w', encoding='utf-8') as f:
        f.write(llm_text_content)
    print(f'处理完成: {os.path.basename(input_file_path)}')
    print(f'  - ID范围: 0 - {global_id_counter - 1}')
    return cleaned_blocks
if __name__ == '__main__':
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
    _DATA_DIR = os.environ.get('TDM_DATA_DIR') or os.path.join(_PROJECT_ROOT, 'data')
    input_folder_path = os.path.join(_DATA_DIR, '02_intermediate_json')
    output_folder_path = os.path.join(_DATA_DIR, '03_tagged_content')
    if not os.path.exists(output_folder_path):
        os.makedirs(output_folder_path)
    if os.path.exists(input_folder_path):
        processed_count = 0
        for filename in os.listdir(input_folder_path):
            if filename.endswith('.json'):
                input_full_path = os.path.join(input_folder_path, filename)
                output_full_path = os.path.join(output_folder_path, filename)
                try:
                    preprocess_content_list_final(input_full_path, output_full_path)
                    fail_marker = _failure_marker_path(output_full_path)
                    if os.path.exists(fail_marker):
                        os.remove(fail_marker)
                    processed_count += 1
                except Exception as e:
                    fail_marker = _failure_marker_path(output_full_path)
                    with open(fail_marker, 'w', encoding='utf-8') as f:
                        f.write(f'Step 3 failed for {filename}\n')
                        f.write(f'Detail: {e}\n')
                    print(f'处理文件 {filename} 时出错: {e}')
        if processed_count == 0:
            print('警告: 文件夹中没有找到 .json 文件')
    else:
        print(f'错误: 输入文件夹不存在: {input_folder_path}')
