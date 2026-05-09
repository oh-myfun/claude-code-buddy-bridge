#!/usr/bin/env python3
"""Check pose character counts and compare with original C++ source."""
import os, re, sys

def extract_strings(text):
    result = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            i += 1
            chars = []
            while i < len(text):
                if text[i] == '\\' and i + 1 < len(text):
                    chars.append(text[i:i+2])
                    i += 2
                elif text[i] == '"':
                    break
                else:
                    chars.append(text[i])
                    i += 1
            result.append(''.join(chars))
            i += 1
        else:
            i += 1
    return result

def check_js_poses():
    """Check all JS species files for pose character alignment."""
    buddies_dir = 'src/ccbb/web/static/buddies'
    total_issues = 0

    for fname in sorted(os.listdir(buddies_dir)):
        if not fname.endswith('.js'):
            continue
        path = os.path.join(buddies_dir, fname)
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()

        name = fname.replace('.js', '')
        issues = []

        # Find all string literals in poses arrays
        # Each pose is ['line0','line1','line2','line3','line4']
        pose_m = re.finditer(r"\[('[^']*'(?:,'[^']*'){4})\]", content)
        for pi, pm in enumerate(pose_m):
            line_str = pm.group(1)
            lines = re.findall(r"'(.*?)(?<!\\)'", line_str)
            for li, line in enumerate(lines):
                # Unescape JS
                line = line.replace("\\'", "'").replace("\\\\", "\\")
                if len(line) != 12:
                    issues.append(f'  len={len(line)} line[{li}]: "{line}"')
                    total_issues += 1

        if issues:
            print(f'{name}: {len(issues)} issues')
            for i in issues[:3]:
                print(i)
        else:
            print(f'{name}: OK')

    print(f'\nTotal issues: {total_issues}')
    return total_issues

def compare_with_cpp(src_dir='D:/Project/AI/claude-desktop-buddy/src/buddies'):
    """Compare JS data with original C++ source."""
    SPECIES = [
        'cat','capybara','duck','goose','blob','dragon',
        'octopus','owl','penguin','turtle','snail','ghost',
        'axolotl','cactus','robot','rabbit','mushroom','chonk'
    ]
    FUNCS = ['doSleep','doIdle','doBusy','doAttention','doCelebrate','doDizzy','doHeart']
    STATES = ['sleep','idle','busy','attention','celebrate','dizzy','heart']

    total_diff = 0

    for name in SPECIES:
        cpp_path = os.path.join(src_dir, f'{name}.cpp')
        js_path = os.path.join('src/ccbb/web/static/buddies', f'{name}.js')

        if not os.path.exists(cpp_path):
            print(f'{name}: SKIP (no C++ source)')
            continue
        if not os.path.exists(js_path):
            print(f'{name}: SKIP (no JS file)')
            continue

        with open(cpp_path, 'r', encoding='utf-8') as f:
            cpp_code = f.read()
        with open(js_path, 'r', encoding='utf-8') as f:
            js_code = f.read()

        diffs = []

        for fn, sn in zip(FUNCS, STATES):
            # Extract C++ function
            fc = extract_func(cpp_code, fn)
            if not fc:
                continue

            # Parse C++ poses
            cpp_poses = parse_poses(fc)
            p_names = parse_p(fc)
            cpp_seq = parse_seq(fc)
            cpp_tick = parse_tick(fc)
            cpp_ys = parse_arr(fc, 'Y_SHIFT')
            cpp_xs = parse_arr(fc, 'X_SHIFT')
            cpp_yb = parse_arr(fc, 'Y_BOB')

            # Parse JS state
            js_state = parse_js_state(js_code, sn)
            if not js_state:
                diffs.append(f'  {sn}: missing in JS')
                continue

            js_tick = js_state.get('tickDiv', -1)
            js_seq = js_state.get('seq', [])
            js_poses = js_state.get('poses', [])

            # Compare tickDiv
            if js_tick != cpp_tick:
                diffs.append(f'  {sn}: tickDiv JS={js_tick} C++={cpp_tick}')

            # Compare seq
            if js_seq != cpp_seq:
                diffs.append(f'  {sn}: seq mismatch (JS={len(js_seq)} C++={len(cpp_seq)})')

            # Compare poses
            cpp_pose_list = [cpp_poses[n] for n in p_names if n in cpp_poses]
            if len(js_poses) != len(cpp_pose_list):
                diffs.append(f'  {sn}: pose count JS={len(js_poses)} C++={len(cpp_pose_list)}')
            else:
                for i, (jp, cp) in enumerate(zip(js_poses, cpp_pose_list)):
                    if jp != cp:
                        for li, (jl, cl) in enumerate(zip(jp, cp)):
                            if jl != cl:
                                diffs.append(f'  {sn} pose[{i}] line[{li}]: JS="{jl}" C++="{cl}"')
                                total_diff += 1

            # Compare transforms
            if cpp_ys is not None:
                js_ys = js_state.get('yShift')
                if js_ys != cpp_ys:
                    diffs.append(f'  {sn}: yShift mismatch')
            if cpp_xs is not None:
                js_xs = js_state.get('xShift')
                if js_xs != cpp_xs:
                    diffs.append(f'  {sn}: xShift mismatch')
            if cpp_yb is not None:
                js_yb = js_state.get('yBob')
                if js_yb != cpp_yb:
                    diffs.append(f'  {sn}: yBob mismatch')

        if diffs:
            print(f'\n{name}: {len(diffs)} diffs')
            for d in diffs[:10]:
                print(d)
            if len(diffs) > 10:
                print(f'  ... and {len(diffs)-10} more')
        else:
            print(f'{name}: MATCH')

    print(f'\nTotal pose line diffs: {total_diff}')

