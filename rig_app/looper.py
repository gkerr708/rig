"""The looper: record a few bars over a click, then play over them.

No footswitch needed: set the tempo and how many bars, press space, and it clicks
one bar in, records exactly that many bars, and starts looping by itself. The
audio side is looper_engine.py, in its own process; it records what goes to the
speakers, so a loop sounds the way the rig did when you played it.

The same process runs the metronome (M anywhere, m in the looper; it uses the
looper's tempo and beats) and plays backing tracks: audio files in ~/Music/guitar/backing/.

Tempo, bars, beats and the track volume are remembered between sessions
(~/.cache/rig/looper.json), not kept in the rig file: a loop carries on when you
switch rigs.
"""
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .colors import FOAM, GOLD, IRIS, LOVE, MUTED, OVERLAY, ROSE, SUBTLE, TEXT
from .host import LOOPER, Child, EngineError
from .lv2 import CACHE

SETTINGS = CACHE / 'looper.json'
LOG = CACHE / 'looper.log'
LOOPS = Path('~/Music/guitar/loops').expanduser()
BACKING = Path('~/Music/guitar/backing').expanduser()
RECORDINGS = Path('~/Music/guitar/recordings').expanduser()
AUDIO = {'.mp3', '.wav', '.flac', '.ogg', '.opus', '.m4a', '.aac'}
DEFAULTS = {'bpm': 100, 'bars': 4, 'beats': 4, 'track_db': -6.0, 'metro_db': 0.0,
            'click_bars': 2, 'gap_bars': 2}
BPM_RANGE = (30, 300)
MAX_BARS = 32
TAP_GAP = 2.0                    # seconds between taps before it starts a new tempo


class LooperHost(Child):
    def __init__(self):
        super().__init__(LOG)
        self.settings = dict(DEFAULTS)
        try:
            self.settings.update(json.loads(SETTINGS.read_text()))
        except (OSError, ValueError):
            pass
        self.ports = None            # (in ports, out ports) for the engine to wire

    def start(self):
        CACHE.mkdir(parents=True, exist_ok=True)
        self._spawn('rig_app.looper_engine', f'{LOOPER}-{os.getpid()}')
        try:
            self.ports = tuple(self.call('ports', timeout=10))
            self.call('track_gain', self.settings['track_db'])
            self.call('metro_gain', self.settings['metro_db'])
        except EngineError as e:
            self._close()
            raise RuntimeError(f'looper failed to start ({e}), see {self.log_path}')

    def stop(self):
        self._close()

    def save_settings(self):
        SETTINGS.write_text(json.dumps(self.settings))


# ---------------------------------------------------------------- reading the status

def place(st):
    """(bar, beat) being played, from 1. In the count-in, bar is 0."""
    beat, beats, pos = st['beat'], st['beats'], st['pos']
    if pos < 0:
        return 0, pos // beat % beats + 1
    pos %= st['length']
    return pos // (beat * beats) + 1, pos // beat % beats + 1


def loop_bpm(st):
    return round(60 * st['sr'] / st['beat'])


def headline(st):
    """What the looper is doing, as (words, color)."""
    state = st.get('state', 'empty')
    if state == 'rec':
        return ('COUNT-IN', GOLD) if st['pos'] < 0 else ('● RECORDING', LOVE)
    if state == 'play':
        return ('● OVERDUB', LOVE) if st['dub'] else ('▶ PLAYING', FOAM)
    if state == 'stopped':
        return '■ STOPPED', SUBTLE
    return 'ready', MUTED


def beat_dots(st, color):
    bar, beat = place(st)
    out = Text()
    for i in range(1, st['beats'] + 1):
        out.append('● ' if i == beat else '○ ', style=f'bold {color}' if i == beat else OVERLAY)
    return out


def clock(frames, sr):
    secs = int(frames / sr)
    return f'{secs // 60}:{secs % 60:02d}'


