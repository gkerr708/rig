"""Find installed LV2 plugins and read their port/parameter descriptions.

Everything parsed from Turtle is cached as JSON in ~/.cache/rig so startup
stays fast after the first run.
"""
import difflib
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rdflib import RDF, RDFS, Graph, Namespace, URIRef

LV2 = Namespace('http://lv2plug.in/ns/lv2core#')
ATOM = Namespace('http://lv2plug.in/ns/ext/atom#')
PP = Namespace('http://lv2plug.in/ns/ext/port-props#')
PATCH = Namespace('http://lv2plug.in/ns/ext/patch#')
UNITS = Namespace('http://lv2plug.in/ns/extensions/units#')
DOAP = Namespace('http://usefulinc.com/ns/doap#')

CACHE = Path('~/.cache/rig').expanduser()
CACHE_VERSION = 1

UNIT_SYMBOLS = {
    'db': 'dB', 'hz': 'Hz', 'khz': 'kHz', 'mhz': 'MHz', 'ms': 'ms', 's': 's',
    'min': 'min', 'pc': '%', 'bpm': 'BPM', 'semitone12TET': 'st', 'cent': 'ct',
    'm': 'm', 'cm': 'cm', 'mm': 'mm', 'km': 'km', 'degree': '°', 'oct': 'oct',
    'beat': 'beat', 'bar': 'bar', 'frame': 'frames', 'coef': '', 'midiNote': 'note',
}


@dataclass
class Port:
    index: int
    symbol: str
    name: str
    kind: str              # audio | control | cv | atom | other
    output: bool
    minimum: float | None = None
    maximum: float | None = None
    default: float | None = None
    integer: bool = False
    toggled: bool = False
    log: bool = False
    sidechain: bool = False
    hidden: bool = False   # designated ports (enabled, freewheel...) the host drives itself
    unit: str = ''
    gain: bool = False     # linear gain coefficient; shown and written as dB
    enum: list = field(default_factory=list)   # [(label, value)]


@dataclass
class Prop:
    """A patch:writable parameter: file paths (models, IRs) or plain values set by message."""
    uri: str
    key: str
    label: str
    range: str
    minimum: float | None = None
    maximum: float | None = None
    default: float | None = None
    enum: list = field(default_factory=list)

    @property
    def is_path(self):
        return self.range.endswith('#Path')

    @property
    def toggled(self):
        return self.range.endswith('#Bool')

    @property
    def integer(self):
        return self.range.endswith(('#Int', '#Long'))


@dataclass
class Plugin:
    uri: str
    name: str
    bundle: str
    ports: list
    props: list

    @property
    def audio_ins(self):
        return [p for p in self.ports if p.kind == 'audio' and not p.output and not p.sidechain]

    @property
    def audio_outs(self):
        return [p for p in self.ports if p.kind == 'audio' and p.output and not p.sidechain]

    @property
    def controls(self):
        return [p for p in self.ports if p.kind == 'control' and not p.output and not p.hidden]

    def control(self, symbol):
        return next((p for p in self.controls if p.symbol == symbol), None)

    def prop(self, key):
        key = key.lower()
        return next((p for p in self.props if p.key.lower() == key), None)


def lv2_dirs():
    env = os.environ.get('LV2_PATH')
    dirs = env.split(':') if env else ['~/.lv2', '/usr/local/lib/lv2', '/usr/lib/lv2']
    return [d for d in (Path(x).expanduser() for x in dirs) if d.is_dir()]


def _short(uri):
    """Last meaningful path segment of a URI, e.g. .../lv2/compressor_stereo -> compressor_stereo."""
    path = uri.split('#')[0].rstrip('/')
    return path.rsplit('/', 1)[-1].lower()


def _signature(files):
    return {str(f): os.path.getmtime(f) for f in files if os.path.exists(f)}


