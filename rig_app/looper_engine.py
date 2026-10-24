"""The looper process: a JACK client that records what the rig plays and loops it.

    python -m rig_app.looper_engine <fd> <client-name>

Speaks the same newline-delimited JSON as carla_engine.py over the socket <fd>, and
runs in its own process for the same reason: nothing in the TUI can hold up the
audio callback.

A loop is a set number of bars at a set tempo, so no footswitch is needed: `record`
clicks one bar in, records the bars, and goes straight into playing them back.
Overdubs are layers on top of the first take; undo stops playing the newest one.

It also runs a timing trainer (a click, a silent gap, and how far off your entry is),
records sessions of any length (what you hear, and the guitar alone), runs a metronome for practice, separate from any loop, and plays a backing track: any audio file, decoded by ffmpeg when loaded.
The track goes straight to the outputs, so a loop never records it.

Only the audio callback changes the loop. Commands are queued and the callback
picks them up at the start of its next buffer, so it never sees half a change.
"""
import collections
import gc
import json
import selectors
import socket
import subprocess
import sys
import threading
import time
import wave

import cffi
import jack
import numpy as np

CLICK_MS = 25
CLICK_LEVEL = 0.3            # about -10 dBFS
MAX_FRAMES = 8192            # the biggest buffer we expect; index arrays are cut from this

_ffi = cffi.FFI()
_ffi.cdef('typedef struct { uint32_t min; uint32_t max; } jack_latency_range_t;'
          'void jack_port_get_latency_range(void *port, int mode, jack_latency_range_t *range);')
_libjack = _ffi.dlopen('libjack.so.0')
CAPTURE, PLAYBACK = 0, 1     # jack_latency_callback_mode_t


def port_latency(port, mode):
    """The most latency JACK reports between this port and the hardware, in frames."""
    r = _ffi.new('jack_latency_range_t *')
    ptr = _ffi.cast('void *', int(jack._ffi.cast('uintptr_t', port._ptr)))
    _libjack.jack_port_get_latency_range(ptr, mode, r)
    return r.max


def pcm24(x):
    """float frames -> 24-bit little-endian WAV data."""
    x = np.clip(x, -1.0, 1.0)
    return (x * 8388607).astype('<i4').view(np.uint8).reshape(-1, 4)[:, :3].tobytes()


