"""Backing tracks from chord charts: drums, bass and keys or guitar, rendered to mp3.

    rig --tracks          # render every tracks/*.track that changed
    rig --tracks --all    # render them all again

A .track file is a chart, in the same spirit as a .rig file:

    title = 12-bar blues shuffle in A
    bpm   = 92
    style = shuffle          # rock | shuffle | slowblues | metal | funk | punk | halftime | swing
    keys  = organ            # organ | piano | epiano | clav | guitar | none (default: the style's)
    minutes = 3              # how long, roughly (the chart repeats to fill it)

    | A7 | D7 | A7 | A7 |    # one bar each; | A7 D7 | splits a bar
    | D7 | D7 | A7 | A7 |
    | E7 | D7 | A7 | E7 |

A file with no chart is a drum track. Every track starts with a bar of sticks,
has a fill every 8 bars, and ends on the first chord. The bpm goes into the file
name, so the looper and metronome pick it up when the track plays.

The chart becomes MIDI, and FluidSynth plays it with a General MIDI SoundFont
(sampled instruments); ffmpeg encodes the mp3.
"""
import re
import struct
import subprocess
import tempfile
from pathlib import Path

import numpy as np

SR = 48000
ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / 'tracks'
OUT = Path('~/Music/guitar/backing').expanduser()
SOUNDFONTS = [Path('~/.local/share/soundfonts/GeneralUser-GS.sf2').expanduser(),
              Path('/usr/share/soundfonts/FluidR3_GM.sf2'), Path('/usr/share/soundfonts/default.sf2')]
TPB = 480                        # MIDI ticks per beat
SEED = 7

# ---------------------------------------------------------------- styles
# Drum steps per bar (16 = sixteenths, 12 = triplet eighths). X accent, x hit, g ghost,
# o open hi-hat. Bass: (step, degree, length in steps), or 'walk' for a walking line.
# Comp: x holds to the next hit, h holds half as long, s is a short stab (a palm-muted
# chug on guitar); a list of comps is picked from bar by bar.

STYLES = {
    'rock': dict(steps=16, keys='organ', bass='picked', drums={
        'kick': 'X.....x.X.x.....', 'snare': '....X.......X...', 'hat': 'x.x.x.x.x.x.x.o.'},
        bassline=[(i * 2, 'R', 2) for i in range(8)], comp='x.......x.......'),
    'shuffle': dict(steps=12, keys='organ', bass='finger', drums={
        'kick': 'X.....x.....', 'snare': '...X.....X..', 'ride': 'x.gx.gx.gx.g', 'pedal': '...x.....x..'},
        bassline=[(0, 'R', 2), (2, '3', 1), (3, '5', 2), (5, '6', 1),
                  (6, 'b7', 2), (8, '6', 1), (9, '5', 2), (11, '3', 1)], comp='..s..s..s..s'),
    'slowblues': dict(steps=12, keys='epiano', bass='finger', drums={
        'kick': 'X.....x.....', 'snare': '...X.....X..', 'hat': 'xggxggxggxgg'},
        bassline=[(0, 'R', 3), (3, '3', 3), (6, '5', 3), (9, '6', 3)], comp='x.....x.....'),
    'metal': dict(steps=16, keys='guitar', bass='picked', drums={
        'kick': 'xxxxxxxxxxxxxxxx', 'snare': '....X.......X...', 'ride': 'x...x...x...x...'},
        bassline=[(i, 'R', 1) for i in range(16)], comp='ssssssssssssssss'),
    'funk': dict(steps=16, keys='clav', bass='finger', drums={
        'kick': 'X..x..x...x..x..', 'snare': '....X..g.g..X..g', 'hat': 'xgxgxgxgxgxgxgog'},
        bassline=[(0, 'R', 2), (3, 'O', 1), (6, 'R', 1), (7, 'b7', 1), (10, 'R', 2), (13, 'O', 1),
                  (14, '5', 1)], comp='..s..s.s..s..s..'),
    'punk': dict(steps=16, keys='guitar', bass='picked', drums={
        'kick': 'X...x...X...x...', 'snare': '....X.......X...', 'hat': 'x.x.x.x.x.x.x.x.'},
        bassline=[(i * 2, 'R', 2) for i in range(8)], comp='s.s.s.s.s.s.s.s.'),
    'halftime': dict(steps=16, keys='piano', bass='finger', drums={
        'kick': 'X.........x.....', 'snare': '........X.......', 'hat': 'x.x.x.x.x.x.x.x.'},
        bassline=[(0, 'R', 6), (6, 'R', 2), (8, '5', 8)], comp='x.......x.......'),
    'swing': dict(steps=12, keys='piano', bass='upright', kit=32, fills=False, voicing='shell', drums={
        'ride': 'x..x.xx..x.x', 'pedal': '...x.....x..', 'kick': 'g..g..g..g..', 'snare': '.......g...g'},
        bassline='walk', comp=['h....h......', '...h.....h..', 'h.....h.....', '..h.....h...',
                               'h........h..', '.....h....h.']),
}
VELOCITY = {'X': 118, 'x': 92, 'g': 42, 'o': 92}
DEGREES = {'R': 0, 'O': 12, '5': 7, '6': 9, 'b7': 10}

