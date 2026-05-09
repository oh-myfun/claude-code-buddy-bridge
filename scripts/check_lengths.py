#!/usr/bin/env python3
"""Check all decoded JS pose strings are exactly 12 characters."""
import os, re

def decode_js_str(s):
    result = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            nc = s[i + 1]
            if nc == '\\': result.append('\\')
            elif nc == "'": result.append("'")
            elif nc == '"': result.append('"')
            elif nc == 'n': result.append('\n')
            elif nc == 't': result.append('\t')
            else: result.append(nc)
            i += 2
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)

buddies_dir = 'src/ccbb/web/static/buddies'
total = 0
bad = 0

for fname in sorted(os.listdir(buddies_dir)):
    if not fname.endswith('.js'):
        continue
    with open(os.path.join(buddies_dir, fname), 'r', encoding='utf-8') as f:
        content = f.read()
    name = fname.replace('.js', '')
    issues = 0
    for m in re.finditer(r"'((?:[^'\\]|\\.)*)'", content):
        decoded = decode_js_str(m.group(1))
        total += 1
        if len(decoded) != 12:
            issues += 1
            bad += 1
            if issues <= 2:
                print(f'  {name} len={len(decoded)}: {repr(decoded)}')
    if issues == 0:
        print(f'{name}: OK')
    else:
        print(f'{name}: {issues} bad strings')

print(f'\nTotal: {total} strings, {bad} with len != 12')
