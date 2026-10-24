"""Draw the signal chain as boxes and arrows, wrapping into rows."""
from rich.text import Text

from .colors import FOAM, IRIS, LOVE, MUTED, TEXT

BOX_MIN, BOX_MAX = 10, 22


def render(nodes, selected, width, ins='IN', outs='OUT'):
    """nodes: [(label, subtitle, state)] with state 'on' | 'off' | 'error'."""
    if not nodes:
        return Text(f' {ins} ──────▶ {outs}   (no plugins yet)', style='dim')
    inner = min(BOX_MAX, max(BOX_MIN, *(max(len(l), len(s)) + 2 for l, s, _ in nodes)))
    box_w = inner + 2
    gap = 3                                   # "──▶"
    lead = 5                                  # "IN ─▶" / "  └─▶"
    per_row = max(1, (width - lead - 3) // (box_w + gap))
    rows = [nodes[i:i + per_row] for i in range(0, len(nodes), per_row)]

    out = Text()
    for r, row in enumerate(rows):
        lines = [Text() for _ in range(4)]
        first, last = r == 0, r == len(rows) - 1
        lines[0].append(' ' * lead if first else '  │  ')
        lines[1].append('IN ─▶' if first else '  └─▶', style='bold' if first else '')
        lines[2].append(' ' * lead)
        lines[3].append(' ' * lead)
        for c, (label, sub, state) in enumerate(row):
            style = {'on': 'green', 'off': 'grey50', 'error': 'red'}[state]
            if label == selected:
                style = f'bold {style}' if state != 'on' else 'bold cyan'
            name = label if len(label) <= inner - 2 else label[:inner - 3] + '…'
            if state == 'off':
                sub = '⏸ ' + sub
            elif state == 'error':
                sub = '✖ ' + sub
            sub = sub if len(sub) <= inner - 2 else sub[:inner - 3] + '…'
            edge = '━' if label == selected else '─'
            lines[0].append('┏' + edge * inner + '┓' if label == selected
                            else '┌' + edge * inner + '┐', style=style)
            v = '┃' if label == selected else '│'
            lines[1].append(v, style=style)
            lines[1].append(f' {name}'.ljust(inner), style=style)
            lines[1].append(v, style=style)
            lines[2].append(v, style=style)
            lines[2].append(f' {sub}'.ljust(inner), style=f'{style} dim' if state == 'on' else style)
            lines[2].append(v, style=style)
            lines[3].append('┗' + edge * inner + '┛' if label == selected
                            else '└' + edge * inner + '┘', style=style)
            if c < len(row) - 1:
                lines[0].append(' ' * gap)
                lines[1].append('──▶')
                lines[2].append(' ' * gap)
                lines[3].append(' ' * gap)
        if last:
            lines[1].append('──▶ ')
            lines[1].append(outs, style='bold')
        else:
            lines[1].append('──┐')
            lines[2].append('  │')
            lines[3].append('  │')
        for ln in lines:
            out.append_text(ln)
            out.append('\n')
        if not last:
            row_w = lead + len(row) * box_w + (len(row) - 1) * gap + 3
            out.append('  ┌' + '─' * (row_w - 3) + '┘\n')
    out.rstrip()
    return out


def _node(label, state, selected):
    mark = {'on': '', 'off': ' ⏸', 'error': ' ✖'}[state]
    style = {'on': FOAM, 'off': MUTED, 'error': f'bold {LOVE}'}[state]
    if label == selected:     # brackets and underline, never a background
        color = IRIS if state == 'on' else style.split()[-1]
        return [Text(f'[{label}{mark}]', style=f'bold underline {color}')]
    return [Text(f' {label}{mark} ', style=style)]


def _width(lines):
    return max(t.cell_len for t in lines)


def _pad(lines, width, fill_first=' '):
    """Pad every line to width; the first (the signal line) with fill_first."""
    out = []
    for i, t in enumerate(lines):
        t = t.copy()
        t.append((fill_first if i == 0 else ' ') * (width - t.cell_len), style=MUTED)
        out.append(t)
    return out


def _hcat(parts):
    """Join drawings side by side, signal lines aligned on top."""
    height = max(len(p) for p in parts)
    rows = [Text() for _ in range(height)]
    for p in parts:
        w = _width(p)
        p = _pad(p + [Text()] * (height - len(p)), w)
        for row, t in zip(rows, p):
            row.append_text(t)
    return rows


def _seq(items, states, selected):
    if not items:
        return [Text(' dry ', style=f'italic {MUTED}')]
    parts = []
    for item in items:
        if parts:
            parts.append([Text(' → ', style=MUTED)])
        parts.append(_item(item, states, selected))
    return _hcat(parts)


def _split(split, states, selected):
    paths = [_seq(p, states, selected) for p in split.paths]
    w = max(_width(p) for p in paths)
    out = []
    for i, p in enumerate(paths):
        first, last = i == 0, i == len(paths) - 1
        for j, t in enumerate(_pad(p, w, '─')):
            row = Text()
            if j == 0:
                row.append('┬─' if first else '└─' if last else '├─', style=MUTED)
            else:
                row.append('  ' if last else '│ ', style=MUTED)
            row.append_text(t)
            if j == 0:
                row.append('─┬' if first else '─┘' if last else '─┤', style=MUTED)
            else:
                row.append('  ' if last else ' │', style=MUTED)
            out.append(row)
    return out


def _item(item, states, selected):
    if isinstance(item, str):
        return _node(item, states.get(item, 'error'), selected)
    return _split(item, states, selected)


def _end(name, key, selected, extra=''):
    if key == selected:
        return [Text(f'[{name}{extra}]', style=f'bold underline {IRIS}')]
    return [Text(f' {name}{extra} ', style=f'bold {TEXT}')]


def render_chain(chain, states, selected, width, ends=('@in', '@out'), volume=''):
    """The signal chain, splits drawn as branches, wrapped to width at the top level.
    chain: labels and Splits; states: label -> 'on' | 'off' | 'error'. IN and OUT
    are selectable too (as ends[0] / ends[1]); OUT shows the master volume."""
    items = [_end('IN', ends[0], selected)]
    items += [_item(x, states, selected) for x in chain]
    items.append(_end('OUT', ends[1], selected, f' {volume}' if volume and volume != '0dB' else ''))
    arrow = [Text(' → ', style=MUTED)]
    rows, row = [], []
    for item in items:
        if row and _width(_hcat(row + [arrow, item])) > width:
            rows.append(row)
            row = [arrow]
        row += [arrow, item] if row and row != [arrow] else [item]
    rows.append(row)
    out = Text()
    for i, r in enumerate(rows):
        for j, line in enumerate(_hcat(r)):
            if i or j:
                out.append('\n')
            line.rstrip()
            out.append_text(line)
    return out
