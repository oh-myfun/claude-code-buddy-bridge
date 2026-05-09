#!/usr/bin/env python3
"""Accurate comparison: decode both C++ and JS strings, then compare."""
import re, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from convert_buddies import extract_strings, extract_func, parse_poses, parse_p, parse_seq, parse_tick, parse_arr

SPECIES = [
    'cat','capybara','duck','goose','blob','dragon',
    'octopus','owl','penguin','turtle','snail','ghost',
    'axolotl','cactus','robot','rabbit','mushroom','chonk'
]
FUNCS = ['doSleep','doIdle','doBusy','doAttention','doCelebrate','doDizzy','doHeart']
STATES = ['sleep','idle','busy','attention','celebrate','dizzy','heart']


def decode_js_str(s):
    """Decode JS single-quoted string escape sequences."""
    result = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            nc = s[i + 1]
            if nc == '\\':
                result.append('\\')
            elif nc == "'":
                result.append("'")
            elif nc == 'n':
                result.append('\n')
            elif nc == 't':
                result.append('\t')
            elif nc == '"':
                result.append('"')
            else:
                result.append(nc)
            i += 2
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)


def parse_js_state(js_code, state_name):
    """Parse a state block from JS file, returning decoded data."""
    # Match state: { ... },
    pat = rf'{state_name}:\s*\{{(.*?)\}},\s*\w+:'
    m = re.search(pat, js_code, re.DOTALL)
    if not m:
        # Try last state (no trailing comma + state name)
        pat2 = rf'{state_name}:\s*\{{(.*)\}}\s*\}}'
        m = re.search(pat2, js_code, re.DOTALL)
    if not m:
        return None
    block = m.group(1)

    result = {}
    td = re.search(r'tickDiv:\s*(\d+)', block)
    result['tickDiv'] = int(td.group(1)) if td else -1

    seq_m = re.search(r'seq:\s*\[([^\]]+)\]', block)
    result['seq'] = [int(x.strip()) for x in seq_m.group(1).split(',') if x.strip()] if seq_m else []

    # Parse poses: arrays of 5 single-quoted strings
    poses = []
    # Match [...] blocks that contain pose lines
    for pm in re.finditer(r"\[([^]]+)\]", block):
        inner = pm.group(1)
        # Extract single-quoted strings (handling escapes)
        lines = re.findall(r"'((?:[^'\\]|\\.)*)'", inner)
        if len(lines) == 5:
            poses.append([decode_js_str(l) for l in lines])
    result['poses'] = poses

    for arr_name in ['yShift', 'xShift', 'yBob']:
        arr_m = re.search(rf'{arr_name}:\s*\[([^\]]+)\]', block)
        if arr_m:
            result[arr_name] = [int(x.strip()) for x in arr_m.group(1).split(',') if x.strip()]

    return result


def main():
    src_dir = sys.argv[1] if len(sys.argv) > 1 else 'D:/Project/AI/claude-desktop-buddy/src/buddies'
    js_dir = 'src/ccbb/web/static/buddies'

    total_diff = 0
    for name in SPECIES:
        cpp_path = os.path.join(src_dir, f'{name}.cpp')
        js_path = os.path.join(js_dir, f'{name}.js')
        if not os.path.exists(cpp_path) or not os.path.exists(js_path):
            continue

        with open(cpp_path, 'r', encoding='utf-8') as f:
            cpp_code = f.read()
        with open(js_path, 'r', encoding='utf-8') as f:
            js_code = f.read()

        diffs = []
        for fn, sn in zip(FUNCS, STATES):
            fc = extract_func(cpp_code, fn)
            if not fc:
                continue

            cpp_poses = parse_poses(fc)
            p_names = parse_p(fc)
            cpp_pose_list = [cpp_poses[n] for n in p_names if n in cpp_poses]
            cpp_seq = parse_seq(fc)
            cpp_tick = parse_tick(fc)
            cpp_ys = parse_arr(fc, 'Y_SHIFT')
            cpp_xs = parse_arr(fc, 'X_SHIFT')
            cpp_yb = parse_arr(fc, 'Y_BOB')

            js_state = parse_js_state(js_code, sn)
            if not js_state:
                diffs.append(f'  {sn}: missing in JS')
                continue

            if js_state['tickDiv'] != cpp_tick:
                diffs.append(f'  {sn}: tickDiv JS={js_state["tickDiv"]} C++={cpp_tick}')
            if js_state['seq'] != cpp_seq:
                diffs.append(f'  {sn}: seq mismatch (JS={len(js_state["seq"])} C++={len(cpp_seq)})')
            if len(js_state['poses']) != len(cpp_pose_list):
                diffs.append(f'  {sn}: pose count JS={len(js_state["poses"])} C++={len(cpp_pose_list)}')
            else:
                for i, (jp, cp) in enumerate(zip(js_state['poses'], cpp_pose_list)):
                    if jp != cp:
                        for li, (jl, cl) in enumerate(zip(jp, cp)):
                            if jl != cl:
                                total_diff += 1
                                if len(diffs) < 3:
                                    diffs.append(
                                        f'  {sn} pose[{i}] line[{li}]: '
                                        f'JS={repr(jl)} C++={repr(cl)}'
                                    )

            for arr_name, cpp_arr in [('yShift', cpp_ys), ('xShift', cpp_xs), ('yBob', cpp_yb)]:
                if cpp_arr is not None and js_state.get(arr_name) != cpp_arr:
                    diffs.append(f'  {sn}: {arr_name} mismatch')

        if diffs:
            print(f'{name}: {len(diffs)} diffs')
            for d in diffs[:5]:
                print(d)
            if len(diffs) > 5:
                print(f'  ... and {len(diffs) - 5} more')
        else:
            print(f'{name}: MATCH')

    print(f'\nTotal line diffs: {total_diff}')


if __name__ == '__main__':
    main()
