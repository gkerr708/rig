"""The TUI: chain diagram, level meters, and the settings of the selected block. Edit the
.rig file in your own editor; every save is applied. Keys here change values and write them
back to the file. ? lists the keys."""
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static

from . import diagram
from .colors import BLACK_CSS, FOAM, GOLD, IRIS, LOVE, MUTED, ROSE, SUBTLE, TEXT, THEME
from .host import CarlaHost, Engine, EngineError, keyed, list_ports, ours
from .looper import RECORDINGS, BackingScreen, LooperHost, LooperScreen, TrainerScreen, strip
from .lv2 import Index
from .meters import AMP_ZONES, LEVEL_ZONES, Meter
from .picker import LEVEL_KEYS, CapturePicker, capture_files, capture_setting, matched_level
from .rigfile import (LATENCIES, add_lines, fmt_range, fmt_value, fmt_volume, nudge, parse,
                      parse_value, parse_volume, remove_line, resolve, set_top, set_value,
                      settings_of, toggle_bypass)
from .screens import HelpScreen, TunerScreen

ROOT = Path(__file__).resolve().parent.parent
PRESET_DIR = ROOT / 'presets.lv2'
LOG = Path('~/.cache/rig/engine.log').expanduser()
IN, OUT = '@in', '@out'          # the ends of the chain, selectable like blocks
AMP_WORDS = re.compile(r'amp|tone3000|neural|aida|chump|jcm800|preamp|studiopre|alembic|metal_?head', re.I)
CHORD = 0.6                      # seconds to finish a two-key command (gg, dd)