def track_line(st):
    out = Text('♪ ', style=IRIS)
    out.append(st['track'], style=f'bold {TEXT}' if st['track_on'] else SUBTLE)
    out.append(f'  {clock(st["track_pos"], st["sr"])} / {clock(st["track_len"], st["sr"])}',
               style=TEXT if st['track_on'] else MUTED)
    if not st['track_on']:
        out.append('  paused', style=GOLD)
    return out


def metro_line(st):
    beat, beats, pos = st['metro']
    on = pos // beat % beats + 1
    out = Text('♩ ', style=IRIS)
    out.append(f'metronome {round(60 * st["sr"] / beat)} bpm  ', style=f'bold {TEXT}')
    for i in range(1, beats + 1):
        out.append('● ' if i == on else '○ ', style=f'bold {IRIS}' if i == on else OVERLAY)
    return out


def strip(st):
    """Lines for under the meters while there's a metronome, a loop or a backing track."""
    if not st:
        return None
    lines = []
    if st.get('recording') is not None:
        out = Text('● REC ', style=f'bold {LOVE}')
        out.append(clock(st['recording'], 1), style=f'bold {TEXT}')
        if st.get('rec_error'):
            out.append(f'  ✖ {st["rec_error"]}', style=f'bold {LOVE}')
        out.append('  R stops', style=MUTED)
        lines.append(out)
    if st.get('trainer'):
        tr = st['trainer']
        out = Text('◷ timing trainer ', style=f'bold {IRIS}')
        out.append(f'{round(60 * st["sr"] / tr["beat"])} bpm, round {len(tr["results"]) + 1}', style=TEXT)
        out.append('  T opens it', style=MUTED)
        lines.append(out)
    if st.get('metro'):
        lines.append(metro_line(st))
        lines[-1].append('  M stops · p tempo', style=MUTED)
    if st.get('track'):
        lines.append(track_line(st))
        lines[-1].append('   B backing', style=MUTED)
    if st.get('state', 'empty') != 'empty':
        lines.append(_loop_line(st))
    return Text('\n').join(lines) if lines else None


def _loop_line(st):
    words, color = headline(st)
    out = Text('loop ', style=MUTED)
    out.append(words, style=f'bold {color}')
    if st['state'] != 'stopped':
        bar, _ = place(st)
        bars = st['length'] // (st['beat'] * st['beats'])
        out.append(f'  bar {bar}/{bars}  ' if bar else '  ', style=TEXT)
        out.append_text(beat_dots(st, color))
    out.append(f' {loop_bpm(st)} bpm', style=MUTED)
    if st['layers'] > 1:
        out.append(f' · {st["layers"]} layers', style=MUTED)
    out.append('   space ' + ('plays' if st['state'] == 'stopped' else 'stops') + ' · p looper',
               style=MUTED)
    return out


# ---------------------------------------------------------------- the overlays

class Overlay(ModalScreen):
    """Shared by the looper and backing overlays: status polling and sending commands."""

    def __init__(self, get_looper):
        super().__init__()
        self.get_looper = get_looper     # None until its process is up
        self.st = {}
        self.note = Text()

    def on_mount(self):
        self.set_interval(0.05, self._poll)
        self._draw()

    def _poll(self):
        looper = self.get_looper()
        if not looper:
            return

        def work():
            try:
                self.st = looper.call('status', timeout=2)
            except EngineError:
                self.st = {}
            self.app.call_from_thread(self._draw)
        self.run_worker(work, thread=True, group='looper-poll', exclusive=True, exit_on_error=False)

    def _send(self, *call, then=None, timeout=30):
        looper = self.get_looper()
        if not looper:
            return

        def work():
            try:
                result = looper.call(*call, timeout=timeout)
            except EngineError as e:
                self.app.call_from_thread(self._say, f'✖ {e}', LOVE)
                return
            if then:
                self.app.call_from_thread(then, result)
        self.run_worker(work, thread=True, group='looper', exit_on_error=False)

    def _say(self, msg, color=MUTED):
        self.note = Text(msg, style=color)
        self._draw()

    def _settings(self):
        looper = self.get_looper()
        return looper.settings if looper else DEFAULTS

    def _setting(self, key, value):
        looper = self.get_looper()
        if looper:
            looper.settings[key] = value
            looper.save_settings()
            self._draw()

    def _draw(self):
        raise NotImplementedError


