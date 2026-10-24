"""Run the audio engine and keep it in sync with a resolved rig.

Carla's engine library does the audio, in its own process (carla_engine.py);
we talk to it over a socket. Each plugin is its own JACK client, and wiring is
done with pw-link so port names match what `pw-link -o` / `pw-link -i` show.
"""
import fnmatch
import itertools
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .lv2 import CACHE, lv2_dirs
from .rigfile import Split, settings_of

ATOM_PATH = 'http://lv2plug.in/ns/ext/atom#Path'
ATOM_STRING = 'http://lv2plug.in/ns/ext/atom#String'
CLIENT = 'rig'          # JACK client prefix: plugin ports are named rig.N/<label>:<port>
LOOPER = 'rig-looper'   # the looper's JACK client is rig-looper-<pid>: unique, so wiring by name can't
                        # pick up another one (a second rig, or one left over)


def ours(port):
    return port.startswith((CLIENT + '.', LOOPER + '-'))


class EngineError(Exception):
    pass


class Child:
    """A helper process we talk to in newline-delimited JSON over a socket pair."""

    def __init__(self, log_path):
        self.log_path = log_path
        self.on_event = None        # called (from a reader thread) with each event dict
        self.proc = None
        self.sock = None
        self.ids = itertools.count()
        self.waiting = {}
        self.lock = threading.Lock()

    def _spawn(self, module, *args, env=None):
        ours_, theirs = socket.socketpair()
        root = Path(__file__).resolve().parent.parent
        log = open(self.log_path, 'w')
        self.proc = subprocess.Popen(
            [sys.executable, '-m', module, str(theirs.fileno()), *args],
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=dict(env or os.environ, PYTHONPATH=str(root)),
            pass_fds=[theirs.fileno()], start_new_session=True)
        theirs.close()
        self.sock = ours_
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b''
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                chunk = b''
            if not chunk:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                msg = json.loads(line)
                if 'event' in msg:
                    if self.on_event:
                        self.on_event(msg)
                elif msg.get('id') in self.waiting:
                    slot = self.waiting[msg['id']]
                    slot.append(msg)
                    slot[0].set()
        for slot in list(self.waiting.values()):    # wake anyone still waiting
            slot[0].set()

    def call(self, cmd, *args, timeout=30):
        """Run one engine command and return its result. Raises EngineError."""
        i = next(self.ids)
        slot = self.waiting[i] = [threading.Event()]
        try:
            with self.lock:
                self.sock.sendall(json.dumps({'id': i, 'cmd': cmd, 'args': args}).encode() + b'\n')
            if not slot[0].wait(timeout) or len(slot) < 2:
                raise EngineError('audio engine not responding' if self.alive() else 'audio engine exited')
        except OSError:
            raise EngineError('audio engine exited')
        finally:
            del self.waiting[i]
        if 'error' in slot[1]:
            raise EngineError(slot[1]['error'])
        return slot[1]['result']

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _close(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
                self.sock.close()
            except OSError:
                pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(5)         # closing the socket makes it shut down cleanly
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


class CarlaHost(Child):
    def __init__(self, log_path, preset_dir, latency=64):
        super().__init__(log_path)
        self.preset_dir = Path(preset_dir)
        self.latency = latency

    def _lv2_path(self):
        # presets.lv2 goes on the plugin path so Carla lists our presets as programs;
        # a directory of its own keeps Carla from scanning the rest of the project
        link_dir = CACHE / 'lv2'
        link_dir.mkdir(parents=True, exist_ok=True)
        link = link_dir / 'presets.lv2'
        if not link.is_symlink() or link.resolve() != self.preset_dir.resolve():
            link.unlink(missing_ok=True)
            link.symlink_to(self.preset_dir.resolve())
        return ':'.join([*map(str, lv2_dirs()), str(link_dir)])

    def start(self):
        if not os.path.exists('/usr/lib/carla/libcarla_standalone2.so'):
            raise RuntimeError('Carla is not installed (needs /usr/lib/carla/libcarla_standalone2.so)')
        # ask PipeWire for a small buffer while we run; it uses the smallest any client requests
        env = dict(os.environ, PIPEWIRE_LATENCY=f'{self.latency}/48000')
        self._spawn('rig_app.carla_engine', CLIENT, self._lv2_path(), env=env)
        try:
            self.call('cpu', timeout=20)        # answered once the engine is up
        except EngineError as e:
            self.stop()
            raise RuntimeError(f'audio engine failed to start ({e}), see {self.log_path}')

    def stop(self):
        self._close()
        # PipeWire lists the old ports for a moment after exit; a restart that
        # links by those names before they're gone would hit the dead ports
        for _ in range(30):
            outs, ins = list_ports()
            if not any(p.startswith(CLIENT + '.') for p in outs + ins):
                break
            time.sleep(0.1)


@dataclass
class Live:
    name: str
    uri: str
    preset: str | None
    bypass: bool = False
    volume: float = 1.0                           # Carla's per-plugin gain; master volume uses it
    base: dict = field(default_factory=dict)      # values to fall back to when a line is removed
    applied: dict = field(default_factory=dict)


def list_ports():
    def run(flag):
        try:
            out = subprocess.run(['pw-link', flag], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [x.strip() for x in out.splitlines() if x.strip()]
    return run('-o'), run('-i')


def match_port(pattern, ports):
    pat = pattern if any(c in pattern for c in '*?[') else f'*{pattern}*'
    hits = [p for p in ports if fnmatch.fnmatchcase(p, pat) or fnmatch.fnmatchcase(p, f'*{pat}')]
    exact = [p for p in hits if p == pattern]
    return exact or hits


def _pairs(srcs, dsts):
    """How to connect n outputs to m inputs: pairwise, mono→both, or both→mono."""
    if not srcs or not dsts:
        return []
    if len(srcs) == len(dsts):
        return list(zip(srcs, dsts))
    if len(srcs) == 1:
        return [(srcs[0], d) for d in dsts]
    if len(dsts) == 1:
        return [(s, dsts[0]) for s in srcs]
    return list(zip(srcs, dsts))


def preset_label(preset):
    """The name Carla lists a preset under: our files are labelled with their file name."""
    tail = re.split(r'[/#]', preset.rstrip('/'))[-1]
    return tail.removesuffix('.ttl')


class Engine:
    def __init__(self, host, preset_dir):
        self.host = host
        self.preset_dir = Path(preset_dir)
        self.live = {}
        self.links = set()
        self.lock = threading.Lock()
        self.stale_presets = False    # a preset was saved that the engine can't list until restarted
        self.start = []               # hardware ports the chain starts from
        self.first = []               # plugins fed by the hardware input
        self.last = []                # plugins feeding the hardware output
        self.tuner = None             # [links] while the tuner is listening
        self.looper = None            # (in ports, out ports) of the looper once it runs
        self.volume_db = 0.0          # master volume, dB (None = silent)
        self.muted = False

    # -------------------------------------------------------------- helpers

    def _call(self, problems, what, cmd, *args):
        try:
            return self.host.call(cmd, *args)
        except EngineError as e:
            problems.append(f'{what}: {e}')
            return None

    def _set(self, live, s, v, problems, label):
        what = f'[{label}] {s.key}'
        if s.kind == 'port':
            self._call(problems, what, 'set', live.name, s.key, v)
        elif isinstance(v, str):
            self._call(problems, what, 'set_path', live.name, s.uri, v, s.path)
        else:
            self._call(problems, what, 'set', live.name, s.uri, v)
        live.applied[s.key] = v

    def _read_base(self, live, plugin, problems):
        """Current values (after a preset load, these are what the preset set)."""
        values = self._call(problems, f'[{live.name}] read', 'values', live.name) or {}
        live.base = {}
        for p in plugin.controls:
            live.base[p.symbol] = values.get(p.symbol, p.default)
        for p in plugin.props:
            v = values.get(p.uri, p.default)
            if v is not None:
                live.base[p.key] = v
        live.applied = dict(live.base)

    # -------------------------------------------------------------- apply

    def apply(self, rig, resolved, structural):
        """Bring the engine in line with the rig. Returns (problems, pending_structural)."""
        chain, ins, outs = rig.chain, rig.ins, rig.outs
        with self.lock:
            problems = []
            want = {r.label: r for r in resolved if r.uri}
            changed = (set(self.live) != set(want)
                       or any(self.live[k].uri != r.uri or self.live[k].preset != r.preset
                              for k, r in want.items() if k in self.live))
            if structural:
                for label in [k for k in self.live if k not in want
                              or self.live[k].uri != want[k].uri
                              or self.live[k].preset != want[k].preset]:
                    name = self.live[label].name
                    self._call(problems, f'remove [{label}]', 'remove', name)
                    self.links = {l for l in self.links if not any(f'/{name}:' in p for p in l)}
                    del self.live[label]
                for label, r in want.items():
                    if label not in self.live:
                        self._add(r, problems)
            for label, r in want.items():
                live = self.live.get(label)
                if not live or live.uri != r.uri:
                    continue
                settings = settings_of(r.plugin)
                for key, s in settings.items():
                    v = r.values.get(key, live.base.get(key))
                    if v is None:
                        continue
                    old = live.applied.get(key)
                    if old is None or (v != old if isinstance(v, str) or isinstance(old, str)
                                       else abs(v - old) > 1e-9 * max(1, abs(v))):
                        self._set(live, s, v, problems, label)
                if live.bypass != r.bypass:
                    # a bypassed plugin is switched off and wired around
                    self._call(problems, f'bypass [{label}]', 'active', live.name, not r.bypass)
                    live.bypass = r.bypass
                    changed = True
            if structural:
                problems += self._wire(resolved, chain, ins, outs)
            self._volume(rig.volume, problems)
            return problems, (changed and not structural)

    def _volume(self, db, problems):
        """Master volume: the gain of whatever plugins feed the output."""
        self.volume_db = db
        gain = 0.0 if db is None or self.muted else 10 ** (db / 20)
        for label, live in self.live.items():
            want = gain if label in self.last else 1.0
            if abs(live.volume - want) > 1e-6:
                self._call(problems, 'volume', 'volume', live.name, want)
                live.volume = want

    def audition(self, label, plugin, values):
        """Set values on a running plugin without touching the rig file."""
        with self.lock:
            live = self.live.get(label)
            if not live:
                return ['not loaded']
            problems, settings = [], settings_of(plugin)
            for key, v in values.items():
                self._set(live, settings[key], v, problems, label)
            return problems

    def set_mute(self, on):
        with self.lock:
            self.muted = on
            self._volume(self.volume_db, [])

    def meters(self):
        """{label: (input peak, output peak)} since the last call, 0..1."""
        names = {live.name: label for label, live in self.live.items()}
        return {names[n]: tuple(v) for n, v in self.host.call('meters', timeout=5).items() if n in names}

    # -------------------------------------------------------------- tuner

    TUNER = 'http://guitarix.sourceforge.net/plugins/gxtuner#tuner'

    def tuner_on(self):
        """Listen to the guitar input with a tuner that isn't part of the chain."""
        with self.lock:
            if self.tuner is not None:
                return
            self.host.call('add', '_tuner', self.TUNER)
            ins_ = []
            for _ in range(30):
                ins_ = [p for p in list_ports()[1] if p.endswith('/_tuner:In')]
                if ins_:
                    break
                time.sleep(0.1)
            links = [(a, ins_[0]) for a in self.start[:1]] if ins_ else []
            for a, b in links:
                subprocess.run(['pw-link', a, b], capture_output=True)
            self.tuner = links

    def tuner_freq(self):
        return self.host.call('output', '_tuner', 'FREQ', timeout=5)

    def tuner_off(self):
        with self.lock:
            if self.tuner is None:
                return
            self.tuner = None
            try:
                self.host.call('remove', '_tuner')
            except EngineError:
                pass

    def _add(self, r, problems):
        if self._call(problems, f'load [{r.label}]', 'add', r.label, r.uri) is None:
            return
        live = Live(r.label, r.uri, r.preset)
        if r.preset:
            try:
                self.host.call('preset', live.name, preset_label(r.preset))
            except EngineError as e:
                if 'unknown preset' in str(e) and r.preset.startswith('file://'):
                    self.stale_presets = True      # saved after the engine started
                problems.append(f'[{r.label}] preset: {e}')
        self._read_base(live, r.plugin, problems)
        self.live[r.label] = live

    def _ports(self, name, direction, wanted):
        """This plugin's audio ports, matched by LV2 port name. Ports of a plugin that
        was just added can take a moment to show up in PipeWire, so look again for a while."""
        mine = []
        for _ in range(30):
            outs, ins = list_ports()
            mine = [p for p in (ins if direction == 'in' else outs) if ours(p) and f'/{name}:' in p]
            by_name = {p.split(':', 1)[1]: p for p in mine}
            found = [by_name.get(w.name) for w in wanted]
            if all(found):
                return found
            time.sleep(0.1)
        return [p for p in mine if ':events-' not in p][:len(wanted)]

    def _wire(self, resolved, chain, ins, outs):
        problems = []
        hw_out, hw_in = list_ports()
        start = []
        for pat, _ in ins:
            hits = match_port(pat, [p for p in hw_out if not ours(p)])
            if len(hits) == 1:
                start.append(hits[0])
            else:
                problems.append(f"in: '{pat}' " + ('matches nothing' if not hits
                                                   else f'matches {len(hits)} ports'))
        by_label = {r.label: r for r in resolved}
        want = set()
        owner = {}                    # id(port list) -> label that produces it
        self.start, self.first = start, []

        def connect(groups, dsts):
            for g in groups:
                want.update(_pairs(g, dsts))

        def run(items, groups):
            """Wire a path; groups are the port sets feeding it (several after a join,
            which JACK mixes). Returns the port sets coming out of it."""
            for item in items:
                if isinstance(item, Split):
                    groups = [g for path in item.paths for g in run(path, groups)]
                    continue
                r, live = by_label.get(item), self.live.get(item)
                if not r or not live or live.uri != r.uri or live.bypass:
                    continue
                if any(g is start for g in groups):
                    self.first.append(item)
                connect(groups, self._ports(live.name, 'in', r.plugin.audio_ins))
                groups = [self._ports(live.name, 'out', r.plugin.audio_outs)]
                owner[id(groups[0])] = item
            return groups

        prev = run(chain, [start])
        self.last = [owner[id(g)] for g in prev if id(g) in owner]
        final = []
        for pat, _ in outs:
            hits = match_port(pat, [p for p in hw_in if not ours(p)])
            if len(hits) == 1:
                final.append(hits[0])
            else:
                problems.append(f"out: '{pat}' " + ('matches nothing' if not hits
                                                    else f'matches {len(hits)} ports'))
        connect(prev, final)
        if self.looper:
            # the looper records what goes to the speakers and plays into them too
            loop_ins, loop_outs = self.looper
            connect(prev, loop_ins)
            want.update(_pairs(loop_outs, final))
        for a, b in self.links - want:
            subprocess.run(['pw-link', '-d', a, b], capture_output=True)
        for a, b in want - self.links:
            res = subprocess.run(['pw-link', a, b], capture_output=True, text=True)
            if res.returncode and 'exists' not in res.stderr.lower():
                problems.append(f"can't link {a} → {b}: {res.stderr.strip()}")
        self.links = want
        return problems

    # -------------------------------------------------------------- presets

    def save_preset(self, label, name, plugin):
        """Capture a plugin's full internal state into presets.lv2/<name>.ttl.
        Returns the plugin's current values, keyed like the rig file."""
        with self.lock:
            live = self.live[label]
            data = self.host.call('state', live.name)
            controls = {p.symbol for p in plugin.controls}
            ports = {k: v for k, v in data['values'].items() if k in controls}
            write_preset(self.preset_dir / f'{name}.ttl', name, live.uri, ports, data['state'])
            write_manifest(self.preset_dir)
            self.stale_presets = True
            live.preset = f'file://{self.preset_dir}/{name}.ttl'
            return keyed(plugin, data['values'])

    def values(self, label, plugin):
        """A plugin's current values in the engine, keyed like the rig file."""
        with self.lock:
            return keyed(plugin, self.host.call('values', self.live[label].name))


def keyed(plugin, values):
    """Engine values {symbol or property URI: value} -> {setting key: value}."""
    uris = {p.uri: p.key for p in plugin.props}
    return {uris.get(k, k): v for k, v in values.items()}


def _literal(v):
    return '"' + v.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def write_preset(path, name, uri, ports, state_data):
    """An LV2 preset: control port values plus the plugin's saved state."""
    ports = [f'        lv2:symbol {_literal(k)} ;\n        pset:value {v!r}'
             for k, v in ports.items()]
    state = []
    for type_, key, value in state_data:
        if type_ == ATOM_STRING:
            state.append(f'        <{key}> {_literal(value)}')
        elif type_ == ATOM_PATH:
            state.append(f'        <{key}> {_literal(value)}^^atom:Path')
        # other kinds of state are binary atoms Carla hands over as base64; they can't
        # be written back faithfully, and the port values above cover those plugins
    lines = ['@prefix atom: <http://lv2plug.in/ns/ext/atom#> .',
             '@prefix lv2: <http://lv2plug.in/ns/lv2core#> .',
             '@prefix pset: <http://lv2plug.in/ns/ext/presets#> .',
             '@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .',
             '@prefix state: <http://lv2plug.in/ns/ext/state#> .', '',
             f'<{path.name}> a pset:Preset ;',
             f'    lv2:appliesTo <{uri}> ;',
             f'    rdfs:label {_literal(name)}']
    if ports:
        lines[-1] += ' ;'
        lines.append('    lv2:port [\n' + ('\n    ] , [\n'.join(ports)) + '\n    ]')
    if state:
        lines[-1] += ' ;'
        lines.append('    state:state [\n' + (' ;\n'.join(state)) + '\n    ]')
    lines[-1] += ' .'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n')


def write_manifest(preset_dir):
    lines = ['@prefix lv2: <http://lv2plug.in/ns/lv2core#> .',
             '@prefix pset: <http://lv2plug.in/ns/ext/presets#> .',
             '@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .', '']
    for f in sorted(Path(preset_dir).glob('*.ttl')):
        if f.name == 'manifest.ttl':
            continue
        m = re.search(r'lv2:appliesTo\s+<([^>]+)>', f.read_text(errors='ignore')[:4096])
        if m:
            lines += [f'<{f.name}> a pset:Preset ;', f'    lv2:appliesTo <{m.group(1)}> ;',
                      f'    rdfs:seeAlso <{f.name}> .', '']
    (Path(preset_dir) / 'manifest.ttl').write_text('\n'.join(lines))
