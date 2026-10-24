"""Level meters: input, the signal going into the amp, and the final output.

Bars are colored by zone, the way a hardware meter is painted, with a marker
holding the recent peak. The amp meter also says whether the level suits an
amp sim: captures are made with peaks well below 0 dBFS, and a hotter input
than that pushes them into sounding harsher and more compressed.
"""
import math
import time

from rich.text import Text

from .colors import FOAM, GOLD, LOVE, MUTED, OVERLAY, PINE, TEXT

FLOOR = -60.0          # dB at the left end of a bar
HOLD = 1.5             # seconds the peak marker stays put
FALL = 24.0            # dB per second the bar falls back
CLIP_SHOW = 3.0        # seconds a clip stays lit

# (up to dB, color, word): the first zone whose limit the level is under
AMP_ZONES = [(-36, PINE, 'quiet'), (-12, FOAM, 'good'), (-3, GOLD, 'hot'), (99, LOVE, 'too hot')]
LEVEL_ZONES = [(-40, PINE, ''), (-12, FOAM, ''), (-3, GOLD, ''), (99, LOVE, '')]


def to_db(peak):
    return 20 * math.log10(peak) if peak > 1e-6 else -120.0


def zone(zones, db):
    return next(z for z in zones if db < z[0])


class Meter:
    def __init__(self, name, zones, clip_db=-0.3):
        self.name = name
        self.zones = zones
        self.clip_db = clip_db
        self.level = -120.0         # what the bar shows (falls back smoothly)
        self.hold = -120.0
        self.hold_at = 0.0
        self.clip_at = -CLIP_SHOW
        self.last = time.monotonic()
        self.active = False         # false when there is nothing to measure

    def update(self, peak):
        now = time.monotonic()
        db = to_db(peak)
        self.level = max(db, self.level - FALL * (now - self.last))
        self.last = now
        if db >= self.hold or now - self.hold_at > HOLD:
            self.hold, self.hold_at = db, now
        if db >= self.clip_db:
            self.clip_at = now
        self.active = True

    def render(self, bar):
        out = Text()
        out.append(f'{self.name} ', style=f'bold {TEXT}' if self.active else MUTED)
        if not self.active:
            out.append('·' * bar + '        ', style=OVERLAY)
            return out
        span = -FLOOR
        filled = (self.level - FLOOR) / span * bar
        hold_cell = int((self.hold - FLOOR) / span * bar)
        for i in range(bar):
            cell_db = FLOOR + (i + 0.5) / bar * span
            color = zone(self.zones, cell_db)[1]
            if i < int(filled):
                out.append('█', style=color)
            elif i == int(filled) and filled % 1 >= 0.5:
                out.append('▌', style=color)
            elif i == hold_cell and self.hold > FLOOR:
                out.append('│', style=color)
            else:
                out.append('·', style=OVERLAY)
        clipped = time.monotonic() - self.clip_at < CLIP_SHOW
        _, color, word = zone(self.zones, self.hold)
        shown = f'{self.hold:5.1f}' if self.hold > FLOOR else '  -∞ '
        out.append(f' {shown} dB', style=color if self.hold > FLOOR else MUTED)
        if clipped:
            out.append(' CLIP', style=f'bold {LOVE}')
        elif word:
            out.append(f' {word}', style=color)
        return out