class Trainer:
    """Internal-clock practice: the click plays for some bars, then stops for `gap`
    bars, and you come in on the first beat after the gap (the click comes back
    there too). The audio thread finds your first note near that beat and keeps
    how early or late it was.

    Times are compared the way the looper records: a note that arrives now was
    played a round trip after the click it answers went out."""
    LISTEN = 0.5                     # bars either side of the target to look for your entry
    QUIET = 0.05                     # louder than this just before the window: you were playing
    FLOOR = 0.02                     # the least a note has to reach to count

    def __init__(self, t, beat, beats, click_bars, gap_bars, rtl):
        self.beat, self.beats = beat, beats
        self.bar = beat * beats
        self.click_bars, self.cycle = click_bars, (click_bars + gap_bars) * self.bar
        self.start, self.rtl = t, rtl
        self.results = []            # [round, offset in frames or None, played during the gap]
        self.round = 1               # the entry we're waiting for: at round * cycle
        self.noise = 0.0             # loudest input just before the window opens

    def clicks(self, rel):
        """Where the click sounds: the first click_bars of every cycle."""
        return rel % self.cycle < self.click_bars * self.bar

    def listen(self, rel, inp):
        """Audio thread: rel is each frame's position (as played), inp the mono input."""
        rec = rel - self.rtl
        target = self.round * self.cycle
        win = round(self.LISTEN * self.bar)
        before = (rec >= target - win - self.beat // 2) & (rec < target - win)
        if before.any():
            self.noise = max(self.noise, float(np.abs(inp[before]).max()))
        inside = (rec >= target - win) & (rec < target + win)
        if inside.any():
            threshold = max(self.FLOOR, 4 * self.noise)
            hits = np.nonzero(inside & (np.abs(inp) > threshold))[0]
            if len(hits):
                self._result(int(rec[hits[0]] - target))
                return
        if rec[-1] >= target + win:
            self._result(None)

    def _result(self, offset):
        self.results.append([self.round, offset, self.noise > self.QUIET])
        self.round += 1
        self.noise = 0.0


class Recorder:
    """Streams a session to disk as two WAVs: the mix you hear and the guitar alone.
    The audio callback only queues buffers; a thread converts and writes them.

    The guitar comes in a round trip after you heard what you played along to, so
    it's moved that much earlier to line up with the backing in the mix."""

    def __init__(self, base, sr, rtl):
        self.paths = [f'{base} - mix.wav', f'{base} - guitar.wav']
        self.files = []
        for path in self.paths:
            w = wave.open(path, 'wb')
            w.setnchannels(2)
            w.setsampwidth(3)
            w.setframerate(sr)
            self.files.append(w)
        self.sr = sr
        self.skip = rtl                  # guitar frames from before the first played frame was heard
        self.played = np.zeros((0, 2), dtype=np.float32)
        self.queue = collections.deque()
        self.frames = 0
        self.error = None
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def feed(self, guitar, played):
        """Audio thread: hand over one buffer of each."""
        self.queue.append((guitar.copy(), played.copy()))

    def _run(self):
        while self.running or self.queue:
            if not self.queue:
                time.sleep(0.05)
                continue
            guitar, played = self.queue.popleft()
            try:
                self._write(guitar, played)
            except OSError as e:
                self.error, self.running = str(e), False
                self.queue.clear()

    def _write(self, guitar, played):
        self.played = np.concatenate([self.played, played])
        if self.skip:
            cut = min(self.skip, len(guitar))
            guitar, self.skip = guitar[cut:], self.skip - cut
        n = len(guitar)
        mix, self.played = guitar + self.played[:n], self.played[n:]
        self.files[0].writeframes(pcm24(mix))
        self.files[1].writeframes(pcm24(guitar))
        self.frames += n

    def stop(self):
        self.running = False
        self.thread.join()
        for f in self.files:
            f.close()
        return self.paths, self.frames / self.sr


def click(sr, hz):
    n = int(sr * CLICK_MS / 1000)
    t = np.arange(n) / sr
    return (np.sin(2 * np.pi * hz * t) * np.exp(-t * 180) * CLICK_LEVEL).astype(np.float32)


class Looper:
    def __init__(self, client_name):
        self.client = jack.Client(client_name, no_start_server=True)
        self.sr = self.client.samplerate
        self.ins = [self.client.inports.register(n) for n in ('in_L', 'in_R')]
        self.outs = [self.client.outports.register(n) for n in ('out_L', 'out_R')]
        self.clicks = (click(self.sr, 1000), click(self.sr, 1500))   # beat, first beat of the bar
        self.steps = np.arange(MAX_FRAMES, dtype=np.int64)
        self.queue = collections.deque()
        self.t = 0                   # frames processed since we started: the timeline
        # the loop; only the callback changes these
        self.state = 'empty'         # empty | rec | play | stopped
        self.layers = []             # (length, 2) float32 each: the take, then overdubs
        self.top = 0                 # how many layers play; the rest can be redone
        self.length = 0              # loop length in frames
        self.beat = 0                # beat length in frames
        self.beats = 4               # beats per bar
        self.start = 0               # timeline frame where the loop's first frame goes out
        self.play_from = 0           # loop playback begins this far after start
        self.rtl = 0                 # round trip: played at t, it comes back in at t + rtl
        self.taking = False          # the first take is still coming in
        self.dub = None              # layer being overdubbed
        self.dub_from = 0            # timeline frame the overdub started
        self.metro = None            # (beat, beats per bar, timeline start) while the metronome runs
        self.metro_gain = 1.0
        self.trainer = None          # a Trainer while timing practice runs
        self.recorder = None         # a Recorder while a session is being recorded
        self.track = None            # (frames, 2) float32 backing track
        self.track_name = ''
        self.track_pos = 0
        self.track_on = False
        self.track_gain = 0.5
        self.snap = {}
        self.client.set_process_callback(self._process)
        self.client.activate()
        gc.freeze()                  # keep the collector from walking setup objects mid-buffer

    # ------------------------------------------------------------ audio thread

    def _process(self, frames):
        while self.queue:
            self._do(*self.queue.popleft())
        out = np.zeros((frames, 2), dtype=np.float32)
        clicks = np.zeros((frames, 2), dtype=np.float32)    # kept out of recordings
        rel = self.steps[:frames] + (self.t - self.start)    # position in the loop, per frame
        if self.state in ('rec', 'play'):
            if not self.metro:          # when the metronome runs, it's the count-in and the click
                on = (rel >= -self.beat * self.beats) & (rel < self.length)   # count-in and take
                self._click(clicks, rel, self.beat, self.beats, on)
            played = rel >= self.play_from
            if self.top and played.any():
                pos = rel[played] % self.length
                for layer in self.layers[:self.top]:
                    out[played] += layer[pos]
            self._record(rel - self.rtl, frames)
            if self.state == 'rec' and rel[-1] >= self.length:
                self.state = 'play'
        if self.trainer:
            tr = self.trainer
            rel_t = self.steps[:frames] + (self.t - tr.start)
            self._click(clicks, rel_t, tr.beat, tr.beats, tr.clicks(rel_t), self.metro_gain)
            tr.listen(rel_t, sum(p.get_array() for p in self.ins) / 2)
        if self.metro:
            beat, beats, start = self.metro
            self._click(clicks, self.steps[:frames] + (self.t - start), beat, beats, True, self.metro_gain)
        if self.track_on:
            part = self.track[self.track_pos:self.track_pos + frames]
            out[:len(part)] += part * self.track_gain
            self.track_pos += frames
            if self.track_pos >= len(self.track):        # the end: stop and rewind
                self.track_on, self.track_pos = False, 0
        if self.recorder:
            self.recorder.feed(np.stack([p.get_array() for p in self.ins], axis=1), out)
        out += clicks
        for i, port in enumerate(self.outs):
            port.get_array()[:] = out[:, i]
        self.t += frames
        self._snapshot(rel[-1])

    def _click(self, out, rel, beat, beats, on, gain=1.0):
        """Add a click at each beat, accented on the first of the bar, where `on` allows."""
        phase = rel % beat
        on = on & (phase < len(self.clicks[0]))
        if on.any():
            first = (rel[on] // beat) % beats == 0
            p = phase[on]
            out[on] += gain * np.where(first, self.clicks[1][p], self.clicks[0][p])[:, None]

    def _record(self, rec, frames):
        """rec: the loop position each incoming frame was played against. The take
        keeps coming in for a round trip after playback starts, which is fine: those
        frames are at the end of the loop, and playback doesn't reach them until then."""
        if self.taking:
            take = (rec >= 0) & (rec < self.length)
            if take.any():
                inp = np.stack([p.get_array() for p in self.ins], axis=1)
                self.layers[0][rec[take]] = inp[take]
            if rec[-1] >= self.length - 1:
                self.taking = False
        if self.dub is not None:
            fresh = self.steps[:frames] + self.t >= self.dub_from
            if fresh.any():
                inp = np.stack([p.get_array() for p in self.ins], axis=1)
                self.layers[self.dub][rec[fresh] % self.length] += inp[fresh]

    def _do(self, cmd, *args):
        if cmd == 'record':
            self.length, self.beat, self.beats, layer, self.rtl = args
            self.layers, self.top, self.dub = [layer], 1, None
            bar = self.beat * self.beats
            self.start = self.t + bar                         # after a bar of count-in
            if self.metro and self.metro[:2] == (self.beat, self.beats):
                # on the metronome's next bar line instead, so the two stay together
                self.start += -(self.start - self.metro[2]) % bar
            self.play_from = self.length
            self.state, self.taking = 'rec', True
        elif cmd == 'play':
            self.start, self.play_from, self.state = self.t, 0, 'play'
        elif cmd == 'stop':
            self.dub, self.taking = None, False
            self.state = 'stopped' if self.state == 'play' else 'empty'   # a cut-short take is dropped
        elif cmd == 'dub':
            layer, self.rtl = args
            if layer is None:
                self.dub = None
            elif self.state == 'play':
                self.layers = self.layers[:self.top] + [layer]     # a new layer drops what undo left
                self.dub, self.top = self.top, self.top + 1
                self.dub_from = self.t
        elif cmd == 'undo':
            if self.dub is not None:
                self.dub = None
            self.top = max(1 if self.layers else 0, self.top - 1)
        elif cmd == 'redo':
            self.top = min(len(self.layers), self.top + 1)
        elif cmd == 'clear':
            self.state, self.layers, self.top, self.dub, self.taking = 'empty', [], 0, None, False
        elif cmd == 'trainer':
            self.trainer = Trainer(self.t + args[0], *args[1:]) if args[1] else None
        elif cmd == 'metro':
            self.metro = (args[0], args[1], self.t) if args[0] else None
        elif cmd == 'metro_gain':
            self.metro_gain = args[0]
        elif cmd == 'track':
            self.track, self.track_name = args
            self.track_pos, self.track_on = 0, True
        elif cmd == 'track_play':
            self.track_on = args[0] and self.track is not None
        elif cmd == 'track_seek':
            if self.track is not None:
                self.track_pos = min(max(self.track_pos + args[0], 0), len(self.track) - 1)
        elif cmd == 'track_gain':
            self.track_gain = args[0]

    def _snapshot(self, rel):
        self.snap = {'state': self.state, 'pos': int(rel), 'length': self.length,
                     'beat': self.beat, 'beats': self.beats, 'layers': self.top,
                     'redo': len(self.layers) - self.top, 'dub': self.dub is not None,
                     'rtl': self.rtl, 'sr': self.sr,
                     'metro': self.metro and [self.metro[0], self.metro[1], self.t - self.metro[2]],
                     'trainer': self.trainer and {
                         'pos': self.t - self.trainer.start, 'beat': self.trainer.beat,
                         'beats': self.trainer.beats, 'click_bars': self.trainer.click_bars,
                         'cycle': self.trainer.cycle, 'results': self.trainer.results[-50:]},
                     'track': self.track_name, 'track_on': self.track_on, 'track_pos': self.track_pos,
                     'track_len': 0 if self.track is None else len(self.track),
                     'recording': self.recorder and self.recorder.frames / self.sr,
                     'rec_error': self.recorder and self.recorder.error}

    # ------------------------------------------------------------ commands (main thread)

    def latency(self):
        try:
            return port_latency(self.ins[0], CAPTURE) + port_latency(self.outs[0], PLAYBACK)
        except Exception:
            return 2 * self.client.blocksize

    def cmd_ports(self):
        return [p.name for p in self.ins], [p.name for p in self.outs]

    def cmd_record(self, bpm, bars, beats):
        beat = round(self.sr * 60 / bpm)
        length = beat * beats * bars
        self.queue.append(('record', length, beat, beats,
                           np.zeros((length, 2), dtype=np.float32), self.latency()))

    def cmd_play(self):
        self.queue.append(('play',))

    def cmd_stop(self):
        self.queue.append(('stop',))

    def cmd_dub(self, on):
        layer = np.zeros((self.length, 2), dtype=np.float32) if on and self.length else None
        self.queue.append(('dub', layer, self.latency()))

    def cmd_undo(self):
        self.queue.append(('undo',))

    def cmd_redo(self):
        self.queue.append(('redo',))

    def cmd_clear(self):
        self.queue.append(('clear',))

    def cmd_trainer(self, bpm, beats=4, click_bars=2, gap_bars=2):
        """Start timing practice (stopping the metronome); bpm None stops it."""
        if not bpm:
            self.queue.append(('trainer', 0, None))
            return
        beat = round(self.sr * 60 / bpm)
        self.queue.append(('metro', None, beats))
        self.queue.append(('trainer', self.client.blocksize, beat, beats, click_bars, gap_bars,
                           self.latency()))

    def cmd_metro(self, bpm, beats=4):
        """Start the metronome (or change its tempo); bpm None stops it."""
        self.queue.append(('metro', round(self.sr * 60 / bpm) if bpm else None, beats))

    def cmd_metro_gain(self, db):
        self.queue.append(('metro_gain', 10 ** (db / 20)))

    def cmd_track(self, path):
        """Load a backing track and start it. Returns its length in seconds."""
        res = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-f', 'f32le', '-ac', '2',
                              '-ar', str(self.sr), '-'], capture_output=True)
        if res.returncode or not res.stdout:
            raise RuntimeError(res.stderr.decode(errors='replace').strip().splitlines()[-1]
                               if res.stderr.strip() else "ffmpeg can't read it")
        track = np.frombuffer(res.stdout, dtype=np.float32).reshape(-1, 2)
        self.queue.append(('track', track, path.rsplit('/', 1)[-1]))
        return len(track) / self.sr

    def cmd_track_play(self, on):
        self.queue.append(('track_play', bool(on)))

    def cmd_track_seek(self, seconds):
        self.queue.append(('track_seek', round(seconds * self.sr)))

    def cmd_track_gain(self, db):
        self.queue.append(('track_gain', 10 ** (db / 20)))

    def cmd_status(self):
        return self.snap

    def cmd_save(self, path):
        """Write what's playing to a 24-bit WAV. Returns its length in seconds."""
        layers = self.layers[:self.top]
        if not layers:
            raise LookupError('nothing recorded yet')
        mix = sum(layers)
        with wave.open(path, 'wb') as w:
            w.setnchannels(2)
            w.setsampwidth(3)
            w.setframerate(self.sr)
            w.writeframes(pcm24(mix))
        return len(mix) / self.sr

    def cmd_rec_start(self, base):
        """Start recording to '<base> - mix.wav' and '<base> - guitar.wav'."""
        if self.recorder:
            raise RuntimeError('already recording')
        self.recorder = Recorder(base, self.sr, self.latency())

    def cmd_rec_stop(self):
        """Stop recording. Returns ([mix path, guitar path], seconds)."""
        rec, self.recorder = self.recorder, None
        if not rec:
            raise RuntimeError('not recording')
        return rec.stop()

    def cmd_ping(self):
        return True


def serve(sock, looper):
    buf = b''
    sel = selectors.DefaultSelector()
    sel.register(sock, selectors.EVENT_READ)
    while True:
        sel.select()
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            req = json.loads(line)
            try:
                msg = {'id': req['id'], 'result': getattr(looper, 'cmd_' + req['cmd'])(*req.get('args', []))}
            except Exception as e:
                msg = {'id': req['id'], 'error': str(e) or type(e).__name__}
            sock.sendall(json.dumps(msg).encode() + b'\n')
    if looper.recorder:                 # quitting mid-recording still leaves complete files
        looper.cmd_rec_stop()
    looper.client.deactivate()
    looper.client.close()


def main():
    fd, client_name = int(sys.argv[1]), sys.argv[2]
    serve(socket.socket(fileno=fd), Looper(client_name))


if __name__ == '__main__':
    main()