def extract_func(code, name):
    pat = rf'static\s+void\s+{name}\s*\(uint32_t\s+t\)\s*\{{'
    m = re.search(pat, code)
    if not m: return None
    depth = 0
    for i in range(m.end() - 1, len(code)):
        if code[i] == '{': depth += 1
        elif code[i] == '}':
            depth -= 1
            if depth == 0: return code[m.start():i+1]
    return None

def parse_poses(fc):
    poses = {}
    for m in re.finditer(r'static\s+const\s+char\*\s+const\s+(\w+)\[5\]', fc):
        name = m.group(1)
        rest = fc[m.end():]
        brace = rest.find('{')
        semi = rest.find(';')
        if brace < 0 or semi < 0 or brace > semi:
            continue
        chunk = rest[brace:semi+1]
        strs = extract_strings(chunk)
        if len(strs) >= 5:
            poses[name] = strs[:5]
    return poses

def parse_p(fc):
    m = re.search(r'const\s+char\*\s+const\*\s+P\[\d+\]\s*=\s*\{([^}]+)\}', fc)
    return [n.strip() for n in m.group(1).split(',')] if m else []

def parse_seq(fc):
    m = re.search(r'static\s+const\s+uint8_t\s+SEQ\[\]\s*=\s*\{([^}]+)\}', fc)
    return [int(x) for x in m.group(1).split(',') if x.strip()] if m else []

def parse_tick(fc):
    m = re.search(r'\(t\s*/\s*(\d+)\)', fc)
    return int(m.group(1)) if m else 5

def parse_arr(fc, name):
    m = re.search(rf'static\s+const\s+int8_t\s+{name}\[\]\s*=\s*\{{([^}}]+)\}}', fc)
    return [int(x) for x in m.group(1).split(',') if x.strip()] if m else None

def parse_js_state(js_code, state_name):
    m = re.search(rf'{state_name}:\s*\{{(.*?)\}}\s*,', js_code, re.DOTALL)
    if not m:
        m = re.search(rf'{state_name}:\s*\{{(.*)\}}\s*\}}', js_code, re.DOTALL)
    if not m:
        return None

    block = m.group(1)

    result = {}

    # tickDiv
    td = re.search(r'tickDiv:\s*(\d+)', block)
    if td:
        result['tickDiv'] = int(td.group(1))

    # seq
    seq_m = re.search(r'seq:\s*\[([^\]]+)\]', block)
    if seq_m:
        result['seq'] = [int(x.strip()) for x in seq_m.group(1).split(',') if x.strip()]

    # poses - parse as list of lists
    poses = []
    for pm in re.finditer(r'\[([^\]]+)\]', block):
        line_str = pm.group(1)
        lines = re.findall(r"'(.*?)(?<!\\)'", line_str)
        if len(lines) == 5:
            poses.append(lines)
    result['poses'] = poses

    # yShift, xShift, yBob
    for arr_name in ['yShift', 'xShift', 'yBob']:
        arr_m = re.search(rf'{arr_name}:\s*\[([^\]]+)\]', block)
        if arr_m:
            result[arr_name] = [int(x.strip()) for x in arr_m.group(1).split(',') if x.strip()]

    return result

if __name__ == '__main__':
    print('=== Checking JS pose character counts ===')
    check_js_poses()
    print()
    print('=== Comparing JS vs C++ source ===')
    src_dir = sys.argv[1] if len(sys.argv) > 1 else 'D:/Project/AI/claude-desktop-buddy/src/buddies'
    compare_with_cpp(src_dir)
