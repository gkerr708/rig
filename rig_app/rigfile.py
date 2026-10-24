"""The .rig text format.

    # comment
    in  = hw_USB_0:capture_AUX1              # port name patterns (substring / glob)
    out = Scarlett*:playback_FL, Scarlett*:playback_FR
    latency = 64                             # buffer size in samples (lower = less delay)
    volume = -3dB                            # master volume at the end of the chain

    [amp] tone3000                           # [label] plugin  [off]
      preset = metal_rythm-amp
      inputLevel = 0.6

    [delay] slap_delay_stereo  off
      g_out = -3dB                           # gain settings accept dB
      mode  = FFT                            # enumerations accept their labels

Blocks are wired in file order. `split` / `|` / `join` run paths side by side
and mix them; an empty path is the dry signal:

    split
      [verb] gx_reverb_stereo
    |
    join
"""
import difflib
import math
import os
import re
from dataclasses import dataclass, field

HEADER = re.compile(r'^\[([A-Za-z0-9_.-]+)\]\s*(\S+)?\s*(.*)$')
KEYVAL = re.compile(r'^([A-Za-z0-9_.:-]+)\s*=\s*(.*)$')
FLAGS_OFF = {'off', 'bypass', 'bypassed'}


@dataclass
class Entry:
    key: str
    raw: str
    line: int


@dataclass
class Block:
    label: str
    ref: str
    bypass: bool
    line: int                  # header line (0-based)
    end: int                   # last line belonging to the block
    entries: list = field(default_factory=list)


@dataclass
class Split:
    """Paths that run side by side; each path is a list of labels and Splits."""
    line: int
    paths: list = field(default_factory=lambda: [[]])


@dataclass
class Rig:
    ins: list = field(default_factory=list)
    outs: list = field(default_factory=list)
    blocks: list = field(default_factory=list)   # every block, in file order
    chain: list = field(default_factory=list)    # labels and Splits: how they are wired
    errors: list = field(default_factory=list)   # [(line, message)]
    latency: int = 64                             # PipeWire buffer size in samples
    volume: float = 0.0                           # master volume, dB (None = silent)

    def block_at(self, line):
        return next((b for b in self.blocks if b.line <= line <= b.end), None)


def strip_comment(text):
    quoted = False
    for i, ch in enumerate(text):
        if ch == '"':
            quoted = not quoted
        elif ch == '#' and not quoted:
            return text[:i]
    return text


def parse(text):
    rig = Rig()
    block = None
    labels = set()
    path = rig.chain             # where the next block goes
    splits = []                  # open splits, innermost last
    stops = []                   # split/|/join lines: blocks end before them
    for n, raw in enumerate(text.splitlines()):
        line = strip_comment(raw).strip()
        if not line:
            continue
        if line in ('split', '|', 'join'):
            stops.append(n)
            block = None
            if line == 'split':
                splits.append(Split(n))
                path.append(splits[-1])
                path = splits[-1].paths[0]
            elif not splits:
                rig.errors.append((n, f"'{line}' without a 'split' before it"))
            elif line == '|':
                splits[-1].paths.append([])
                path = splits[-1].paths[-1]
            else:
                splits.pop()
                path = splits[-1].paths[-1] if splits else rig.chain
            continue
        if m := HEADER.match(line):
            label, ref, rest = m.groups()
            if not ref:
                rig.errors.append((n, f'[{label}] needs a plugin name after it'))
                ref = ''
            flags = rest.split()
            bad = [f for f in flags if f.lower() not in FLAGS_OFF]
            if bad:
                rig.errors.append((n, f"unknown flag '{bad[0]}' (use 'off' to bypass)"))
            if label in labels:
                rig.errors.append((n, f"label [{label}] is used twice"))
            labels.add(label)
            block = Block(label, ref, any(f.lower() in FLAGS_OFF for f in flags), n, n)
            rig.blocks.append(block)
            path.append(label)
            continue
        m = KEYVAL.match(line)
        if not m:
            rig.errors.append((n, "expected 'name = value' or '[label] plugin'"))
            continue
        key, value = m.group(1), m.group(2).strip()
        if block is None and splits:
            rig.errors.append((n, "settings go under a '[label] plugin' line"))
            continue
        if block is None:
            if key in ('in', 'out'):
                ports = [p.strip() for p in value.split(',') if p.strip()]
                setattr(rig, key + 's', [(p, n) for p in ports])
            elif key == 'latency':
                if value.isdigit() and int(value) in LATENCIES:
                    rig.latency = int(value)
                else:
                    rig.errors.append((n, 'latency is a buffer size in samples: 32, 64, 128, 256…'))
            elif key == 'volume':
                try:
                    rig.volume = parse_volume(value)
                except ValueError as e:
                    rig.errors.append((n, f'volume: {e}'))
            else:
                rig.errors.append((n, f"unknown top-level setting '{key}' (in, out, latency, volume)"))
            continue
        if any(e.key == key for e in block.entries):
            rig.errors.append((n, f"'{key}' is set twice in [{block.label}]"))
        block.entries.append(Entry(key, value, n))
        block.end = n
    for s in splits:
        rig.errors.append((s.line, "'split' needs a 'join' after its paths"))
    # a block extends over trailing blank/comment lines up to the next header or split line
    last = len(text.splitlines()) - 1
    for i, b in enumerate(rig.blocks):
        nxt = [x.line for x in rig.blocks[i + 1:i + 2]] + [x for x in stops if x > b.line]
        b.end = max(b.end, min(nxt) - 1 if nxt else last)
    return rig