class LooperScreen(Overlay):
    """Set up and run the looper. Closing it leaves the loop playing."""
    CSS = """
    LooperScreen { align: center middle; background: #000000 70%; }
    #looper { width: 72; max-width: 100%; height: auto; border: round $primary;
              background: #000000; padding: 1 2; }
    """
    BINDINGS = [Binding('escape,q,p', 'dismiss', priority=True),
                Binding('space,enter', 'main', priority=True),
                Binding('r', 'record', priority=True),
                Binding('o', 'dub', priority=True),
                Binding('u', 'call("undo")', priority=True),
                Binding('ctrl+r', 'call("redo")', priority=True),
                Binding('x', 'call("clear")', priority=True),
                Binding('plus,equals_sign', 'bpm(1)', priority=True),
                Binding('minus,underscore', 'bpm(-1)', priority=True),
                Binding('right_square_bracket', 'bpm(10)', priority=True),
                Binding('left_square_bracket', 'bpm(-10)', priority=True),
                Binding('l,right', 'bars(1)', priority=True),
                Binding('h,left', 'bars(-1)', priority=True),
                Binding('b', 'beats', priority=True),
                Binding('t', 'tap', priority=True),
                Binding('m', 'metro', priority=True),
                Binding('comma', 'metro_gain(-2)', priority=True),
                Binding('full_stop', 'metro_gain(2)', priority=True),
                Binding('w', 'save', priority=True)]

    def __init__(self, get_looper):
        super().__init__(get_looper)
        self.taps = []

    def compose(self):
        with Vertical(id='looper'):
            yield Static(id='looper-view')

    def on_mount(self):
        self.query_one('#looper').border_title = 'looper · metronome'
        super().on_mount()

    # ------------------------------------------------------------ keys

    def action_main(self):
        """space: record when there's no loop; otherwise stop, or play again."""
        state = self.st.get('state', 'empty')
        if state == 'empty':
            self.action_record()
        elif state == 'stopped':
            self._send('play')
        else:
            self._send('stop')

    def action_record(self):
        looper = self.get_looper()
        if looper:
            s = looper.settings
            self._send('record', s['bpm'], s['bars'], s['beats'])
            self._say('')

    def action_dub(self):
        if self.st.get('state') == 'play':
            self._send('dub', not self.st['dub'])
        else:
            self._say('overdub works while a loop is playing', GOLD)

    def action_call(self, cmd):
        self._send(cmd)

    def action_bpm(self, step):
        lo, hi = BPM_RANGE
        self._setting('bpm', min(max(self._settings()['bpm'] + step, lo), hi))

    def action_bars(self, step):
        self._setting('bars', min(max(self._settings()['bars'] + step, 1), MAX_BARS))

    def action_beats(self):
        beats = self._settings()['beats']
        self._setting('beats', beats + 1 if beats < 7 else 2)

    def action_tap(self):
        now = time.monotonic()
        if self.taps and now - self.taps[-1] > TAP_GAP:
            self.taps = []
        self.taps = (self.taps + [now])[-5:]
        if len(self.taps) < 2:
            self._say('tap again…', IRIS)
            return
        lo, hi = BPM_RANGE
        bpm = round(60 * (len(self.taps) - 1) / (self.taps[-1] - self.taps[0]))
        self._setting('bpm', min(max(bpm, lo), hi))
        self._say(f'tapped {bpm} bpm', IRIS)

    def _setting(self, key, value):
        super()._setting(key, value)
        if key in ('bpm', 'beats') and self.st.get('metro'):     # a running metronome follows
            s = self._settings()
            self._send('metro', s['bpm'], s['beats'])

    def action_metro(self):
        s = self._settings()
        self._send('metro', None if self.st.get('metro') else s['bpm'], s['beats'])

    def action_metro_gain(self, step):
        db = min(max(self._settings()['metro_db'] + step, -30.0), 10.0)
        self._setting('metro_db', db)
        self._send('metro_gain', db)

    def action_save(self):
        if not self.st.get('layers'):
            self._say('nothing recorded yet', GOLD)
            return
        LOOPS.mkdir(parents=True, exist_ok=True)
        path = LOOPS / f'{datetime.now():%Y-%m-%d_%H%M%S}_{loop_bpm(self.st)}bpm.wav'
        shown = str(path).replace(str(Path.home()), '~', 1)
        self._send('save', str(path), then=lambda secs: self._say(f'saved {shown} ({secs:.1f} s)', FOAM))

    # ------------------------------------------------------------ drawing

    def _draw(self):
        st, s = self.st, self._settings()
        out = Text()
        if not self.get_looper():
            out.append('starting the looper…', style=GOLD)
            self.query_one('#looper-view', Static).update(out)
            return
        words, color = headline(st)
        out.append(words, style=f'bold {color}')
        state = st.get('state', 'empty')
        if state == 'empty':
            out.append(f'   space counts in one bar, then records {s["bars"]} '
                       f'bar{"s" if s["bars"] != 1 else ""}', style=MUTED)
        elif state != 'stopped':
            bar, beat = place(st)
            bars = st['length'] // (st['beat'] * st['beats'])
            out.append(f'   beat {beat} of {st["beats"]}' if not bar else f'   bar {bar} of {bars}',
                       style=TEXT)
            out.append('\n\n')
            out.append_text(beat_dots(st, color))
            out.append('\n')
            out.append_text(self._progress(st, color, 64))
        out.append('\n\n')
        # the next take's settings; a loop that exists keeps its own
        out.append(f'{s["bpm"]} bpm', style=f'bold {ROSE}')
        out.append(f'   {s["bars"]} bar{"s" if s["bars"] != 1 else ""} of {s["beats"]}/4', style=f'bold {ROSE}')
        secs = 60 / s['bpm'] * s['beats'] * s['bars']
        out.append(f'   {secs:.1f} s', style=MUTED)
        if st.get('layers') and state != 'rec':
            bars = st['length'] // (st['beat'] * st['beats'])
            if (loop_bpm(st), bars, st['beats']) != (s['bpm'], s['bars'], s['beats']):
                out.append(f'\nthis loop: {loop_bpm(st)} bpm, {bars} bar{"s" if bars != 1 else ""} '
                           f'of {st["beats"]}/4 '
                           '(new settings are for the next take)', style=MUTED)
            out.append(f'\nlayers {st["layers"]}', style=TEXT)
            if st['redo']:
                out.append(f' (+{st["redo"]} undone)', style=MUTED)
        if st.get('metro'):
            out.append('\n')
            out.append_text(metro_line(st))
        out.append(f'\nmetronome volume {s["metro_db"]:+.0f} dB', style=MUTED)
        if st.get('metro'):
            out.append('\nthe metronome counts in: a take starts on its next bar', style=MUTED)
        if st.get('rtl'):
            out.append(f'   timing corrected by {st["rtl"] / st["sr"] * 1000:.1f} ms', style=MUTED)
        if self.note.plain:
            out.append('\n')
            out.append_text(self.note)
        out.append('\n')
        keys = [('space', self._space_does(state)), ('r', 'record a new take'),
                ('o', 'overdub on / off'), ('u  ctrl+r', 'undo / redo a layer'),
                ('x', 'clear the loop'), ('w', 'save it as a WAV'),
                ('+ -  ] [', 'tempo ±1 / ±10'), ('t', 'tap the tempo'),
                ('h l', 'fewer / more bars'), ('b', 'beats per bar'),
                ('m', 'metronome on / off'),
                (', .', 'metronome volume'), ('esc', 'close (loop keeps going)')]
        for i, (k, what) in enumerate(keys):
            out.append('\n' if i % 2 == 0 else '')
            out.append(f'{k:<10}', style=f'bold {ROSE}')
            out.append(f'{what:<24}', style=SUBTLE)
        self.query_one('#looper-view', Static).update(out)

    @staticmethod
    def _space_does(state):
        return {'empty': 'count in and record', 'rec': 'cancel the take', 'play': 'stop',
                'stopped': 'play'}.get(state, '')

    @staticmethod
    def _progress(st, color, width):
        """The loop as a bar, with a tick at each bar line."""
        length, per_bar = st['length'], st['beat'] * st['beats']
        pos = st['pos'] % length if st['pos'] >= 0 else -1
        out = Text()
        for i in range(width):
            at = i * length // width
            tick = at // per_bar != ((i - 1) * length // width) // per_bar if i else False
            done = pos >= 0 and at <= pos
            out.append('┃' if tick else '━' if done else '─',
                       style=color if done else MUTED if tick else OVERLAY)
        return out


