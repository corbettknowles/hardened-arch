#!/usr/bin/env python3
"""
fix_headers.py
Unified Cross Compiler — Header Guard and Include Path Fixer
Created by Corbett Knowles 2026
License: GPL v3.0

Run from project root:
  python3 fix_headers.py

Targets:
  backend/Core/adt/
  backend/Core/support/

Fixes:
  - Corrupted header guards (include path format -> UPPER_UNDERSCORE format)
  - Missing #define after #ifndef
  - Missing or wrong #endif comment
  - llvm/ include paths -> core/ paths
  - namespace llvm -> namespace ucc
"""

import os
import re
import sys

# Target directories relative to where script is run
# Adjust separators for Windows
TARGET_DIRS = [
    os.path.join("backend", "Core", "adt"),
    os.path.join("backend", "Core", "support"),
]

def filename_to_guard(filename, subdir):
    """Convert filename to proper header guard name.
    e.g. TypeSize.h in support -> CORE_SUPPORT_TYPESIZE_H
         SmallVector.h in adt  -> CORE_ADT_SMALLVECTOR_H
    """
    base = os.path.basename(filename)
    name = base.replace('.', '_').replace('-', '_').upper()
    if 'adt' in subdir.lower():
        return f"CORE_ADT_{name}"
    elif 'support' in subdir.lower():
        return f"CORE_SUPPORT_{name}"
    else:
        return f"CORE_{name}"

def fix_corrupted_guard(line, guard_name):
    """Fix header guard lines that got turned into path format."""
    # Fix: #ifndef core/support/typesize_h -> #ifndef CORE_SUPPORT_TYPESIZE_H
    # Fix: #ifndef core/adt/smallvector_h  -> #ifndef CORE_ADT_SMALLVECTOR_H
    # Also fix LLVM_ prefix guards
    ifndef_match = re.match(r'^(#ifndef\s+)(.*)', line)
    if ifndef_match:
        prefix = ifndef_match.group(1)
        guard = ifndef_match.group(2).strip()
        # If guard looks like a path or has llvm prefix, replace it
        if '/' in guard or guard.upper().startswith('LLVM_'):
            return f"{prefix}{guard_name}"
    
    define_match = re.match(r'^(#define\s+)(.*)', line)
    if define_match:
        prefix = define_match.group(1)
        guard = define_match.group(2).strip()
        if '/' in guard or guard.upper().startswith('LLVM_'):
            return f"{prefix}{guard_name}"

    endif_match = re.match(r'^#endif', line)
    if endif_match:
        return f"#endif // {guard_name}"

    return line

def fix_header(filepath, subdir):
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    guard_name = filename_to_guard(filepath, subdir)
    new_lines = []
    i = 0
    ifndef_found = False
    define_found = False
    original = ''.join(lines)

    while i < len(lines):
        line = lines[i].rstrip('\n').rstrip('\r')

        # Fix corrupted header guards
        line = fix_corrupted_guard(line, guard_name)

        # Fix include paths: llvm/Support/ -> core/support/
        line = re.sub(r'#include\s+"llvm/[Ss]upport/', '#include "core/support/', line)
        line = re.sub(r'#include\s+"llvm/[Aa][Dd][Tt]/', '#include "core/adt/', line)
        line = re.sub(r'#include\s+"llvm/', '#include "core/', line)

        # Fix namespace
        line = re.sub(r'\bnamespace\s+llvm\b', 'namespace ucc', line)
        line = re.sub(r'(//\s*)namespace\s+llvm\b', r'\1namespace ucc', line)

        # Track #ifndef
        if re.match(r'^#ifndef\s+', line):
            ifndef_found = True
            new_lines.append(line + '\n')
            i += 1
            # Check if next non-blank line is #define
            j = i
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            next_line = lines[j].rstrip() if j < len(lines) else ''
            if re.match(r'^#define\s+', next_line):
                define_found = True
            else:
                # Insert missing #define
                new_lines.append(f'#define {guard_name}\n')
                define_found = True
            continue

        new_lines.append(line + '\n')
        i += 1

    new_content = ''.join(new_lines)

    if new_content != original:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True
    return False


def process_directory(directory):
    fixed = 0
    skipped = 0
    errors = 0
    
    for root, dirs, files in os.walk(directory):
        for filename in sorted(files):
            if filename.endswith('.h'):
                filepath = os.path.join(root, filename)
                try:
                    if fix_header(filepath, directory):
                        print(f"  FIXED:   {filename}")
                        fixed += 1
                    else:
                        skipped += 1
                except Exception as e:
                    print(f"  ERROR:   {filename} — {e}")
                    errors += 1

    return fixed, skipped, errors


if __name__ == '__main__':
    # Can override target dirs via command line args
    targets = sys.argv[1:] if len(sys.argv) > 1 else TARGET_DIRS

    total_fixed = 0
    total_skipped = 0
    total_errors = 0

    for target in targets:
        if not os.path.isdir(target):
            print(f"WARNING: Directory not found, skipping: {target}")
            continue
        print(f"\nProcessing: {target}")
        print("-" * 50)
        f, s, e = process_directory(target)
        total_fixed += f
        total_skipped += s
        total_errors += e

    print("\n" + "=" * 50)
    print(f" TOTAL — Fixed: {total_fixed}  Unchanged: {total_skipped}  Errors: {total_errors}")
    print("=" * 50)