MAX_VOLUME = 2.0          # dB; the most Carla's volume goes up (×1.27)
LATENCIES = (16, 32, 64, 128, 256, 512, 1024)


def parse_volume(raw):
    """'-3dB' / '-3' / '-inf' -> dB, or None for silence."""
    low = raw.strip().lower().removesuffix('db').strip()
    if low in ('-inf', 'off', 'mute'):
        return None
    v = float(low)
    if math.isnan(v) or v > MAX_VOLUME:
        raise ValueError(f'{raw} is louder than the most it can go ({fmt_num(MAX_VOLUME)}dB)')
    return v


def fmt_volume(db):
    return '-inf' if db is None or db <= -60 else f'{fmt_num(round(db, 1))}dB'


# ---------------------------------------------------------------- settings

@dataclass
class Setting:
    """One adjustable thing on a plugin: a control port or a patch parameter."""
    key: str
    name: str
    kind: str                  # port | prop
    uri: str = ''              # prop URI
    minimum: float | None = None
    maximum: float | None = None
    default: float | None = None
    integer: bool = False
    toggled: bool = False
    log: bool = False
    gain: bool = False
    unit: str = ''
    enum: list = field(default_factory=list)
    path: bool = False         # takes a file path
    text: bool = False         # takes some other string


def settings_of(plugin):
    out = {}
    for p in plugin.controls:
        out[p.symbol] = Setting(p.symbol, p.name, 'port', minimum=p.minimum, maximum=p.maximum,
                                default=p.default, integer=p.integer, toggled=p.toggled,
                                log=p.log, gain=p.gain, unit=p.unit, enum=p.enum)
    for p in plugin.props:
        if p.key in out:
            continue
        numeric = p.range.endswith(('#Float', '#Double', '#Int', '#Long', '#Bool'))
        out[p.key] = Setting(p.key, p.label, 'prop', uri=p.uri, minimum=p.minimum,
                             maximum=p.maximum, default=p.default, integer=p.integer,
                             toggled=p.toggled, enum=p.enum, path=p.is_path,
                             text=not numeric and not p.is_path)
    return out


