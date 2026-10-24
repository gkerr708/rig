"""Capture picker: choose an amp capture or impulse response by ear.

Moving the highlight loads each file live; Enter keeps it, Esc puts back what
was there. NAM captures record how loud they are, so switching captures moves
the output level by the difference and the volume stays put.
"""
import json
import re
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .colors import IRIS, MUTED, TEXT

MODELS = Path('~/Music/guitar/models').expanduser()
IRS = Path('~/Music/guitar/irs').expanduser()
IR_EXTS = ('.wav', '.flac', '.aif', '.aiff')
LEVEL_KEYS = ('output_level',)         # where a capture's level goes (NAM)
NOMINAL = -18.0                        # loudness (dB) assumed when nothing to compare with


def is_ir(s):
    words = re.split(r'[^a-z]+', f'{s.key} {s.name}'.lower())
    return any(w in ('ir', 'irs', 'cab', 'cabinet', 'impulse') for w in words)


def capture_setting(settings, selected_key):
    """The file setting to pick for: the selected row if it is one, else the first."""
    paths = [s for s in settings.values() if s.path]
    chosen = settings.get(selected_key)
    return chosen if chosen in paths else (paths[0] if paths else None)


def capture_files(plugin, s):
    """(folder, files) the picker offers for setting s."""
    if is_ir(s):
        folder, exts = IRS, IR_EXTS
    elif 'neural-amp-modeler' in plugin.uri or 'neural_amp_modeler' in plugin.uri:
        folder, exts = MODELS, ('.nam',)
    else:
        folder, exts = MODELS, ('.json', '.aidax', '.nam')
    files = sorted((p for p in folder.rglob('*') if p.suffix.lower() in exts and p.is_file()),
                   key=lambda p: str(p.relative_to(folder)).lower())
    return folder, files


def loudness(path):
    """A NAM capture's recorded loudness in dB, or None."""
    if not path or not str(path).endswith('.nam'):
        return None
    try:
        with open(path) as f:
            value = (json.load(f).get('metadata') or {}).get('loudness')
        return float(value) if value is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def matched_level(level_setting, old_level, old_path, new_path):
    """Output level for new_path that sounds as loud as old_path did at old_level."""
    new = loudness(new_path)
    if new is None or old_level is None:
        return None
    old = loudness(old_path)
    v = old_level + (old if old is not None else NOMINAL) - new
    lo, hi = level_setting.minimum, level_setting.maximum
    return round(min(hi if hi is not None else v, max(lo if lo is not None else v, v)), 1)


class CapturePicker(ModalScreen):
    """Lists files; posts every highlight to on_move. Dismisses with a path, or None."""

    CSS = """
    CapturePicker { align: center middle; background: #000000 70%; }
    #picker { width: 90; max-width: 100%; height: 80%; border: round $primary;
              background: #000000; padding: 0 1; }
    #picker-help { height: auto; color: $text-muted; }
    #picker OptionList { height: 1fr; background: #000000; border: none; }
    """
    BINDINGS = [
        Binding('escape', 'cancel', 'Revert', priority=True),
        Binding('j', 'move(1)', show=False),
        Binding('k', 'move(-1)', show=False),
    ]

    def __init__(self, title, folder, files, current, on_move, level_for=None):
        super().__init__()
        self.title_text = title
        self.folder = folder
        self.files = files
        self.current = current
        self.on_move = on_move
        self.level_for = level_for      # path -> output level it would get, or None
        self.ready = False              # ignore the highlight from opening on the current file

    def compose(self):
        with Vertical(id='picker'):
            yield Static(id='picker-help')
            yield OptionList(*[Option(self._row(p), id=str(p)) for p in self.files])

    def _row(self, p):
        out = Text()
        out.append('▶ ' if str(p) == self.current else '  ', style=IRIS)
        out.append(str(p.relative_to(self.folder)), style=f'bold {TEXT}' if str(p) == self.current else TEXT)
        loud = loudness(p)
        if loud is not None:
            out.append(f'   {loud:.1f} dB', style=MUTED)
        level = self.level_for(p) if self.level_for else None
        if level is not None:
            out.append(f'  → level {level:+g}', style=MUTED)
        return out

    def on_mount(self):
        box = self.query_one('#picker')
        box.border_title = self.title_text
        where = str(self.folder).replace(str(Path.home()), '~')
        help_ = Text(f'{where}   ↑↓ audition · Enter keep · Esc revert', style=MUTED)
        if not self.files:
            help_ = Text(f'No files in {where} yet. Put captures there and press m again.', style=MUTED)
        self.query_one('#picker-help', Static).update(help_)
        options = self.query_one(OptionList)
        names = [str(p) for p in self.files]
        if self.current in names:
            options.highlighted = names.index(self.current)
        options.focus()
        self.call_after_refresh(self._arm)

    def _arm(self):
        self.ready = True

    def on_option_list_option_highlighted(self, event):
        if self.ready:
            self.on_move(event.option.id)

    def on_option_list_option_selected(self, event):
        self.dismiss(event.option.id)

    def action_move(self, step):
        options = self.query_one(OptionList)
        options.action_cursor_down() if step > 0 else options.action_cursor_up()

    def action_cancel(self):
        self.dismiss(None)
