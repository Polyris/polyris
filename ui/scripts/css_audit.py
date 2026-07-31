#!/usr/bin/env python3
"""
CSS Dead Code Audit — finds CSS classes defined in styles/modules/ 
that are not referenced in any TSX/TS source file.

Usage:  python3 scripts/css_audit.py
Output: List of potentially unused CSS classes grouped by source file.

Note: Some classes may be used by:
  - ReactFlow (adds .react-flow__node, .react-flow__edge etc.)
  - Dynamic class construction (e.g., `task-icon-${status}`)
  - CSS nesting / parent selectors
Review results manually before removing.
"""

import re
import os
import glob
import sys

def main():
    ui_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src_dir = os.path.join(ui_dir, 'src')
    styles_dir = os.path.join(src_dir, 'styles', 'modules')
    
    # Extract CSS class selectors
    css_classes = set()
    css_class_sources = {}
    for f in sorted(glob.glob(os.path.join(styles_dir, '*.css'))):
        fname = os.path.basename(f)
        with open(f) as fh:
            for m in re.finditer(r'\.([a-zA-Z_][a-zA-Z0-9_-]+)', fh.read()):
                css_classes.add(m.group(1))
                css_class_sources.setdefault(m.group(1), set()).add(fname)

    # Read all TSX/TS content
    tsx_content = ""
    for root, dirs, files in os.walk(src_dir):
        if 'styles' in root or 'node_modules' in root:
            continue
        for f in files:
            if f.endswith('.tsx') or f.endswith('.ts'):
                with open(os.path.join(root, f)) as fh:
                    tsx_content += fh.read()

    # Check usage
    unused_by_file = {}
    used_count = 0
    for cls in sorted(css_classes):
        if cls in tsx_content:
            used_count += 1
        else:
            for src in css_class_sources.get(cls, set()):
                unused_by_file.setdefault(src, []).append(cls)

    unused_count = len(css_classes) - used_count
    
    print(f"Total unique CSS classes: {len(css_classes)}")
    print(f"Used in TSX/TS: {used_count}")
    print(f"Potentially unused: {unused_count}")
    print()
    
    for f in sorted(unused_by_file.keys()):
        classes = unused_by_file[f]
        print(f"{f}: {len(classes)} unused")
        for c in classes:
            print(f"  .{c}")
        print()
    
    return 0 if unused_count == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