class RigApp(App):
    TITLE = 'rig'
    ENABLE_COMMAND_PALETTE = False
    CSS = BLACK_CSS + """
    #diagram { height: auto; border: round $border; padding: 0 1; }
    #meters { height: auto; padding: 0 1; }
    #settings-box { height: 1fr; border: round $border; padding: 0 0 0 1; }
    DataTable > .datatable--header { color: $primary; }
    #info { height: auto; }
    #table { height: 1fr; }
    #prompt { height: 1; border: none; padding: 0 1; display: none; }
    #status { height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding('question_mark', 'help', 'help', priority=True),
        Binding('left,h', 'select_block(-1)', show=False, priority=True),
        Binding('right,l', 'select_block(1)', show=False, priority=True),
        Binding('j', 'move_row(1)', show=False),
        Binding('k', 'move_row(-1)', show=False),
        Binding('ctrl+d', 'half_page(1)', show=False, priority=True),
        Binding('ctrl+u', 'half_page(-1)', show=False, priority=True),
        Binding('plus,equals_sign', 'nudge(1)', show=False, priority=True),
        Binding('minus,underscore', 'nudge(-1)', show=False, priority=True),
        Binding('right_square_bracket', 'nudge(1, True)', show=False, priority=True),
        Binding('left_square_bracket', 'nudge(-1, True)', show=False, priority=True),
        Binding('i', 'edit_value', show=False, priority=True),
        Binding('greater_than_sign', 'switch_rig(1)', show=False, priority=True),
        Binding('less_than_sign', 'switch_rig(-1)', show=False, priority=True),
        Binding('b', 'bypass', 'bypass', priority=True),
        Binding('a', 'template', show=False, priority=True),
        Binding('o', 'window', 'window', priority=True),
        Binding('m', 'capture', 'capture', priority=True),
        Binding('t', 'tuner', 'tuner', priority=True),
        Binding('p', 'looper', 'looper', priority=True),
        Binding('space', 'loop_toggle', show=False, priority=True),
        Binding('s', 'save_state', show=False, priority=True),
        Binding('u', 'undo', 'undo', priority=True),
        Binding('ctrl+r', 'redo', show=False, priority=True),
        Binding('e', 'edit_file', show=False, priority=True),
        Binding('r', 'restart', show=False, priority=True),
        Binding('escape', 'cancel_prompt', show=False, priority=True),
        Binding('q,ctrl+q', 'quit', 'quit', priority=True),
    ]

    def __init__(self, path):
        super().__init__()
        self.path = Path(path)
        self.disk_text = self._read() or ''
        self.disk_mtime = self._mtime()
        self._pending_mtime = None
        self.index = None
        self.host = None
        self.engine = None
        self.rig = parse(self.disk_text)
        self.resolved = []
        self.selected = self.rig.blocks[0].label if self.rig.blocks else OUT
        self.sel_key = None
        self.message = Text('starting…', style=GOLD)
        self.stats = ''
        self.hw_ports = ([], [])
        self.windows = {}          # label -> plugin state when its window was opened
        self._dirty = False        # a plugin window changed values; refresh soon
        self.undo, self.redo = [], []
        self.chord = (None, 0.0)   # first key of gg / dd, and when it was pressed
        self.search = ''
        self.prompt_mode = None    # 'search' | 'value' while the prompt is open
        self._prompt_label = ''
        self.meters = {'in': Meter('in ', LEVEL_ZONES), 'amp': Meter('amp', AMP_ZONES),
                       'out': Meter('out', LEVEL_ZONES)}
        self.looper = None         # started the first time p is pressed, then kept until quit
        self.loop_status = {}

    def _read(self):
        try:
            return self.path.read_text()
        except OSError:
            return None

    def _mtime(self):
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static(id='diagram')
        yield Static(id='meters')
        with Vertical(id='settings-box'):
            yield Static(id='info')
            yield DataTable(id='table', cursor_type='row', zebra_stripes=False)
        yield Input(id='prompt')
        yield Static(id='status')
        yield Footer()

    @property
    def table(self) -> DataTable:
        return self.query_one('#table', DataTable)

    @property
    def prompt(self) -> Input:
        return self.query_one('#prompt', Input)

    def on_mount(self):
        self.register_theme(THEME)
        self.theme = THEME.name
        self.query_one('#diagram').border_title = escape(str(self.path).replace(str(Path.home()), '~'))
        self.table.add_column('', key='mark', width=1)
        for name in ('setting', 'value', 'range', 'name'):
            self.table.add_column(name, key=name)
        self.table.focus()
        self.refresh_views()
        self.run_worker(self._boot, thread=True, exit_on_error=False)
        self.set_interval(0.2, self._check_disk)
        self.set_interval(2.0, self._poll_stats)
        self.set_interval(0.15, self._refresh_if_dirty)
        self.set_interval(0.1, self._poll_meters)
        self.set_interval(0.1, self._poll_loop)

    def _boot(self):
        self.call_from_thread(self._say, 'reading plugin info…', GOLD)
        self.index = Index()
        self.hw_ports = list_ports()
        self.call_from_thread(self.reparse)
        self._start_audio()

    def _start_audio(self, done='loaded'):
        """Start (or restart) the audio engine at the rig's latency and load everything. Runs in a thread."""
        if self.host:
            self.host.stop()
        self.host = self.engine = None
        self.call_from_thread(self._say, f'starting audio ({self.rig.latency} samples)…', GOLD)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            host = CarlaHost(LOG, PRESET_DIR, self.rig.latency)
            host.on_event = lambda msg: self.call_from_thread(self._engine_event, msg)
            host.start()
        except Exception as e:
            self.call_from_thread(self._say, f'audio engine failed: {e}', f'bold {LOVE}')
            return
        self.windows = {}
        self.host, self.engine = host, Engine(host, PRESET_DIR)
        self.engine.looper = self.looper.ports if self.looper else None
        ms = self.rig.latency / 48
        if subprocess.run(['pgrep', '-f', 'share/carla/carla'], capture_output=True).returncode == 0:
            done += '  (Carla is also running: you may hear both)'
        self.call_from_thread(self.apply, f'{done}, buffer {self.rig.latency} samples ≈ {ms:.1f} ms')

    def on_unmount(self):
        if self.host:
            self.host.stop()
        if self.looper:
            self.looper.stop()

    # ------------------------------------------------------------ parse & apply

    def reparse(self):
        self.rig = parse(self.disk_text)
        if self.index:
            self.resolved = resolve(self.rig, self.index, PRESET_DIR)
        if self.selected not in self._order():
            labels = [b.label for b in self.rig.blocks]
            self.selected = labels[0] if labels else OUT
        self.refresh_views()

    def apply(self, done_msg='applied'):
        if not self.engine:
            return
        if self.host.latency != self.rig.latency:
            self.run_worker(lambda: self._start_audio('buffer size changed'), thread=True,
                            group='apply', exit_on_error=False)
            return
        rig, resolved = self.rig, self.resolved

        def work():
            engine = self.engine
            try:
                problems, _ = engine.apply(rig, resolved, True)
            except Exception as e:
                problems = [f'engine error: {e}']
            if engine.stale_presets and any('unknown preset' in p for p in problems):
                # the engine only lists presets that existed when it started
                self._start_audio('reloaded presets')
                return
            self.hw_ports = list_ports()
            self.call_from_thread(self._applied, problems, done_msg)
        self.run_worker(work, thread=True, group='apply', exit_on_error=False)

    def _applied(self, problems, done_msg):
        if problems:
            self._say('✖ ' + problems[0] + (f'  (+{len(problems) - 1} more)' if len(problems) > 1 else ''),
                      f'bold {LOVE}')
            for p in problems[1:6]:
                self.notify(p, severity='error', timeout=8)
        elif done_msg:
            self._say('✔ ' + done_msg, FOAM)
        self.refresh_views()

    def _say(self, msg, style=''):
        self.message = Text(msg, style=style)
        self.refresh_status()

    def _check_disk(self):
        mtime = self._mtime()
        if mtime is None or mtime == self.disk_mtime:
            return
        # editors may truncate then write: only read once the file has stopped changing
        if mtime != self._pending_mtime:
            self._pending_mtime = mtime
            return
        self.disk_mtime = mtime
        text = self._read()
        if text is None or text == self.disk_text:
            return
        self.disk_text = text
        self.undo.clear()           # undo here only covers changes made here
        self.redo.clear()
        self.reparse()
        self.apply(f'applied {self.path.name}')

    def _write(self, text, msg='', record=True):
        """Change the file from here: write it, then apply right away."""
        if record and text != self.disk_text:
            self.undo.append(self.disk_text)
            self.redo.clear()
        self.disk_text = text
        tmp = self.path.with_name(f'.{self.path.name}.tmp')
        tmp.write_text(text)
        os.replace(tmp, self.path)
        self.disk_mtime = self._mtime()
        self.reparse()
        self.apply(msg)

    def _poll_stats(self):
        if not self.host:
            return

        def work():
            try:
                load, xruns = self.host.call('stats', timeout=5)
                self.stats = (f'dsp {load:.0f}%', f'{xruns} xrun' + ('' if xruns == 1 else 's'), xruns)
            except EngineError:
                self.stats = ('audio engine not responding (r restarts)', '', 0)
            self.call_from_thread(self.refresh_status)
        self.run_worker(work, thread=True, group='stats', exclusive=True, exit_on_error=False)

    def _poll_meters(self):
        engine = self.engine
        if not engine:
            for m in self.meters.values():
                m.active = False
            self.refresh_meters()
            return
        amp = self._amp_label()

        def work():
            try:
                peaks = engine.meters()
            except EngineError:
                return
            levels = {'in': [peaks[l][0] for l in engine.first if l in peaks],
                      'amp': [peaks[amp][0]] if amp in peaks else [],
                      'out': [peaks[l][1] for l in engine.last if l in peaks]}
            self.call_from_thread(self._update_meters, levels)
        self.run_worker(work, thread=True, group='meters', exclusive=True, exit_on_error=False)

    def _update_meters(self, levels):
        for name, values in levels.items():
            if values:
                self.meters[name].update(max(values))
            else:
                self.meters[name].active = False
        self.refresh_meters()

    def _amp_label(self):
        """The block the amp meter watches: one labelled amp, else the first amp-like plugin."""
        live = [b for b in self.rig.blocks if not b.bypass]
        for b in live:
            if b.label.lower().startswith('amp'):
                return b.label
        for b in live:
            r = self._resolved(b.label)
            if r and r.plugin and (AMP_WORDS.search(r.uri) or AMP_WORDS.search(r.plugin.name)):
                return b.label
        return None

    # ------------------------------------------------------------ keys

    def check_action(self, action, parameters):
        # overlays handle their own keys; while typing, keys are text
        if isinstance(self.screen, ModalScreen):
            return False
        if self.prompt.has_focus:
            return action == 'cancel_prompt'
        return action != 'cancel_prompt'

    def on_key(self, event):
        """Vim keys that are characters or two-key chords: gg G 0 $ / n N dd."""
        if isinstance(self.screen, ModalScreen) or self.prompt.has_focus:
            return
        ch = event.character
        first, at = self.chord
        chord = first if time.monotonic() - at < CHORD else None
        self.chord = (None, 0.0)
        if ch in ('g', 'd') and chord == ch:
            self.action_first_row() if ch == 'g' else self.action_delete_setting()
        elif ch in ('g', 'd'):
            self.chord = (ch, time.monotonic())
        elif ch == 'G':
            self.action_last_row()
        elif ch == '0':
            self._select(IN)
        elif ch == '$':
            self._select(OUT)
        elif ch == 'B':
            self.action_backing()
        elif ch == 'M':
            self.action_metronome()
        elif ch == 'R':
            self.action_record_session()
        elif ch == 'T':
            self._start_looper()
            self.push_screen(TrainerScreen(lambda: self.looper))
        elif ch == '/':
            self._open_prompt('search', '/', self.search)
        elif ch in ('n', 'N'):
            self._find(self.search, 1 if ch == 'n' else -1)
        else:
            return
        event.stop()

    def action_help(self):
        self.push_screen(HelpScreen())

    # ------------------------------------------------------------ moving around

    def _order(self):
        return [IN] + [b.label for b in self.rig.blocks] + [OUT]

    def _block(self):
        return self._block_named(self.selected)

    def _block_named(self, label):
        return next((b for b in self.rig.blocks if b.label == label), None)

    def _resolved(self, label):
        return next((r for r in self.resolved if r.label == label), None)

    def _select(self, label):
        self.selected = label
        self.sel_key = None
        self.refresh_views()
        self.table.move_cursor(row=0)

    def action_select_block(self, step):
        order = self._order()
        i = order.index(self.selected) if self.selected in order else 0
        self._select(order[(i + step) % len(order)])

    def action_move_row(self, step):
        if step > 0:
            self.table.action_cursor_down()
        else:
            self.table.action_cursor_up()

    def action_first_row(self):
        self.table.move_cursor(row=0)

    def action_last_row(self):
        self.table.move_cursor(row=max(0, self.table.row_count - 1))

    def action_half_page(self, step):
        half = max(1, self.table.size.height // 2)
        row = min(max(0, self.table.cursor_row + step * half), max(0, self.table.row_count - 1))
        self.table.move_cursor(row=row)

    @on(DataTable.RowHighlighted, '#table')
    def _row(self, event):
        key = event.row_key.value if event.row_key else None
        if key and not key.startswith('_'):
            self.sel_key = key
        self._mark_cursor()

    def _mark_cursor(self):
        table = self.table
        for i, row_key in enumerate(table.rows):
            mark = Text('›', style=f'bold {IRIS}') if i == table.cursor_row else ''
            table.update_cell(row_key, 'mark', mark)

    @on(DataTable.RowSelected, '#table')
    def _row_selected(self, event):
        self.action_edit_value()

    # ------------------------------------------------------------ the prompt (/ and typed values)

    def _open_prompt(self, mode, label, value=''):
        self.prompt_mode = mode
        p = self.prompt
        p.display = True
        p.placeholder = ''
        p.value = value
        p.border_title = None
        self._prompt_label = label
        self._say(label, f'bold {IRIS}')
        p.focus()

    def action_cancel_prompt(self):
        self._close_prompt()
        self._say('', '')

    def _close_prompt(self):
        self.prompt_mode = None
        self.prompt.display = False
        self.table.focus()

    @on(Input.Submitted, '#prompt')
    def _prompt_done(self, event):
        mode, text = self.prompt_mode, event.value
        self._close_prompt()
        self._say('', '')
        if mode == 'search':
            self.search = text
            self._find(text, 1, from_current=True)
        elif mode == 'value':
            self._set_typed(text)

    @on(Input.Changed, '#prompt')
    def _prompt_typing(self, event):
        if self.prompt_mode == 'search' and event.value:
            self._find(event.value, 1, from_current=True, quiet=True)

    def _find(self, text, step, from_current=False, quiet=False):
        if not text:
            return
        table, low = self.table, text.lower()
        keys = [k.value for k in table.rows]
        hits = [i for i, k in enumerate(keys) if not k.startswith('_') and (
            low in k.lower() or low in str(table.get_cell_at((i, 4))).lower())]
        if not hits:
            if not quiet:
                self._say(f'no setting matches "{text}"', LOVE)
            return
        here = table.cursor_row
        if from_current:
            row = next((i for i in hits if i >= here), hits[0])
        elif step > 0:
            row = next((i for i in hits if i > here), hits[0])
        else:
            row = next((i for i in reversed(hits) if i < here), hits[-1])
        table.move_cursor(row=row)
        if not quiet:
            self._say(f'/{text}  ({hits.index(row) + 1} of {len(hits)})  n / N for more', MUTED)

    def action_edit_value(self):
        """Type a value for the selected setting."""
        key = self.sel_key
        if not key:
            return
        if self.selected in (IN, OUT):
            current = {'in': ', '.join(p for p, _ in self.rig.ins),
                       'out': ', '.join(p for p, _ in self.rig.outs),
                       'latency': str(self.rig.latency),
                       'volume': fmt_volume(self.rig.volume)}.get(key, '')
            self._open_prompt('value', f'{key} =', current)
            return
        r = self._resolved(self.selected)
        s = settings_of(r.plugin).get(key) if r and r.plugin else None
        if not s:
            return
        b = self._block()
        entry = next((e for e in b.entries if e.key == key), None) if b else None
        live = self.engine.live.get(self.selected) if self.engine else None
        if entry:
            current = entry.raw
        else:
            v = live.base.get(key) if live and key in live.base else s.default
            current = '' if v is None else fmt_value(s, v)
        self._open_prompt('value', f'[{self.selected}] {key} =  ({fmt_range(s)})', current)

    def _set_typed(self, raw):
        key, raw = self.sel_key, raw.strip()
        if not key or not raw:
            return
        if self.selected in (IN, OUT):
            try:
                if key == 'latency' and int(raw) not in LATENCIES:
                    raise ValueError
                if key == 'volume':
                    parse_volume(raw)
            except ValueError as e:
                self._say(f'✖ {key}: {e or "choose one of " + ", ".join(map(str, LATENCIES))}', LOVE)
                return
            self._write(set_top(self.disk_text, key, raw), f'{key} = {raw}')
            return
        r = self._resolved(self.selected)
        s = settings_of(r.plugin).get(key) if r and r.plugin else None
        if not s:
            return
        try:
            parse_value(s, raw)
        except ValueError as e:
            self._say(f'✖ {key}: {e} ({fmt_range(s)})', LOVE)
            return
        if s.path and not raw.startswith('"'):
            raw = f'"{raw}"'
        self._write(set_value(self.disk_text, self.selected, key, raw), f'[{self.selected}] {key} = {raw}')

    # ------------------------------------------------------------ changing things

    def action_nudge(self, direction, fine=False):
        if self.selected in (IN, OUT):
            return self._nudge_top(direction, fine)
        r = self._resolved(self.selected)
        if not r or not r.plugin or not self.sel_key:
            return
        s = settings_of(r.plugin).get(self.sel_key)
        if not s or s.path or s.text:
            self.notify('That setting takes a file path or text: press enter to type it.', timeout=3)
            return
        live = self.engine.live.get(self.selected) if self.engine else None
        v = r.values.get(s.key)
        if v is None:
            v = live.base.get(s.key) if live and s.key in live.base else s.default
        if v is None:
            v = s.minimum or 0.0
        new = fmt_value(s, nudge(s, v, direction, fine))
        self._write(set_value(self.disk_text, self.selected, s.key, new), '')
        self._say(f'{self.selected}.{s.key} = {new}', FOAM)

    def _nudge_top(self, direction, fine):
        key = self.sel_key
        if key == 'volume':
            db = self.rig.volume
            db = -60.0 if db is None else db
            db = round(db + direction * (0.1 if fine else 0.5), 1)
            new = fmt_volume(min(db, 2.0))
        elif key == 'latency':
            i = LATENCIES.index(self.rig.latency) + direction
            new = str(LATENCIES[min(max(i, 0), len(LATENCIES) - 1)])
        else:
            return
        self._write(set_top(self.disk_text, key, new), '')
        self._say(f'{key} = {new}', FOAM)

    def action_delete_setting(self):
        """dd: take the setting's line out of the file (it goes back to preset / default)."""
        key = self.sel_key
        if self.selected in (IN, OUT):
            if key != 'volume':
                return
            lines = self.disk_text.split('\n')
            first = min([b.line for b in self.rig.blocks] + [len(lines)])
            n = next((i for i in range(first) if re.match(r'\s*volume\s*=', lines[i])), None)
            if n is not None:
                self._write(remove_line(self.disk_text, n), 'volume back to 0dB')
            return
        b = self._block()
        entry = next((e for e in b.entries if e.key == key), None) if b else None
        if entry:
            self._write(remove_line(self.disk_text, entry.line), f'[{b.label}] {key} removed from the file')

    def action_undo(self):
        if not self.undo:
            self._say('nothing to undo', MUTED)
            return
        self.redo.append(self.disk_text)
        self._write(self.undo.pop(), f'undone ({len(self.undo)} more)', record=False)

    def action_redo(self):
        if not self.redo:
            self._say('nothing to redo', MUTED)
            return
        self.undo.append(self.disk_text)
        self._write(self.redo.pop(), 'redone', record=False)

    def action_edit_file(self):
        editor = os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'nvim'
        with self.suspend():
            subprocess.run([*editor.split(), str(self.path)])
        self._check_disk()

    def action_bypass(self):
        b = self._block()
        if b:
            self._write(toggle_bypass(self.disk_text, b.label),
                        f'{b.label} ' + ('on' if b.bypass else 'bypassed'))

    def action_template(self):
        b = self._block()
        r = b and self._resolved(b.label)
        if not r or not r.plugin:
            return
        have = {e.key for e in b.entries}
        live = self.engine.live.get(b.label) if self.engine else None
        lines = []
        for key, s in settings_of(r.plugin).items():
            if key in have or s.text:
                continue
            rng = fmt_range(s)
            note = f'# {s.name} ({rng if len(rng) <= 40 else rng[:39] + "…"})'
            if s.path:
                lines.append(f'  # {key} = "~/path/to/file"   {note}')
                continue
            v = live.base.get(key) if live and key in live.base else s.default
            if v is not None:
                lines.append(f'  {key} = {fmt_value(s, v)}   {note}')
        if lines:
            self._write(add_lines(self.disk_text, b.label, lines),
                        f'added {len(lines)} settings to [{b.label}]')

    def action_save_state(self):
        b = self._block()
        r = b and self._resolved(b.label)
        if not r or not r.plugin or not self.engine or b.label not in self.engine.live:
            return
        name = f'{self.path.stem}-{b.label}'

        def work():
            try:
                self.engine.save_preset(b.label, name, r.plugin)
            except Exception as e:
                self.call_from_thread(self.notify, f'Saving state failed: {e}', severity='error')
                return
            self.call_from_thread(self._write, set_value(self.disk_text, b.label, 'preset', name),
                                  f'saved [{b.label}] state as preset "{name}"')
        self.run_worker(work, thread=True, group='apply', exit_on_error=False)

    def action_window(self):
        """Open the selected plugin's own window. What you change there is saved
        back when the window closes."""
        b = self._block()
        if not b or not self.engine or b.label not in self.engine.live:
            return
        name = self.engine.live[b.label].name

        def work():
            try:
                before = self.host.call('state', name)
                self.host.call('show_ui', name, True)
            except EngineError as e:
                self.call_from_thread(self.notify, f'[{b.label}]: {e}', severity='warning', timeout=4)
                return
            self.windows[b.label] = before
            self.call_from_thread(self._say, f'[{b.label}] window open: changes are saved when you close it',
                                  IRIS)
        self.run_worker(work, thread=True, group='window', exit_on_error=False)

    def action_tuner(self):
        engine = self.engine
        if not engine:
            return

        def start():
            try:
                engine.tuner_on()
            except EngineError as e:
                self.call_from_thread(self.notify, f'tuner: {e}', severity='error')
        self.run_worker(start, thread=True, group='apply', exit_on_error=False)

        def done(_):
            self.run_worker(engine.tuner_off, thread=True, group='apply', exit_on_error=False)
        self.push_screen(TunerScreen(engine.tuner_freq, engine.set_mute), done)

    def action_looper(self):
        self._start_looper()
        self.push_screen(LooperScreen(lambda: self.looper))

    def action_backing(self):
        self._start_looper()
        self.push_screen(BackingScreen(lambda: self.looper))

    def action_metronome(self):
        """M: metronome on / off, at the looper's tempo (p changes it)."""
        def toggle(looper):
            s = looper.settings
            on = looper.call('status').get('metro')
            looper.call('metro', None if on else s['bpm'], s['beats'])
            if not on:
                self.call_from_thread(self._say, f'metronome {s["bpm"]} bpm: p changes the tempo', IRIS)
        if self.looper and self.looper.alive():
            looper = self.looper
            self.run_worker(lambda: toggle(looper), thread=True, group='looper', exit_on_error=False)
        else:
            self._start_looper(then=toggle)

    def action_record_session(self):
        """R: record everything until R again: what you hear, and the guitar alone."""
        def toggle(looper):
            try:
                if looper.call('status').get('recording') is None:
                    RECORDINGS.mkdir(parents=True, exist_ok=True)
                    base = RECORDINGS / f'{datetime.now():%Y-%m-%d_%H%M%S} {self.path.stem}'
                    looper.call('rec_start', str(base))
                    self.call_from_thread(self._say, '● recording: R stops', f'bold {LOVE}')
                else:
                    paths, secs = looper.call('rec_stop', timeout=60)
                    shown = paths[0].replace(str(Path.home()), '~', 1).removesuffix(' - mix.wav')
                    self.call_from_thread(self._say, f'✔ saved {secs // 60:.0f}:{secs % 60:02.0f}: '
                                          f'{shown} - mix.wav / - guitar.wav', FOAM)
            except EngineError as e:
                self.call_from_thread(self.notify, f'recording: {e}', severity='error')
        if self.looper and self.looper.alive():
            looper = self.looper
            self.run_worker(lambda: toggle(looper), thread=True, group='looper', exit_on_error=False)
        else:
            self._start_looper(then=toggle)

    def _start_looper(self, then=None):
        """The looper process runs loops, the metronome and backing tracks; it starts when first needed."""
        if not self.looper or not self.looper.alive():
            self.looper = None
            looper = LooperHost()

            def start():
                try:
                    looper.start()
                except Exception as e:
                    self.call_from_thread(self.notify, str(e), severity='error')
                    return
                self.looper = looper
                if self.engine:
                    self.engine.looper = looper.ports
                self.call_from_thread(self.apply, 'looper connected')
                if then:
                    then(looper)
            self.run_worker(start, thread=True, group='apply', exit_on_error=False)

    def action_loop_toggle(self):
        """space: stop or play the loop without opening the looper; with no loop, the backing track."""
        st, looper = self.loop_status, self.looper
        state = st.get('state', 'empty')
        if state != 'empty':
            call = ('play',) if state == 'stopped' else ('stop',)
        elif st.get('track'):
            call = ('track_play', not st['track_on'])
        else:
            self._say('p opens the looper, B the backing tracks', MUTED)
            return
        self.run_worker(lambda: looper.call(*call), thread=True, group='looper', exit_on_error=False)

    def _poll_loop(self):
        looper = self.looper
        if not looper or isinstance(self.screen, (LooperScreen, BackingScreen, TrainerScreen)):
            return

        def work():
            try:
                st = looper.call('status', timeout=2)
            except EngineError:
                st = {}
            if st != self.loop_status:
                self.loop_status = st
                self.call_from_thread(self.refresh_meters)
        self.run_worker(work, thread=True, group='loop-poll', exclusive=True, exit_on_error=False)

    def _engine_event(self, msg):
        label = msg.get('plugin')
        live = self.engine.live.get(label) if self.engine else None
        r = self._resolved(label)
        if not live or not r or not r.plugin:
            return
        if msg['event'] == 'param':
            b = self._block_named(label)
            in_file = {e.key for e in b.entries} if b else set()
            for key, v in keyed(r.plugin, {msg['symbol']: msg['value']}).items():
                live.applied[key] = v
                if key not in in_file:
                    live.base[key] = v
            self._dirty = True
        elif msg['event'] == 'ui_closed':
            before = self.windows.pop(label, None)
            if msg.get('crashed'):
                self.notify(f'[{label}] window crashed', severity='error')
            if before is not None:
                self.run_worker(lambda: self._save_window(label, r.plugin, before), thread=True,
                                group='apply', exit_on_error=False)

    def _save_window(self, label, plugin, before):
        """A plugin window closed: if anything changed, keep it. Settings that are in the
        file get their new values; everything else goes into the plugin's preset."""
        try:
            if self.host.call('state', self.engine.live[label].name) == before:
                return
            name = f'{self.path.stem}-{label}'
            values = self.engine.save_preset(label, name, plugin)
        except (EngineError, KeyError) as e:
            self.call_from_thread(self.notify, f'Saving [{label}] failed: {e}', severity='error')
            return

        def write():
            text = set_value(self.disk_text, label, 'preset', name)
            b = next((x for x in parse(text).blocks if x.label == label), None)
            settings = settings_of(plugin)
            for e in b.entries if b else []:
                s = settings.get(e.key)
                if s and not (s.path or s.text) and e.key in values:
                    new = fmt_value(s, values[e.key])
                    if new != (fmt_value(s, parse_value(s, e.raw)) if _parses(s, e.raw) else None):
                        text = set_value(text, label, e.key, new)
            self._write(text, f'saved [{label}] window changes (preset "{name}")')
        self.call_from_thread(write)

    def _refresh_if_dirty(self):
        if self._dirty:
            self._dirty = False
            self.refresh_settings()

    def action_capture(self):
        """Pick the selected block's capture (or IR) by ear."""
        b = self._block()
        r = b and self._resolved(b.label)
        live = self.engine.live.get(b.label) if b and self.engine else None
        if not r or not r.plugin or not live:
            return
        settings = settings_of(r.plugin)
        s = capture_setting(settings, self.sel_key)
        if not s:
            self.notify(f'{r.plugin.name} does not load capture files.', timeout=3)
            return
        folder, files = capture_files(r.plugin, s)
        level = next((settings[k] for k in LEVEL_KEYS if k in settings), None)
        before = {s.key: live.applied.get(s.key)}
        if level:
            before[level.key] = live.applied.get(level.key, level.default)

        def level_for(path):
            return level and matched_level(level, before[level.key], before[s.key], str(path))

        def values_for(path):
            values = {s.key: path}
            if (v := level_for(path)) is not None:
                values[level.key] = v
            return values

        def audition(values):
            def work():
                problems = self.engine.audition(b.label, r.plugin, values)
                msg = problems[0] if problems else f'[{b.label}] {Path(values[s.key]).name}'
                self.call_from_thread(self._say, msg, f'bold {LOVE}' if problems else IRIS)
            # only the newest highlight matters when scrolling fast
            self.run_worker(work, thread=True, group='audition', exclusive=True, exit_on_error=False)

        def done(path):
            if path is None:
                if before[s.key] is not None:
                    audition({k: v for k, v in before.items() if v is not None})
                self._say(f'[{b.label}] kept {Path(before[s.key] or "nothing").name}', MUTED)
                return
            text = self.disk_text
            for key, v in values_for(path).items():
                shown = v.replace(str(Path.home()), '~', 1) if isinstance(v, str) else v
                text = set_value(text, b.label, key, fmt_value(settings[key], shown))
            self._write(text, f'[{b.label}] {s.key} = {Path(path).name}')

        title = f'[{b.label}] {s.key}: {r.plugin.name}'
        self.push_screen(CapturePicker(title, folder, files, before[s.key],
                                       lambda path: audition(values_for(path)),
                                       level_for if level else None), done)

    def action_switch_rig(self, step):
        rigs = sorted(self.path.parent.glob('*.rig'))
        if not rigs:
            return
        i = rigs.index(self.path) if self.path in rigs else -1
        self.path = rigs[(i + step) % len(rigs)]
        self.disk_text = self._read() or ''
        self.disk_mtime = self._pending_mtime = self._mtime()
        self.undo.clear()
        self.redo.clear()
        self.query_one('#diagram').border_title = escape(str(self.path).replace(str(Path.home()), '~'))
        self.selected, self.sel_key = None, None
        self.reparse()
        self.apply(f'switched to {self.path.stem}')

    def action_restart(self):
        self.run_worker(lambda: self._start_audio('restarted'), thread=True,
                        group='apply', exit_on_error=False)

    # ------------------------------------------------------------ views

    def on_resize(self):
        self.refresh_diagram()
        self.refresh_meters()

    def refresh_views(self):
        self.refresh_diagram()
        self.refresh_settings()
        self.refresh_status()

    def refresh_diagram(self):
        widget = self.query_one('#diagram', Static)
        width = max(30, widget.size.width - 2) if widget.size.width else 100
        by_label = {r.label: r for r in self.resolved}
        states = {}
        for b in self.rig.blocks:
            r = by_label.get(b.label)
            failed = (not r or not r.uri) and self.index is not None
            states[b.label] = 'error' if failed else 'off' if b.bypass else 'on'
        widget.update(diagram.render_chain(self.rig.chain, states, self.selected, width,
                                           ends=(IN, OUT), volume=fmt_volume(self.rig.volume)))

    def refresh_meters(self):
        widget = self.query_one('#meters', Static)
        width = widget.size.width - 2 if widget.size.width else 100
        # each meter is label + bar + about 18 cells of reading; three to a line when they fit
        bar = max(8, min(30, (width - 3 * 26) // 3))
        per_line = 3 if 3 * (bar + 26) <= width else 1
        out = Text()
        for i, m in enumerate(self.meters.values()):
            if i:
                out.append('\n' if i % per_line == 0 else '')
            t = m.render(bar)
            if per_line == 3 and i < 2:
                t.append(' ' * max(1, bar + 26 - t.cell_len))
            out.append_text(t)
        if loop := strip(self.loop_status):
            out.append('\n')
            out.append_text(loop)
        widget.update(out)

    def refresh_settings(self):
        info = self.query_one('#info', Static)
        box = self.query_one('#settings-box')
        table = self.table
        rows = []
        if self.selected in (IN, OUT):
            box.border_title = 'IN' if self.selected == IN else 'OUT'
            info.update(self._io_help(self.selected))
            rows = self._io_rows(self.selected)
        else:
            rows = self._block_rows(info, box)
        keys = [k for k, _ in rows]
        cursor = table.cursor_row
        table.clear()
        for k, cells in rows:
            table.add_row('', *cells, key=k)
        if self.sel_key in keys:
            table.move_cursor(row=keys.index(self.sel_key), animate=False)
        elif rows:
            table.move_cursor(row=min(cursor, len(rows) - 1), animate=False)
        self._mark_cursor()

    def _block_rows(self, info, box):
        b = self._block()
        if not b:
            box.border_title = ''
            info.update(Text('No blocks yet: add one to the file, like  [amp] tone3000', style=MUTED))
            return []
        box.border_title = escape(f'[{b.label}]')
        r = self._resolved(b.label)
        out = Text()
        live = None
        if r and r.plugin:
            live = self.engine.live.get(b.label) if self.engine else None
            out.append(r.plugin.name, style=f'bold {TEXT}')
            if r.preset:
                out.append(f'  preset {r.preset.rsplit("/", 1)[-1].removesuffix(".ttl")}', style=IRIS)
            if b.bypass:
                out.append('  bypassed', style=GOLD)
        elif self.index is None:
            out.append('loading plugin info…', style=GOLD)
        for l, m in self.rig.errors:
            if b.line <= l <= b.end:
                out.append(f'\nline {l + 1}: {m}', style=f'bold {LOVE}')
        info.update(out)
        rows = []
        if r and r.plugin:
            settings = settings_of(r.plugin)
            shown = []
            for e in b.entries:
                s = settings.get(e.key)
                if not s:
                    continue
                shown.append(e.key)
                ok = e.key in r.values
                value = fmt_value(s, r.values[e.key]) if ok else e.raw
                if s.path:
                    value = value.replace(str(Path.home()), '~', 1)
                rows.append((e.key, (Text(e.key, style=f'bold {ROSE}'),
                                     Text(value, style=f'bold {TEXT}' if ok else f'bold {LOVE}'),
                                     Text(_clip(fmt_range(s), 22), style=MUTED),
                                     Text(_clip(s.name, 22), style=MUTED))))
            rest = [s for k, s in settings.items() if k not in shown]
            if rest:
                rows.append(('_sep', (Text('not in file', style=f'italic {MUTED}'), '', '', '')))
                for s in rest:
                    v = live.base.get(s.key) if live and s.key in live.base else s.default
                    rows.append((s.key, (Text(s.key, style=ROSE),
                                         Text('' if v is None else fmt_value(s, v), style=MUTED),
                                         Text(_clip(fmt_range(s), 22), style=MUTED),
                                         Text(_clip(s.name, 22), style=MUTED))))
        return rows

    def _io_rows(self, which):
        def row(key, value, rng, name):
            return key, (Text(key, style=f'bold {ROSE}'), Text(value, style=f'bold {TEXT}'),
                         Text(rng, style=MUTED), Text(name, style=MUTED))
        if which == IN:
            return [row('in', ', '.join(p for p, _ in self.rig.ins) or '(none)', 'port patterns',
                        'where the guitar comes from'),
                    row('latency', str(self.rig.latency), ' | '.join(map(str, LATENCIES)),
                        f'buffer ≈ {self.rig.latency / 48:.1f} ms')]
        return [row('volume', fmt_volume(self.rig.volume), '-inf … +2 dB', 'master volume'),
                row('out', ', '.join(p for p, _ in self.rig.outs) or '(none)', 'port patterns',
                    'where the sound goes')]

    def _io_help(self, which):
        out = Text()
        hw_out, hw_in = self.hw_ports
        skip = ('Midi', 'midi', 'bluez_midi', 'v4l2')
        if which == IN:
            out.append('Patterns match any part of a port name; * is a wildcard. Enter types one.\n',
                       style=MUTED)
            out.append('Inputs you can use: ', style=f'bold {TEXT}')
            ports = [p for p in hw_out if not p.startswith(skip) and not ours(p) and 'monitor' not in p]
        else:
            out.append('+ / - set the master volume (the end of the chain, after everything).\n',
                       style=MUTED)
            out.append('Outputs you can use: ', style=f'bold {TEXT}')
            ports = [p for p in hw_in if not p.startswith(skip) and not ours(p)]
        out.append(', '.join(ports) or '(none found)', style=SUBTLE)
        return out

    def refresh_status(self):
        status = Text()
        if self.prompt_mode:
            status.append(self._prompt_label, style=f'bold {IRIS}')
        elif self.rig.errors:
            line, msg = self.rig.errors[0]
            status.append(f'✖ line {line + 1}: {msg}', style=f'bold {LOVE}')
            if len(self.rig.errors) > 1:
                status.append(f'  (+{len(self.rig.errors) - 1} more)', style=LOVE)
        else:
            status.append_text(self.message)
        if self.stats:
            load, xruns, n = self.stats
            right = Text(load, style=MUTED)
            if xruns:
                right.append(' · ', style=MUTED)
                right.append(xruns, style=GOLD if n else MUTED)
            right.append('  ? help', style=MUTED)
            pad = max(2, self.size.width - status.cell_len - right.cell_len - 3)
            status.append(' ' * pad)
            status.append_text(right)
        try:
            self.query_one('#status', Static).update(status)
        except Exception:
            pass



def _parses(s, raw):
    try:
        parse_value(s, raw)
        return True
    except ValueError:
        return False


def _clip(text, n):
    return text if len(text) <= n else text[:n - 1] + '…'