def parse_value(s, raw):
    """Text from the file -> float (or str for path/text settings). Raises ValueError."""
    raw = raw.strip()
    if s.path or s.text:
        v = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw
        return os.path.expanduser(v) if s.path else v
    low = raw.lower()
    if s.toggled or (s.minimum == 0 and s.maximum == 1 and s.integer):
        if low in ('on', 'true', 'yes'):
            return 1.0
        if low in ('off', 'false', 'no'):
            return 0.0
    for label, value in s.enum:
        if label.lower() == low:
            return value
    if s.gain and low in ('-inf', '-infdb'):
        return 0.0
    if low.endswith('db'):
        x = float(raw[:-2])
        v = 10 ** (x / 20) if s.gain else x
    else:
        v = float(raw)
    if math.isnan(v) or math.isinf(v):
        raise ValueError('not a number')
    if s.minimum is not None and v < s.minimum - 1e-9 or s.maximum is not None and v > s.maximum + 1e-9:
        raise ValueError(f'{raw} is outside {fmt_range(s)}')
    return v


def fmt_num(v):
    if v is None:
        return '?'
    if abs(v - round(v)) < 1e-9 and abs(v) < 1e7:
        return str(int(round(v)))
    a = abs(v)
    digits = 4 if a < 1 else 3 if a < 10 else 2 if a < 100 else 1
    return f'{v:.{digits}f}'.rstrip('0').rstrip('.')


def fmt_value(s, v):
    """Value -> how it should be written in the file."""
    if isinstance(v, str):
        return f'"{v}"' if (s.path or ' ' in v) else v
    if s.toggled:
        return 'on' if v >= 0.5 else 'off'
    for label, value in s.enum:
        if abs(value - v) < 1e-6 and re.fullmatch(r'[A-Za-z0-9_.+/-]+', label):
            return label
    if s.gain:
        return '-inf' if v <= 0 else f'{fmt_num(round(20 * math.log10(v), 1))}dB'
    return fmt_num(v)


def fmt_range(s):
    if s.path:
        return 'file path'
    if s.text:
        return 'text'
    if s.toggled:
        return 'on/off'
    if s.enum:
        return ' | '.join(label for label, _ in s.enum)
    if s.gain:
        lo = '-inf' if not s.minimum else fmt_num(20 * math.log10(s.minimum))
        hi = fmt_num(20 * math.log10(s.maximum)) if s.maximum else '?'
        return f'{lo} … {hi} dB'
    unit = f' {s.unit}' if s.unit else ''
    return f'{fmt_num(s.minimum)} … {fmt_num(s.maximum)}{unit}'


def nudge(s, v, direction, fine=False):
    """Step a value up/down sensibly for its kind of setting."""
    if s.toggled:
        return 0.0 if v >= 0.5 else 1.0
    if s.enum:
        values = [value for _, value in s.enum]
        i = min(range(len(values)), key=lambda k: abs(values[k] - v))
        return values[(i + direction) % len(values)]
    lo, hi = s.minimum, s.maximum
    if s.gain:
        db = -60.0 if v <= 0 else 20 * math.log10(v)
        db = round(db * 2) / 2 + direction * (0.1 if fine else 0.5)
        v = 10 ** (db / 20)
    elif s.integer:
        v = round(v) + direction
    elif s.log:
        span = (hi - lo) if lo is not None and hi is not None else 1
        if v <= (lo or 0) + span * 1e-4 and direction > 0:
            v = (lo or 0) + span * 0.001
        else:
            v = v * (1.01 if fine else 1.05) ** direction
    else:
        span = (hi - lo) if lo is not None and hi is not None else max(abs(v), 1)
        v = v + direction * span * (0.002 if fine else 0.01)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# ---------------------------------------------------------------- resolve

@dataclass
class Resolved:
    label: str
    uri: str | None
    plugin: object | None
    bypass: bool
    preset: str | None
    values: dict              # key -> float | str
    line: int


