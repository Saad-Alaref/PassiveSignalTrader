import os

def count_lines_in_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return sum(1 for line in f)

def main():
    src_dir = os.path.join(os.path.dirname(__file__), 'src')
    total_lines = 0
    print("Lines of code per file:\n")
    for root, dirs, files in os.walk(src_dir):
        for file in files:
            if file.endswith('.py'):
                full_path = os.path.join(root, file)
                try:
                    lines = count_lines_in_file(full_path)
                    total_lines += lines
                    rel_path = os.path.relpath(full_path, start=os.path.dirname(__file__))
                    print(f"{rel_path}: {lines}")
                except Exception as e:
                    print(f"Error reading {full_path}: {e}")
    print(f"\nTotal lines of code in 'src/': {total_lines}")

if __name__ == "__main__":
    main()