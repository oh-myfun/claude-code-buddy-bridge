#!/usr/bin/env python3
"""Convert claude-desktop-buddy C++ species files to individual JS files."""
import re
import os
import sys

SPECIES = [
    'cat','capybara','duck','goose','blob','dragon',
    'octopus','owl','penguin','turtle','snail','ghost',
    'axolotl','cactus','robot','rabbit','mushroom','chonk'
]
FUNCS = ['doSleep','doIdle','doBusy','doAttention','doCelebrate','doDizzy','doHeart']
STATES = ['sleep','idle','busy','attention','celebrate','dizzy','heart']

def rgb565(v):
    r = ((v >> 11) & 0x1F) * 255 // 31
    g = ((v >> 5) & 0x3F) * 255 // 63
    b = (v & 0x1F) * 255 // 31
    return f'#{r:02X}{g:02X}{b:02X}'

def extract_strings(text):
    """Extract C string literals, decoding escape sequences."""
    result = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            i += 1
            chars = []
            while i < len(text):
                if text[i] == '\\' and i + 1 < len(text):
                    next_ch = text[i + 1]
                    if next_ch == 'n':
                        chars.append('\n')
                    elif next_ch == 't':
                        chars.append('\t')
                    elif next_ch == '\\':
                        chars.append('\\')
                    elif next_ch == '"':
                        chars.append('"')
                    elif next_ch == "'":
                        chars.append("'")
                    else:
                        chars.append(next_ch)
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
        # Find the opening brace of the initializer
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

def js_str(s):
    """Escape a string for JS single-quoted string."""
    return s.replace('\\', '\\\\').replace("'", "\\'")

def convert(src_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for name in SPECIES:
        fp = os.path.join(src_dir, f'{name}.cpp')
        if not os.path.exists(fp):
            print(f'SKIP: {fp}', file=sys.stderr)
            continue

        with open(fp, 'r', encoding='utf-8') as f:
            code = f.read()

        # Species color
        m = re.search(r'"[a-z_]+"\s*,\s*0x([0-9A-Fa-f]+)', code)
        color = rgb565(int(m.group(1), 16)) if m else '#FFFFFF'

        lines = [f"// {name} species data"]
        lines.append("if (typeof SPECIES === 'undefined') var SPECIES = {};")
        lines.append(f"SPECIES.{name} = {{")
        lines.append(f"  color: '{color}',")
        lines.append("  states: {")

        for fn, sn in zip(FUNCS, STATES):
            fc = extract_func(code, fn)
            if not fc:
                print(f'  WARN: {name}.{sn} function not found', file=sys.stderr)
                continue

            poses = parse_poses(fc)
            p_names = parse_p(fc)
            seq = parse_seq(fc)
            td = parse_tick(fc)
            ys = parse_arr(fc, 'Y_SHIFT')
            xs = parse_arr(fc, 'X_SHIFT')
            yb = parse_arr(fc, 'Y_BOB')

            pose_list = [poses[n] for n in p_names if n in poses]

            lines.append(f"    {sn}: {{")
            lines.append(f"      tickDiv: {td},")
            lines.append("      poses: [")
            for p in pose_list:
                lines.append("        [" + ",".join(f"'{js_str(l)}'" for l in p) + "],")
            lines.append("      ],")
            lines.append("      seq: [" + ",".join(str(x) for x in seq) + "],")
            if ys is not None:
                lines.append("      yShift: [" + ",".join(str(x) for x in ys) + "],")
            if xs is not None:
                lines.append("      xShift: [" + ",".join(str(x) for x in xs) + "],")
            if yb is not None:
                lines.append("      yBob: [" + ",".join(str(x) for x in yb) + "],")
            lines.append("    },")

        lines.append("  }")
        lines.append("};")

        out_path = os.path.join(out_dir, f'{name}.js')
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        print(f'OK: {name} -> {out_path} ({len(pose_list)} poses in last state)', file=sys.stderr)

if __name__ == '__main__':
    src = sys.argv[1] if len(sys.argv) > 1 else 'D:/Project/AI/claude-desktop-buddy/src/buddies'
    out = sys.argv[2] if len(sys.argv) > 2 else 'D:/Project/AI/claude-code-buddy-bridge/src/ccbb/web/static/buddies'
    convert(src, out)
