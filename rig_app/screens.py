"""Overlays: the key help and the tuner."""
import math

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from .colors import FOAM, GOLD, IRIS, LOVE, MUTED, OVERLAY, ROSE, TEXT

KEYS = [
    ('Move', [
        ('h l  ← →', 'previous / next block (IN and OUT are at the ends)'),
        ('j k  ↓ ↑', 'previous / next setting'),
        ('gg  G', 'first / last setting'),
        ('ctrl+d  ctrl+u', 'half a page down / up'),
        ('0  $', 'IN / OUT'),
        ('/  n  N', 'find a setting by name, next / previous match'),
        ('<  >', 'previous / next rig in rigs/'),
    ]),
    ('Change (written to the file)', [
        ('+  -', 'nudge the setting (on OUT: master volume)'),
        (']  [', 'nudge in fine steps'),
        ('enter  i', 'type a value'),
        ('dd', 'remove the setting from the file (back to the preset or default)'),
        ('b', 'bypass the block on / off'),
        ('a', 'write every setting of the block into the file'),
        ('u  ctrl+r', 'undo / redo changes made here'),
    ]),
    ('Plugins', [
        ('o', "open the plugin's own window; changes are kept when you close it"),
        ('m', 'capture picker: hear each capture live, enter keeps, esc reverts'),
        ('s', "save the plugin's full state as a preset"),
    ]),
    ('Other', [
        ('t', 'tuner (mutes the output; space unmutes)'),
        ('p', 'looper: count in, record a few bars, play over them'),
        ('B', 'backing tracks from ~/Music/guitar/backing'),
        ('R', 'record until R again: the mix you hear + the guitar alone'),
        ('T', 'timing trainer: the click drops out, come in on 1, see how far off'),
        ('M', 'metronome on / off (in p: tempo + - ] [ t, beats b, volume , .)'),
        ('space', 'stop / play the loop (no loop: the backing track)'),
        ('e', 'edit the rig file in $EDITOR'),
        ('r', 'restart the audio engine'),
        ('?', 'this help'),
        ('q', 'quit (stops the audio)'),
    ]),
]

METERS_HELP = [
    ('in', 'what arrives from the interface. Red or CLIP: turn the gain knob on the interface down.'),
    ('amp', 'what goes into the amp. Good is about -36 to -12 dB; hot or too hot sounds '
            'harsher and squashed, so lower whatever comes before the amp.'),
    ('out', 'what goes to the speakers, after the master volume (OUT block, + / -).'),
]


class HelpScreen(ModalScreen):
    CSS = """
    HelpScreen { align: center middle; background: #000000 70%; }
    #help { width: 96; max-width: 100%; height: auto; max-height: 90%; border: round $primary;
            background: #000000; padding: 0 2; overflow-y: auto; }
    """
    BINDINGS = [Binding('escape,q,question_mark', 'dismiss', priority=True)]

    def compose(self):
        out = Text()
        for i, (title, rows) in enumerate(KEYS):
            out.append(('\n' if i else '') + title + '\n', style=f'bold {IRIS}')
            for keys, what in rows:
                out.append(f'  {keys:<16}', style=f'bold {ROSE}')
                out.append(what + '\n', style=TEXT)
        out.append('\nMeters\n', style=f'bold {IRIS}')
        for name, what in METERS_HELP:
            out.append(f'  {name:<16}', style=f'bold {ROSE}')
            out.append(what + '\n', style=TEXT)
        out.append('\nesc or ? closes', style=MUTED)
        with Vertical(id='help'):
            yield Static(out)

    def on_mount(self):
        self.query_one('#help').border_title = 'keys'


NOTES = ['C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B']


def note_of(freq, ref=440.0):
    """Hz -> (note name, octave, cents off)."""
    n = 12 * math.log2(freq / ref) + 69
    near = round(n)
    return NOTES[near % 12], near // 12 - 1, (n - near) * 100


class TunerScreen(ModalScreen):
    """Shows the note being played. read_freq() is called from a thread."""
    CSS = """
    TunerScreen { align: center middle; background: #000000 70%; }
    #tuner { width: 60; height: auto; border: round $primary; background: #000000;
             padding: 1 2; }
    #tuner-view { width: 100%; text-align: center; }
    """
    BINDINGS = [Binding('escape,q,t', 'dismiss', priority=True),
                Binding('space', 'mute', priority=True)]

    def __init__(self, read_freq, set_mute):
        super().__init__()
        self.read_freq = read_freq
        self.set_mute = set_mute
        self.muted = True
        self.freq = 0.0

    def compose(self):
        with Vertical(id='tuner'):
            yield Static(id='tuner-view')

    def on_mount(self):
        self.query_one('#tuner').border_title = 'tuner'
        self.set_mute(True)
        self.set_interval(0.08, self._poll)
        self._draw()

    def on_unmount(self):
        self.set_mute(False)

    def action_mute(self):
        self.muted = not self.muted
        self.set_mute(self.muted)
        self._draw()

    def _poll(self):
        def work():
            try:
                self.freq = self.read_freq()
            except Exception:
                self.freq = 0.0
            self.app.call_from_thread(self._draw)
        self.run_worker(work, thread=True, group='tuner', exclusive=True, exit_on_error=False)

    def _draw(self):
        out = Text()
        width = 49
        if self.freq < 20:
            out.append('play a string\n\n', style=f'bold {MUTED}')
            out.append('·' * (width // 2) + '│' + '·' * (width // 2) + '\n\n', style=OVERLAY)
            out.append('        \n')
        else:
            name, octave, cents = note_of(self.freq)
            color = FOAM if abs(cents) < 3 else GOLD if abs(cents) < 15 else LOVE
            out.append(f'{name}{octave}\n\n', style=f'bold {color}')
            pos = round((cents + 50) / 100 * (width - 1))
            for i in range(width):
                if i == pos:
                    out.append('█', style=color)
                elif i == width // 2:
                    out.append('│', style=TEXT)
                else:
                    out.append('·', style=OVERLAY)
            out.append('\n')
            out.append(f'{cents:+.0f} cents   {self.freq:.1f} Hz\n', style=color)
            out.append('in tune' if abs(cents) < 3 else 'flat: tighten' if cents < 0 else 'sharp: loosen',
                       style=color)
        out.append('\n\n')
        out.append('output muted' if self.muted else 'output on', style=GOLD if self.muted else FOAM)
        out.append('   space toggles · esc closes', style=MUTED)
        self.query_one('#tuner-view', Static).update(out)