# General MIDI: drum notes on channel 10, and programs
DRUM = {'kick': 36, 'snare': 38, 'hat': 42, 'open': 46, 'pedal': 44, 'ride': 51, 'crash': 49,
        'sticks': 31, 'tom_hi': 50, 'tom_mid': 47, 'tom_low': 45, 'tom_floor': 43}
PROGRAM = {'organ': 16, 'piano': 0, 'epiano': 4, 'clav': 7, 'guitar': 30,
           'upright': 32, 'finger': 33, 'picked': 34}
DRUMS, BASS, KEYS = 9, 0, 1                      # MIDI channels
MIX = {DRUMS: (112, 0, 40), BASS: (104, 0, 10), KEYS: (82, 0, 50)}   # volume, pan, reverb
GUITAR_MIX = (90, 0, 30)

# ---------------------------------------------------------------- chords

NOTE = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
QUALITY = {'': (0, 4, 7), 'm': (0, 3, 7), '7': (0, 4, 7, 10), 'm7': (0, 3, 7, 10),
           'maj7': (0, 4, 7, 11), '9': (0, 4, 10, 14), 'm9': (0, 3, 10, 14), '5': (0, 7, 12),
           'sus2': (0, 2, 7), 'sus4': (0, 5, 7), 'dim': (0, 3, 6), '6': (0, 4, 7, 9),
           'm7b5': (0, 3, 6, 10), 'dim7': (0, 3, 6, 9), '7b9': (0, 4, 10, 13)}
CHORD = re.compile(r'^([A-G])([#b]?)(.*)$')


def chord(name):
    """'F#m7' -> (root pitch class, intervals)."""
    m = CHORD.match(name)
    if not m or m.group(3) not in QUALITY:
        raise ValueError(f"unknown chord '{name}' (qualities: {', '.join(q or 'major' for q in QUALITY)})")
    root = (NOTE[m.group(1)] + {'#': 1, 'b': -1, '': 0}[m.group(2)]) % 12
    return root, QUALITY[m.group(3)]


def parse(text):
    spec, bars = {}, []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if line.startswith('|'):
            for cell in line.strip('|').split('|'):
                names = cell.split()
                if not names:
                    raise ValueError(f'line {n}: empty bar')
                bars.append([chord(c) for c in names])
        elif '=' in line:
            key, value = (x.strip() for x in line.split('=', 1))
            spec[key] = value
        else:
            raise ValueError(f"line {n}: expected 'name = value' or a chart line starting with |")
    style = spec.get('style', 'rock')
    if style not in STYLES:
        raise ValueError(f"unknown style '{style}' ({', '.join(STYLES)})")
    keys = spec.get('keys', STYLES[style]['keys'])
    if keys != 'none' and keys not in PROGRAM:
        raise ValueError(f"unknown keys '{keys}' (organ, piano, epiano, clav, guitar, none)")
    return {'title': spec.get('title', 'track'), 'bpm': int(spec.get('bpm', 100)), 'style': style,
            'keys': keys, 'minutes': float(spec.get('minutes', 3)), 'bars': bars}

# ---------------------------------------------------------------- arranging


def voicing(root, intervals, guitar=False, shell=False):
    """Keys: chord around E3–E4, root at the bottom. Shell: the 3rd and 7th with a 9th
    on top, no root (the bass has it). Guitar: a power chord from E2."""
    if guitar:
        base = 40 + (root - 4) % 12
        return [base, base + 7, base + 12]
    if shell:
        third = next((i for i in intervals if i in (3, 4, 5)), 4)
        seventh = next((i for i in intervals if i in (9, 10, 11)), 9)
        top = 13 if 13 in intervals else 14 if 6 not in intervals else 12
        notes = sorted(n % 12 for n in (third, seventh))
        base = 50 + (root + notes[0] - 2) % 12                  # lowest note D3 .. C#4
        second = base + notes[1] - notes[0]
        high = base + (top - notes[0]) % 12
        return [base, second, high if high > second else high + 12]
    base = 52 + (root - 4) % 12
    return [base + i for i in intervals]