class BackingScreen(Overlay):
    """Pick and play a backing track. Closing it leaves the track playing."""
    CSS = """
    BackingScreen { align: center middle; background: #000000 70%; }
    #backing { width: 72; max-width: 100%; height: auto; max-height: 90%; border: round $primary;
               background: #000000; padding: 1 2; }
    #backing-list { height: auto; max-height: 16; border: none; background: #000000; margin-top: 1; }
    """
    BINDINGS = [Binding('escape,q,B', 'dismiss', priority=True),
                Binding('space', 'play', priority=True),
                Binding('h,left', 'seek(-5)', priority=True),
                Binding('l,right', 'seek(5)', priority=True),
                Binding('0', 'seek(-36000)', priority=True),
                Binding('plus,equals_sign', 'gain(1)', priority=True),
                Binding('minus,underscore', 'gain(-1)', priority=True),
                Binding('j', 'move(1)', priority=True),
                Binding('k', 'move(-1)', priority=True)]

    def compose(self):
        with Vertical(id='backing'):
            yield Static(id='backing-view')
            yield OptionList(id='backing-list')

    def on_mount(self):
        self.query_one('#backing').border_title = 'backing track'
        files = sorted(p for p in BACKING.glob('*') if p.suffix.lower() in AUDIO)
        options = self.query_one('#backing-list', OptionList)
        options.add_options([Option(p.name, id=str(p)) for p in files])
        options.display = bool(files)
        if files:
            options.highlighted = 0
        options.focus()
        super().on_mount()

    def action_move(self, step):
        options = self.query_one('#backing-list', OptionList)
        options.action_cursor_down() if step > 0 else options.action_cursor_up()

    def on_option_list_option_selected(self, event):
        path, name = event.option.id, Path(event.option.id).name
        if m := re.search(r'(\d+)\s*bpm', name, re.I):         # the looper and metronome follow it
            lo, hi = BPM_RANGE
            self._setting('bpm', min(max(int(m.group(1)), lo), hi))
            if self.st.get('metro'):
                self._send('metro', self._settings()['bpm'], self._settings()['beats'])
        self._say(f'loading {name}…', GOLD)
        self._send('track', path, timeout=120,
                   then=lambda secs: self._say(f'playing {name} ({clock(secs, 1)})', FOAM))

    def action_play(self):
        if self.st.get('track'):
            self.note = Text()
            self._send('track_play', not self.st['track_on'])

    def action_seek(self, secs):
        self._send('track_seek', secs)

    def action_gain(self, step):
        db = min(max(self._settings()['track_db'] + step, -40.0), 6.0)
        self._setting('track_db', db)
        self._send('track_gain', db)

    def _draw(self):
        out = Text()
        if not self.get_looper():
            out.append('starting the player…', style=GOLD)
        elif not self.query_one('#backing-list', OptionList).option_count:
            out.append('No tracks yet. Put audio files (mp3, wav, flac, ogg, m4a…) in\n', style=TEXT)
            out.append(str(BACKING).replace(str(Path.home()), '~', 1), style=f'bold {ROSE}')
            out.append('  and open this again.', style=TEXT)
        elif self.st.get('track'):
            st = self.st
            out.append_text(track_line(st))
            out.append('\n')
            width, done = 64, st['track_pos'] / max(st['track_len'], 1)
            out.append('━' * round(done * width), style=IRIS)
            out.append('─' * (width - round(done * width)), style=OVERLAY)
        else:
            out.append('pick a track: j k move, enter plays', style=MUTED)
        out.append(f'\nvolume {self._settings()["track_db"]:+.0f} dB', style=MUTED)
        if self.note.plain:
            out.append('\n')
            out.append_text(self.note)
        out.append('\n')
        for i, (k, what) in enumerate([('enter', 'play the highlighted track'), ('space', 'pause / play'),
                                       ('h l', 'back / ahead 5 s'), ('0', 'back to the start'),
                                       ('+ -', 'volume'), ('esc', 'close (keeps playing)')]):
            out.append('\n' if i % 2 == 0 else '')
            out.append(f'{k:<7}', style=f'bold {ROSE}')
            out.append(f'{what:<27}', style=SUBTLE)
        self.query_one('#backing-view', Static).update(out)