class Index:
    """All installed plugins: URI -> bundle + the .ttl files describing it."""

    def __init__(self):
        self.plugins = {}
        self._meta = {}
        self._load()
        # exact names win; the tidier fallbacks (no trailing _, the part after #) only
        # apply when nothing matches exactly, so they can't make existing names ambiguous
        self.aliases, self.fallbacks = {}, {}
        for uri, info in self.plugins.items():
            keys = {uri.lower(), _short(uri)}
            if info['count'] == 1:
                keys.add(Path(info['bundle']).name.removesuffix('.lv2').lower())
            for k in keys:
                self.aliases.setdefault(k, set()).add(uri)
            extra = {_short(uri).strip('_')}
            if '#' in uri:
                extra.add(uri.rsplit('#', 1)[1].strip('_').lower())
            for k in extra - keys:
                self.fallbacks.setdefault(k, set()).add(uri)

    def _load(self):
        CACHE.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE / 'index.json'
        try:
            cache = json.loads(cache_file.read_text())
            if cache.get('version') != CACHE_VERSION:
                cache = {}
        except (OSError, ValueError):
            cache = {}
        bundles = cache.get('bundles', {})
        fresh = {}
        for d in lv2_dirs():
            for bundle in sorted(d.glob('*.lv2')):
                manifest = bundle / 'manifest.ttl'
                if not manifest.exists():
                    continue
                key = str(bundle)
                mtime = manifest.stat().st_mtime
                if key in bundles and bundles[key]['mtime'] == mtime:
                    fresh[key] = bundles[key]
                    continue
                fresh[key] = {'mtime': mtime, 'plugins': self._scan_manifest(manifest)}
        cache_file.write_text(json.dumps({'version': CACHE_VERSION, 'bundles': fresh}))
        for key, b in fresh.items():
            for uri, files in b['plugins'].items():
                # first directory in LV2_PATH order wins, like lilv
                self.plugins.setdefault(uri, {'bundle': key, 'files': files,
                                              'count': len(b['plugins'])})

    @staticmethod
    def _scan_manifest(manifest):
        g = Graph()
        try:
            g.parse(manifest, format='turtle')
        except Exception:
            return {}
        out = {}
        for s in g.subjects(RDF.type, LV2.Plugin):
            files = [str(manifest)]
            for f in g.objects(s, RDFS.seeAlso):
                p = str(f).removeprefix('file://')
                if p.endswith('.ttl') and p not in files:
                    files.append(p)
            out[str(s)] = files
        return out

    def lookup(self, ref):
        """Resolve what the user typed to a plugin URI. Returns (uri, error)."""
        ref = ref.strip('<>')
        hits = self.aliases.get(ref.lower()) or self.fallbacks.get(ref.lower(), set())
        if len(hits) == 1:
            return next(iter(hits)), None
        if hits:
            return None, f"'{ref}' matches several plugins: " + ', '.join(sorted(hits))
        close = difflib.get_close_matches(ref.lower(), [*self.aliases, *self.fallbacks], n=3, cutoff=0.6)
        hint = f" (did you mean {', '.join(close)}?)" if close else ''
        return None, f"no installed plugin called '{ref}'{hint}"

    def plugin(self, uri):
        if uri in self._meta:
            return self._meta[uri]
        info = self.plugins[uri]
        sig = _signature(info['files'])
        cache_file = CACHE / 'meta' / (hashlib.sha1(uri.encode()).hexdigest() + '.json')
        try:
            data = json.loads(cache_file.read_text())
            if data['sig'] != sig or data.get('version') != CACHE_VERSION:
                raise ValueError
            plug = _from_json(data['plugin'])
        except (OSError, ValueError, KeyError):
            plug = _parse_plugin(uri, info)
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({'version': CACHE_VERSION, 'sig': sig,
                                              'plugin': asdict(plug)}))
        self._meta[uri] = plug
        return plug


def _from_json(d):
    d['ports'] = [Port(**p) for p in d['ports']]
    d['props'] = [Prop(**p) for p in d['props']]
    return Plugin(**d)


def _num(lit):
    try:
        return float(lit) if lit is not None else None
    except (TypeError, ValueError):
        return None


def _parse_plugin(uri, info):
    g = Graph()
    for f in info['files']:
        g.parse(f, format='turtle')
    s = URIRef(uri)
    ports = []
    for node in g.objects(s, LV2.port):
        types = set(g.objects(node, RDF.type))
        props = set(g.objects(node, LV2.portProperty))
        if LV2.AudioPort in types:
            kind = 'audio'
        elif LV2.ControlPort in types:
            kind = 'control'
        elif LV2.CVPort in types:
            kind = 'cv'
        elif ATOM.AtomPort in types:
            kind = 'atom'
        else:
            kind = 'other'
        unit, gain = '', False
        u = g.value(node, UNITS.unit)
        if isinstance(u, URIRef):
            unit = UNIT_SYMBOLS.get(str(u).rsplit('#', 1)[-1], '')
        elif u is not None:
            unit = str(g.value(u, UNITS.symbol) or g.value(u, RDFS.label) or '')
            gain = str(g.value(u, RDFS.label) or '').lower() == 'gain' or unit == 'G'
            if gain:
                unit = ''
        enum = sorted(((str(g.value(sp, RDFS.label)), _num(g.value(sp, RDF.value)))
                       for sp in g.objects(node, LV2.scalePoint)), key=lambda e: e[1])
        ports.append(Port(
            index=int(g.value(node, LV2['index'])),
            symbol=str(g.value(node, LV2.symbol)),
            name=str(g.value(node, LV2.name) or g.value(node, LV2.symbol)),
            kind=kind,
            output=LV2.OutputPort in types,
            minimum=_num(g.value(node, LV2.minimum)),
            maximum=_num(g.value(node, LV2.maximum)),
            default=_num(g.value(node, LV2.default)),
            integer=LV2.integer in props,
            toggled=LV2.toggled in props,
            log=PP.logarithmic in props,
            sidechain=LV2.isSideChain in props,
            hidden=g.value(node, LV2.designation) is not None,
            unit=unit,
            gain=gain,
            enum=enum if LV2.enumeration in props else [],
        ))
    ports.sort(key=lambda p: p.index)
    writable = []
    for o in g.objects(s, PATCH.writable):
        o_uri = str(o)
        key = o_uri.rsplit('#', 1)[-1] if '#' in o_uri else o_uri.rsplit('/', 1)[-1]
        key = key.rsplit(':', 1)[-1]
        enum = sorted(((str(g.value(sp, RDFS.label)), _num(g.value(sp, RDF.value)))
                       for sp in g.objects(o, LV2.scalePoint)), key=lambda e: e[1])
        writable.append(Prop(uri=o_uri, key=key,
                             label=str(g.value(o, RDFS.label) or key),
                             range=str(g.value(o, RDFS.range) or ''),
                             minimum=_num(g.value(o, LV2.minimum)),
                             maximum=_num(g.value(o, LV2.maximum)),
                             default=_num(g.value(o, LV2.default)),
                             enum=enum))
    name = str(g.value(s, DOAP.name) or _short(uri))
    return Plugin(uri=uri, name=name, bundle=info['bundle'], ports=ports, props=writable)