def walk(rng, root, intervals, beats, prev, target):
    """A walking bass line over one chord: the root, chord tones, then a half step
    into the next chord's root. Notes stay between E1 and G2, near the last one."""
    def near(pc, ref):
        options = [m for m in range(28, 44) if m % 12 == pc % 12]
        return min(options, key=lambda m: abs(m - ref))
    line = [near(root, prev)]
    tones = [i for i in intervals if i < 12]
    for _ in range(beats - 2):
        line.append(near(root + int(rng.choice(tones[1:] or [7])), line[-1]))
    if beats > 1:
        goal = near(target, line[-1])
        line.append(goal + (1 if goal < 40 and rng.random() < 0.5 or goal <= 29 else -1))
    return line


def arrange(spec):
    """The chart as notes: [(start beat, channel, note, velocity, length in beats)]."""
    rng = np.random.default_rng(SEED)
    style = STYLES[spec['style']]
    shell = style.get('voicing') == 'shell'
    walk_note = 33
    steps = style['steps']
    step = 4 / steps                             # in beats
    notes = []

    def add(beat, ch, note, vel, length, loose=0.01):
        beat = max(0.0, beat + rng.normal(0, loose))           # played, not quantized
        vel = int(np.clip(vel * (1 + rng.normal(0, 0.06)), 1, 127))
        notes.append((beat, ch, note, vel, length))

    chart = spec['bars']
    bar_beats = 4
    if chart:
        repeats = max(1, round(spec['minutes'] * 60 / (bar_beats * 60 / spec['bpm'] * len(chart))))
        bars = chart * repeats + [chart[0]]      # end on the first chord
    else:
        total = spec['minutes'] * 60 / (bar_beats * 60 / spec['bpm'])
        bars = [None] * (max(4, round(total / 8)) * 8) + [None]
    for b in range(4):                            # a bar of sticks to count in
        add(b, DRUMS, DRUM['sticks'], 100, 0.1, 0)
    last = len(bars) - 1
    guitar = spec['keys'] == 'guitar'
    for n, bar in enumerate(bars):
        at = (n + 1) * bar_beats
        if n == last:                             # the ending: one hit, let it ring
            add(at, DRUMS, DRUM['kick'], 118, 1, 0)
            add(at, DRUMS, DRUM['crash'], 118, 4, 0)
        else:
            fill = n % 8 == 7 and n != last - 1 and style.get('fills', True)
            if n % 8 == 0:
                add(at, DRUMS, DRUM['crash'], 105 if style.get('fills', True) else 70, 2)
            for name, pattern in style['drums'].items():
                for i, ch in enumerate(pattern):
                    if fill and i >= steps // 2 and name != 'kick':
                        continue
                    if n % 8 == 0 and i == 0 and name in ('hat', 'ride'):
                        continue                  # the crash takes the downbeat
                    if ch in VELOCITY:
                        drum = 'open' if ch == 'o' else name
                        add(at + i * step, DRUMS, DRUM[drum], VELOCITY[ch], step)
            if fill:                              # a tom run down into the next section
                toms = ['snare', 'snare', 'tom_hi', 'tom_hi', 'tom_mid', 'tom_mid', 'tom_low', 'tom_floor']
                half = steps // 2
                for k, i in enumerate(range(half, steps, max(1, half // len(toms)))):
                    add(at + i * step, DRUMS, DRUM[toms[min(k, len(toms) - 1)]], 90 + 3 * k, step)
        if bar is None:
            continue
        chords = bar if n != last else bar[:1]
        span = steps // len(chords)
        comp = style['comp']
        if isinstance(comp, list):
            comp = comp[rng.integers(len(comp))]
        following = bars[n + 1] if n + 1 < len(bars) and bars[n + 1] else bars[0]
        for c, (root, intervals) in enumerate(chords):
            start = at + c * span * step
            minor = 3 in intervals and 4 not in intervals
            bass_root = 28 + (root - 4) % 12                     # E1 .. D#2
            if n == last:
                add(start, BASS, bass_root, 105, 4, 0)
                if spec['keys'] != 'none':
                    for m in voicing(root, intervals, guitar, shell):
                        add(start, KEYS, m, 95, 6, 0)
                continue
            if style['bassline'] == 'walk':
                beats = span * 4 // steps
                target = (chords[c + 1] if c + 1 < len(chords) else following[0])[0]
                for k, m in enumerate(walk(rng, root, intervals, beats, walk_note, target)):
                    add(start + k, BASS, m, 100 if k == 0 else 88, 0.95)
                    walk_note = m
            for s, degree, length in ([] if style['bassline'] == 'walk' else style['bassline']):
                if not c * span <= s < (c + 1) * span:
                    continue
                offset = DEGREES.get(degree, 0)
                if degree == '3':
                    offset = 3 if minor else 4
                elif degree == '5' and 6 in intervals and 7 not in intervals:
                    offset = 6                                    # diminished fifth
                accent = 105 if s % (steps // 4) == 0 else 88
                add(at + s * step, BASS, bass_root + offset, accent, length * step * 0.9)
            if spec['keys'] == 'none':
                continue
            hits = [i for i, ch in enumerate(comp) if ch in 'xhs' and c * span <= i < (c + 1) * span]
            hits = hits or [c * span]                 # every chord gets played at least once
            for k, i in enumerate(hits):
                nxt = hits[k + 1] if k + 1 < len(hits) else (c + 1) * span
                stab = comp[i] == 's'
                length = (0.6 if stab else (0.5 if comp[i] == 'h' else 0.95) * (nxt - i)) * step
                vel = (100 if i % (steps // 4) == 0 else 82) if stab else 88
                for j, m in enumerate(voicing(root, intervals, guitar, shell)):
                    # a strum: each string a touch later
                    add(at + i * step + j * (0.012 if guitar or spec['keys'] == 'piano' else 0), KEYS, m,
                        vel, length, 0.006)
    return sorted(notes)

# ---------------------------------------------------------------- MIDI


def _vlq(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def midi(spec, notes):
    """A type-0 MIDI file."""
    events = [(0, bytes([0xFF, 0x51, 3]) + round(60e6 / spec['bpm']).to_bytes(3, 'big'))]
    keys = spec['keys']
    programs = {BASS: PROGRAM[STYLES[spec['style']]['bass']], DRUMS: STYLES[spec['style']].get('kit', 0)}
    if keys != 'none':
        programs[KEYS] = PROGRAM[keys]
    for ch, program in programs.items():
        events.append((0, bytes([0xC0 | ch, program])))
    for ch, (volume, pan, reverb) in MIX.items():
        if ch == KEYS and keys == 'guitar':
            volume, pan, reverb = GUITAR_MIX
        events += [(0, bytes([0xB0 | ch, 7, volume])), (0, bytes([0xB0 | ch, 10, 64 + pan])),
                   (0, bytes([0xB0 | ch, 91, reverb])), (0, bytes([0xB0 | ch, 93, 0]))]
    for beat, ch, note, vel, length in notes:
        on = round(beat * TPB)
        events.append((on, bytes([0x90 | ch, note, vel])))
        events.append((on + max(1, round(length * TPB)), bytes([0x80 | ch, note, 0])))
    events.sort(key=lambda e: (e[0], e[1][0] & 0xF0 == 0x90))   # note-offs before note-ons
    track, now = b'', 0
    for tick, data in events:
        track += _vlq(tick - now) + data
        now = tick
    track += b'\x00\xFF\x2F\x00'
    return (b'MThd' + struct.pack('>IHHH', 6, 0, 1, TPB)
            + b'MTrk' + struct.pack('>I', len(track)) + track)


def soundfont():
    for sf in SOUNDFONTS:
        if sf.exists():
            return sf
    raise SystemExit('no SoundFont found; put one at ' + str(SOUNDFONTS[0]))


def render(spec):
    """Stereo float32 audio at SR."""
    with tempfile.TemporaryDirectory() as tmp:
        mid, raw = Path(tmp) / 'track.mid', Path(tmp) / 'track.raw'
        mid.write_bytes(midi(spec, arrange(spec)))
        subprocess.run(['fluidsynth', '-ni', '-q', '-g', '0.5', '-r', str(SR), '-R', '1', '-C', '0',
                        '-T', 'raw', '-O', 'float', '-E', 'little', '-F', str(raw), str(soundfont()), str(mid)],
                       check=True, capture_output=True)
        audio = np.fromfile(raw, dtype='<f4').reshape(-1, 2)
    loud = np.nonzero(np.abs(audio).max(axis=1) > 1e-4)[0]
    audio = audio[:loud[-1] + SR // 2] if len(loud) else audio
    return (audio / np.abs(audio).max() * 0.89).astype(np.float32)    # peaks at -1 dBFS


def out_path(spec):
    return OUT / f'{spec["title"]} - {spec["bpm"]}bpm.mp3'


def encode(audio, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'f32le', '-ar', str(SR), '-ac', '2', '-i', '-',
                    '-c:a', 'libmp3lame', '-q:a', '2', str(path)], input=audio.tobytes(), check=True)


def main(force=False):
    sources = sorted(SOURCES.glob('*.track'))
    if not sources:
        raise SystemExit(f'no .track files in {SOURCES}')
    for src in sources:
        try:
            spec = parse(src.read_text())
        except ValueError as e:
            print(f'{src.name}: {e}')
            continue
        path = out_path(spec)
        if not force and path.exists() and path.stat().st_mtime >= src.stat().st_mtime:
            continue
        encode(render(spec), path)
        print(f'{src.name} -> {str(path).replace(str(Path.home()), "~", 1)}')


if __name__ == '__main__':
    main()
