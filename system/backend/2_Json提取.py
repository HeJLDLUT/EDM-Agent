import os
import shutil

def _failure_marker_path(target_dir, base_name):
    return os.path.join(target_dir, f'{base_name}_FAILURE.txt')

def collect_and_rename_files(source_dir, target_dir):
    if not os.path.exists(source_dir):
        print(f"错误：源文件夹 '{source_dir}' 不存在，请检查路径。")
        return
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)
        print(f'已创建目标文件夹：{target_dir}')
    count = 0
    for root, dirs, files in os.walk(source_dir):
        for file in files:
            if file.endswith('_content_list.json'):
                parent_folder_name = os.path.basename(root)
                source_file_path = os.path.join(root, file)
                new_file_name = f'{parent_folder_name}.json'
                target_file_path = os.path.join(target_dir, new_file_name)
                try:
                    shutil.copy2(source_file_path, target_file_path)
                    fail_marker = _failure_marker_path(target_dir, parent_folder_name)
                    if os.path.exists(fail_marker):
                        os.remove(fail_marker)
                    print(f'[成功] 复制: {file} -> 重命名为: {new_file_name}')
                    count += 1
                except Exception as e:
                    fail_marker = _failure_marker_path(target_dir, parent_folder_name)
                    with open(fail_marker, 'w', encoding='utf-8') as f:
                        f.write(f'Step 2 failed for {new_file_name}\n')
                        f.write(f'Detail: {e}\n')
                    print(f'[失败] 无法复制 {source_file_path}: {e}')
    print(f"\n处理完成！共复制并重命名了 {count} 个文件到 '{target_dir}' 文件夹中。")
if __name__ == '__main__':
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
    _DATA_DIR = os.environ.get('TDM_DATA_DIR') or os.path.join(_PROJECT_ROOT, 'data')
    source_folder = os.path.join(_DATA_DIR, '01_mineru_raw')
    output_folder = os.path.join(_DATA_DIR, '02_intermediate_json')
    collect_and_rename_files(source_folder, output_folder)