def verdict(ms):
    """How an entry `ms` off the beat went, as (words, color)."""
    if abs(ms) < 15:
        return 'right on it', FOAM
    if abs(ms) < 35:
        return ('a little late', GOLD) if ms > 0 else ('a little early', GOLD)
    return ('late', LOVE) if ms > 0 else ('early', LOVE)


class TrainerScreen(Overlay):
    """Timing practice: the click plays, drops out for a few bars, and you come in on
    the first beat after the gap. Shows how early or late you were, round after round."""
    CSS = """
    TrainerScreen { align: center middle; background: #000000 70%; }
    #trainer { width: 72; max-width: 100%; height: auto; border: round $primary;
               background: #000000; padding: 1 2; }
    """
    BINDINGS = [Binding('escape,q,T', 'dismiss', priority=True),
                Binding('space,enter', 'toggle', priority=True),
                Binding('plus,equals_sign', 'bpm(1)', priority=True),
                Binding('minus,underscore', 'bpm(-1)', priority=True),
                Binding('right_square_bracket', 'bpm(10)', priority=True),
                Binding('left_square_bracket', 'bpm(-10)', priority=True),
                Binding('l,right', 'bars("gap_bars", 1)', priority=True),
                Binding('h,left', 'bars("gap_bars", -1)', priority=True),
                Binding('k,up', 'bars("click_bars", 1)', priority=True),
                Binding('j,down', 'bars("click_bars", -1)', priority=True),
                Binding('b', 'beats', priority=True),
                Binding('comma', 'gain(-2)', priority=True),
                Binding('full_stop', 'gain(2)', priority=True)]

    def compose(self):
        with Vertical(id='trainer'):
            yield Static(id='trainer-view')

    def on_mount(self):
        self.query_one('#trainer').border_title = 'timing trainer'
        super().on_mount()

    def _start(self):
        s = self._settings()
        self._send('trainer', s['bpm'], s['beats'], s['click_bars'], s['gap_bars'])

    def action_toggle(self):
        if self.st.get('trainer'):
            self._send('trainer', None)
        else:
            self._start()

    def _setting(self, key, value):
        super()._setting(key, value)
        if self.st.get('trainer'):           # running: start over with the new settings
            self._start()

    def action_bpm(self, step):
        lo, hi = BPM_RANGE
        self._setting('bpm', min(max(self._settings()['bpm'] + step, lo), hi))

    def action_bars(self, key, step):
        self._setting(key, min(max(self._settings()[key] + step, 1), 16))

    def action_beats(self):
        beats = self._settings()['beats']
        self._setting('beats', beats + 1 if beats < 7 else 2)

    def action_gain(self, step):
        db = min(max(self._settings()['metro_db'] + step, -30.0), 10.0)
        self._setting('metro_db', db)
        self._send('metro_gain', db)

    def _draw(self):
        st, s = self.st, self._settings()
        out = Text()
        if not self.get_looper():
            self.query_one('#trainer-view', Static).update(Text('starting…', style=GOLD))
            return
        tr = st.get('trainer')
        cb, gb = s['click_bars'], s['gap_bars']
        out.append(f'{cb} bar{"s" if cb != 1 else ""} of click, then {gb} bar{"s" if gb != 1 else ""} '
                   'of silence: keep counting, and come in on beat 1 after the gap.\n\n', style=SUBTLE)
        if not tr:
            out.append('ready', style=f'bold {MUTED}')
            out.append('   space starts', style=MUTED)
        else:
            pos, bar = tr['pos'], tr['beat'] * tr['beats']
            in_cycle = pos % tr['cycle']
            if in_cycle < tr['click_bars'] * bar:
                beat = in_cycle // tr['beat'] % tr['beats'] + 1
                out.append('CLICK  ', style=f'bold {FOAM}')
                for i in range(1, tr['beats'] + 1):
                    out.append('● ' if i == beat else '○ ', style=f'bold {FOAM}' if i == beat else OVERLAY)
            else:
                out.append('SILENT  ', style=f'bold {GOLD}')
                out.append('keep counting…', style=GOLD)       # no position shown: count it yourself
            self._results(out, tr['results'], st['sr'], tr['beat'])
        out.append(f'\n\n{s["bpm"]} bpm  {s["beats"]}/4', style=f'bold {ROSE}')
        out.append(f'   click volume {s["metro_db"]:+.0f} dB', style=MUTED)
        if self.note.plain:
            out.append('\n')
            out.append_text(self.note)
        out.append('\n')
        keys = [('space', 'start / stop'), ('+ -  ] [', 'tempo ±1 / ±10'),
                ('h l', 'fewer / more silent bars'), ('j k', 'fewer / more click bars'),
                ('b', 'beats per bar'), (', .', 'click volume'), ('esc', 'close (stops when you stop)')]
        for i, (k, what) in enumerate(keys):
            out.append('\n' if i % 2 == 0 else '')
            out.append(f'{k:<10}', style=f'bold {ROSE}')
            out.append(f'{what:<26}', style=SUBTLE)
        self.query_one('#trainer-view', Static).update(out)

    @staticmethod
    def _results(out, results, sr, beat):
        if not results:
            out.append('\n\nfirst round: listen, count through the silence, come in on 1', style=MUTED)
            return
        _, offset, playing = results[-1]
        out.append('\n\n')
        if offset is None:
            out.append("didn't hear you come in", style=f'bold {LOVE}')
        else:
            ms = offset / sr * 1000
            words, color = verdict(ms)
            out.append(f'{ms:+.0f} ms', style=f'bold {color}')
            out.append(f'  {words}', style=color)
            if abs(ms) >= 35:
                out.append(f'  ({abs(offset) / beat:.0%} of a beat)', style=MUTED)
            # where it landed: ±100 ms across the line, the beat in the middle
            width = 41
            at = min(max(round(ms / 100 * (width // 2)) + width // 2, 0), width - 1)
            out.append('\nearly ', style=MUTED)
            for i in range(width):
                out.append('●' if i == at else '┃' if i == width // 2 else '─',
                           style=color if i == at else TEXT if i == width // 2 else OVERLAY)
            out.append(' late', style=MUTED)
        if playing:
            out.append('\nyou were still playing in the gap: stop, count, then come in', style=GOLD)
        hits = [r[1] / sr * 1000 for r in results if r[1] is not None]
        out.append(f'\nround {len(results)}', style=TEXT)
        if hits:
            avg = sum(hits) / len(hits)
            off = sum(abs(h) for h in hits) / len(hits)
            out.append(f'   average {off:.0f} ms off', style=TEXT)
            if len(hits) >= 3 and abs(avg) >= 10:
                out.append(f', you tend to come in {"late" if avg > 0 else "early"} ({avg:+.0f} ms)',
                           style=GOLD)
        out.append('\n')
        for _, offset, _ in results[-10:]:
            if offset is None:
                out.append(' miss', style=LOVE)
            else:
                ms = offset / sr * 1000
                out.append(f' {ms:+.0f}', style=verdict(ms)[1])
