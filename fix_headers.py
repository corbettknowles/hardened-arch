#!/usr/bin/env python3
"""
fix_headers.py
Unified Cross Compiler — Header Guard and Include Path Fixer
Created by Corbett Knowles 2026
License: GPL v3.0

Fixes adapted LLVM Support headers:
  - Replaces LLVM_SUPPORT_ guard prefix with CORE_SUPPORT_
  - Adds missing #define after #ifndef if absent
  - Fixes closing #endif comment
  - Replaces llvm/ include paths with core/ paths
  - Replaces namespace llvm with namespace ucc
"""

import os
import re
import sys

def fix_header(filepath):
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    filename = os.path.basename(filepath)
    # Build the guard name from filename
    # e.g. TypeSize.h -> CORE_SUPPORT_TYPESIZE_H
    guard_name = "CORE_SUPPORT_" + filename.replace('.', '_').replace('-', '_').upper()

    original = content
    lines = content.splitlines()
    new_lines = []
    i = 0
    ifndef_found = False
    define_found = False
    ifndef_line_idx = -1

    while i < len(lines):
        line = lines[i]

        # Fix LLVM_SUPPORT_ -> CORE_SUPPORT_ in guards
        line = re.sub(r'LLVM_SUPPORT_', 'CORE_SUPPORT_', line)

        # Fix llvm/ include paths -> core/
        line = re.sub(r'#include\s+"llvm/Support/', '#include "core/support/', line)
        line = re.sub(r'#include\s+"llvm/ADT/', '#include "core/adt/', line)
        line = re.sub(r'#include\s+"llvm/', '#include "core/', line)

        # Fix namespace llvm -> namespace ucc
        line = re.sub(r'\bnamespace\s+llvm\b', 'namespace ucc', line)
        line = re.sub(r'//\s*namespace\s+llvm\b', '// namespace ucc', line)

        # Track #ifndef
        ifndef_match = re.match(r'^#ifndef\s+(\S+)', line)
        if ifndef_match:
            ifndef_found = True
            ifndef_line_idx = len(new_lines)
            new_lines.append(line)
            i += 1
            # Check if next non-empty line is #define
            j = i
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            if j < len(lines) and re.match(r'^#define\s+' + re.escape(ifndef_match.group(1)), lines[j]):
                define_found = True
            else:
                # Missing #define — insert it
                define_name = re.sub(r'LLVM_SUPPORT_', 'CORE_SUPPORT_', ifndef_match.group(1))
                new_lines.append(f'#define {define_name}')
                define_found = True
            continue

        # Fix closing #endif — add comment if missing or wrong
        endif_match = re.match(r'^#endif\s*$', line)
        if endif_match:
            line = f'#endif // {guard_name}'

        endif_comment_match = re.match(r'^#endif\s+//\s*LLVM_', line)
        if endif_comment_match:
            line = f'#endif // {guard_name}'

        new_lines.append(line)
        i += 1

    new_content = '\n'.join(new_lines)
    if not new_content.endswith('\n'):
        new_content += '\n'

    if new_content != original:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True
    return False


def process_directory(directory):
    fixed = 0
    skipped = 0
    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith('.h'):
                filepath = os.path.join(root, filename)
                try:
                    if fix_header(filepath):
                        print(f"  FIXED: {filepath}")
                        fixed += 1
                    else:
                        skipped += 1
                except Exception as e:
                    print(f"  ERROR: {filepath} — {e}")
    print(f"\nDone. Fixed: {fixed}  Unchanged: {skipped}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 fix_headers.py <directory>")
        print("Example: python3 fix_headers.py backend/core/support")
        sys.exit(1)

    target = sys.argv[1]
    if not os.path.isdir(target):
        print(f"Error: {target} is not a directory")
        sys.exit(1)

    print(f"Processing: {target}")
    process_directory(target)