def resolve(rig, index, preset_dir):
    """Attach plugin metadata and turn every setting into a real value."""
    out = []
    for b in rig.blocks:
        uri, err = index.lookup(b.ref) if b.ref else (None, None)
        if err:
            rig.errors.append((b.line, err))
        plugin = index.plugin(uri) if uri else None
        settings = settings_of(plugin) if plugin else {}
        values, preset = {}, None
        for e in b.entries:
            if e.key == 'preset':
                name = e.raw.strip('"')
                preset = name if '://' in name else f'file://{preset_dir}/{name}.ttl'
                if preset.startswith('file://') and not os.path.exists(preset[7:]):
                    rig.errors.append((e.line, f"no saved preset '{name}' in {preset_dir}"))
                    preset = None
                continue
            if not plugin:
                continue
            s = settings.get(e.key)
            if not s:
                close = difflib.get_close_matches(e.key, settings.keys(), n=3)
                hint = f" (did you mean {', '.join(close)}?)" if close else ''
                rig.errors.append((e.line, f"{plugin.name} has no setting '{e.key}'{hint}"))
                continue
            try:
                v = parse_value(s, e.raw)
            except ValueError as ex:
                msg = str(ex) if 'outside' in str(ex) else f"'{e.raw}' isn't a valid value"
                rig.errors.append((e.line, f'{e.key}: {msg} ({fmt_range(s)})'))
                continue
            if s.path and not os.path.exists(v):
                rig.errors.append((e.line, f'{e.key}: file not found: {v}'))
                continue
            values[e.key] = v
        out.append(Resolved(b.label, uri, plugin, b.bypass, preset, values, b.line))
    return out


# ---------------------------------------------------------------- editing the text

def _split_comment(line):
    code = strip_comment(line)
    return code, line[len(code):]


def set_value(text, label, key, value):
    """Set `key = value` in block [label], adding the line if it isn't there."""
    lines = text.split('\n')
    rig = parse(text)
    b = next(x for x in rig.blocks if x.label == label)
    e = next((x for x in b.entries if x.key == key), None)
    if e:
        code, comment = _split_comment(lines[e.line])
        start = code.index('=') + 1
        while start < len(code) and code[start] == ' ':
            start += 1
        old = code[start:].rstrip()
        pad = code[start + len(old):]
        if comment and len(value) != len(old):          # keep comments lined up where possible
            pad = ' ' * max(1, len(pad) + len(old) - len(value))
        lines[e.line] = code[:start] + value + pad + comment
    else:
        last = max([b.line] + [x.line for x in b.entries])
        lines.insert(last + 1, f'  {key} = {value}')
    return '\n'.join(lines)


def set_top(text, key, value):
    """Set a top-level `key = value` (in, out, latency, volume), adding it if missing."""
    lines = text.split('\n')
    rig = parse(text)
    first = min([b.line for b in rig.blocks] + [len(lines)])
    for n in range(first):
        m = KEYVAL.match(strip_comment(lines[n]).strip())
        if m and m.group(1) == key:
            code, comment = _split_comment(lines[n])
            start = code.index('=') + 1
            while start < len(code) and code[start] == ' ':
                start += 1
            old = code[start:].rstrip()
            pad = code[start + len(old):]
            if comment and len(value) != len(old):
                pad = ' ' * max(1, len(pad) + len(old) - len(value))
            lines[n] = code[:start] + value + pad + comment
            return '\n'.join(lines)
    tops = [n for n in range(first) if KEYVAL.match(strip_comment(lines[n]).strip())]
    lines.insert(tops[-1] + 1 if tops else 0, f'{key} = {value}')
    return '\n'.join(lines)


def remove_line(text, line):
    lines = text.split('\n')
    del lines[line]
    return '\n'.join(lines)


def toggle_bypass(text, label):
    lines = text.split('\n')
    b = next(x for x in parse(text).blocks if x.label == label)
    code, comment = _split_comment(lines[b.line])
    indent = code[:len(code) - len(code.lstrip())]
    new = f'{indent}[{b.label}] {b.ref}' + ('' if b.bypass else '  off')
    if comment.strip():
        new = new.ljust(max(len(new) + 2, len(code.rstrip()) + (len(code) - len(code.rstrip())))) + comment.strip()
    lines[b.line] = new
    return '\n'.join(lines)


def add_lines(text, label, new_lines):
    lines = text.split('\n')
    b = next(x for x in parse(text).blocks if x.label == label)
    last = max([b.line] + [x.line for x in b.entries])
    lines[last + 1:last + 1] = new_lines
    return '\n'.join(lines)
