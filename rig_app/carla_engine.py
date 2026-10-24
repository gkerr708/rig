"""The audio engine process: Carla's engine library with no Carla window.

    python -m rig_app.carla_engine <fd> <client-name> <lv2-path>

Speaks newline-delimited JSON over the socket <fd>. Requests are
{"id": n, "cmd": name, "args": [...]} and get {"id": n, "result": ...} or
{"id": n, "error": "..."}. Events (a plugin window closing, a knob turned in
one) arrive unasked as {"event": name, ...}.

Every plugin runs as its own JACK client, so the TUI wires them with pw-link
like any other port. This process's main loop also keeps plugin windows alive.
"""
import json
import selectors
import socket
import sys
import tempfile
import threading
import time
from xml.sax.saxutils import escape

sys.path.insert(0, '/usr/share/carla')
from carla_backend import (BINARY_NATIVE, ENGINE_CALLBACK_PARAMETER_VALUE_CHANGED,  # noqa: E402
                           ENGINE_CALLBACK_UI_STATE_CHANGED, ENGINE_OPTION_CLIENT_NAME_PREFIX,
                           ENGINE_OPTION_PATH_BINARIES, ENGINE_OPTION_PATH_RESOURCES,
                           ENGINE_OPTION_PLUGIN_PATH, ENGINE_OPTION_PREFER_UI_BRIDGES,
                           ENGINE_OPTION_PROCESS_MODE, ENGINE_OPTION_TRANSPORT_MODE,
                           ENGINE_PROCESS_MODE_MULTIPLE_CLIENTS, ENGINE_TRANSPORT_MODE_JACK,
                           PARAMETER_INPUT, PLUGIN_HAS_CUSTOM_UI, PLUGIN_LV2, PLUGIN_OPTIONS_NULL,
                           CarlaHostDLL)

LIB = '/usr/lib/carla/libcarla_standalone2.so'
ATOM_PATH = 'http://lv2plug.in/ns/ext/atom#Path'
ATOM_STRING = 'http://lv2plug.in/ns/ext/atom#String'


class Server:
    def __init__(self, sock, client_name, lv2_path):
        self.sock = sock
        self.buf = b''
        self.send_lock = threading.Lock()
        self.names = []          # plugin names, in Carla's id order
        self.params = {}         # name -> {symbol: parameter id} for writable parameters
        self.active = {}         # name -> False while bypassed
        self.peaks = {}          # name -> [input, output]: highest since last read
        self.plugins_lock = threading.Lock()    # plugin ids shift when one is removed
        h = self.h = CarlaHostDLL(LIB, False)
        h.set_engine_callback(self._callback)
        h.set_engine_option(ENGINE_OPTION_PROCESS_MODE, ENGINE_PROCESS_MODE_MULTIPLE_CLIENTS, '')
        h.set_engine_option(ENGINE_OPTION_TRANSPORT_MODE, ENGINE_TRANSPORT_MODE_JACK, '')
        h.set_engine_option(ENGINE_OPTION_PATH_BINARIES, 0, '/usr/lib/carla')
        h.set_engine_option(ENGINE_OPTION_PATH_RESOURCES, 0, '/usr/share/carla/resources')
        h.set_engine_option(ENGINE_OPTION_PREFER_UI_BRIDGES, 0, '')
        h.set_engine_option(ENGINE_OPTION_CLIENT_NAME_PREFIX, 0, client_name)
        h.set_engine_option(ENGINE_OPTION_PLUGIN_PATH, PLUGIN_LV2, lv2_path)
        if not h.engine_init('JACK', client_name):
            raise SystemExit(f'engine failed to start: {h.get_last_error()}')
        threading.Thread(target=self._sample_peaks, daemon=True).start()

    def _sample_peaks(self):
        """Carla only keeps the peak of the latest buffer, so look often and keep the
        highest until someone reads it; otherwise short peaks would never show."""
        h = self.h
        while True:
            with self.plugins_lock:
                for i, name in enumerate(self.names):
                    p = self.peaks.setdefault(name, [0.0, 0.0])
                    p[0] = max(p[0], h.get_input_peak_value(i, True), h.get_input_peak_value(i, False))
                    p[1] = max(p[1], h.get_output_peak_value(i, True), h.get_output_peak_value(i, False))
            time.sleep(0.003)

    # ------------------------------------------------------------ plumbing

    def send(self, msg):
        with self.send_lock:     # Carla may call back from its own threads
            self.sock.sendall(json.dumps(msg).encode() + b'\n')

    def _callback(self, handle, action, plugin_id, value1, value2, value3, valuef, value_str):
        if not 0 <= plugin_id < len(self.names):
            return
        name = self.names[plugin_id]
        if action == ENGINE_CALLBACK_UI_STATE_CHANGED and value1 != 1:
            self.send({'event': 'ui_closed', 'plugin': name, 'crashed': value1 < 0})
        elif action == ENGINE_CALLBACK_PARAMETER_VALUE_CHANGED and value1 >= 0:
            symbol = next((s for s, i in self.params.get(name, {}).items() if i == value1), None)
            if symbol:
                self.send({'event': 'param', 'plugin': name, 'symbol': symbol, 'value': valuef})

    def run(self):
        sel = selectors.DefaultSelector()
        sel.register(self.sock, selectors.EVENT_READ)
        while True:
            if sel.select(timeout=0.03):
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self.buf += chunk
                while b'\n' in self.buf:
                    line, self.buf = self.buf.split(b'\n', 1)
                    self._handle(json.loads(line))
            self.h.engine_idle()
        self.h.set_engine_about_to_close()
        self.h.remove_all_plugins()
        self.h.engine_close()

    def _handle(self, req):
        try:
            result = getattr(self, 'cmd_' + req['cmd'])(*req.get('args', []))
            self.send({'id': req['id'], 'result': result})
        except Exception as e:
            self.send({'id': req['id'], 'error': str(e) or type(e).__name__})

    def _id(self, name):
        if name not in self.names:
            raise ValueError(f'no plugin {name}')
        return self.names.index(name)

    # ------------------------------------------------------------ commands

    def cmd_add(self, name, uri):
        with self.plugins_lock:
            return self._add(name, uri)

    def _add(self, name, uri):
        if not self.h.add_plugin(BINARY_NATIVE, PLUGIN_LV2, '', name, uri, 0, None, PLUGIN_OPTIONS_NULL):
            raise RuntimeError(self.h.get_last_error() or "plugin can't be loaded")
        i = self.h.get_current_plugin_count() - 1
        self.names.append(name)
        actual = self.h.get_plugin_info(i)['name']
        if actual != name:          # Carla made it unique; keep ours as the handle
            self.h.rename_plugin(i, name)
        self.params[name] = {}
        for p in range(self.h.get_parameter_count(i)):
            data = self.h.get_parameter_data(i, p)
            if data['type'] == PARAMETER_INPUT:
                self.params[name][self.h.get_parameter_info(i, p)['symbol']] = p
        return self.h.get_plugin_info(i)['name']

    def cmd_remove(self, name):
        with self.plugins_lock:
            i = self._id(name)
            self.h.show_custom_ui(i, False)
            if not self.h.remove_plugin(i):
                raise RuntimeError(self.h.get_last_error())
            del self.names[i]
            del self.params[name]
            self.active.pop(name, None)
            self.peaks.pop(name, None)

    def cmd_set(self, name, symbol, value):
        p = self.params[name].get(symbol)
        if p is None:
            raise ValueError(f'unknown setting {symbol}')
        self.h.set_parameter_value(self._id(name), p, value)

    def cmd_set_path(self, name, uri, value, is_path=True):
        """Set a file (or text) parameter. Carla has no call that hands one to the
        plugin, but restoring state does, so restore the current state with this key
        changed. (Carla cuts very long values short when loading state from a file,
        so this suits small state like model paths, not big saved blobs.)"""
        i = self._id(name)
        self.h.prepare_for_save(i)
        data = {(c['type'], c['key']): c['value']
                for c in (self.h.get_custom_data(i, n) for n in range(self.h.get_custom_data_count(i)))}
        data = {k: v for k, v in data.items() if k[1] != uri}
        data[(ATOM_PATH if is_path else ATOM_STRING, uri)] = value
        xml = ["<?xml version='1.0' encoding='UTF-8'?>", '<!DOCTYPE CARLA-PRESET>',
               "<CARLA-PRESET VERSION='2.0'>", '<Info><Type>LV2</Type>',
               f'<Name>{escape(name)}</Name><URI>{escape(self.h.get_plugin_info(i)["label"])}</URI></Info>',
               f'<Data><Active>{"Yes" if self.active.get(name, True) else "No"}</Active>']
        for (type_, key), v in data.items():
            if len(v) < 2000:
                xml.append(f'<CustomData><Type>{escape(type_)}</Type><Key>{escape(key)}</Key>'
                           f'<Value>{escape(v)}</Value></CustomData>')
        xml.append('</Data></CARLA-PRESET>')
        with tempfile.NamedTemporaryFile('w', suffix='.carxs') as f:
            f.write('\n'.join(xml))
            f.flush()
            if not self.h.load_plugin_state(i, f.name):
                raise RuntimeError(self.h.get_last_error())

    def cmd_values(self, name):
        i = self._id(name)
        return {s: self.h.get_current_parameter_value(i, p) for s, p in self.params[name].items()}

    def cmd_active(self, name, on):
        self.h.set_active(self._id(name), bool(on))
        self.active[name] = bool(on)

    def cmd_preset(self, name, preset):
        """Load an LV2 preset by its label (presets show up as Carla programs)."""
        i = self._id(name)
        names = [self.h.get_program_name(i, p) for p in range(self.h.get_program_count(i))]
        if preset not in names:
            raise LookupError(f'unknown preset {preset}')
        self.h.set_program(i, names.index(preset))

    def cmd_state(self, name):
        """Everything needed to write an LV2 preset: port values and plugin state."""
        i = self._id(name)
        self.h.prepare_for_save(i)
        custom = [self.h.get_custom_data(i, c) for c in range(self.h.get_custom_data_count(i))]
        return {'values': self.cmd_values(name),
                'state': [(c['type'], c['key'], c['value']) for c in custom]}

    def cmd_show_ui(self, name, show):
        i = self._id(name)
        if show and not self.h.get_plugin_info(i)['hints'] & PLUGIN_HAS_CUSTOM_UI:
            raise LookupError('this plugin has no window')
        self.h.show_custom_ui(i, bool(show))

    def cmd_meters(self):
        """{name: [input peak, output peak]} (0..1) since the last call."""
        with self.plugins_lock:
            out, self.peaks = self.peaks, {}
        return out

    def cmd_output(self, name, symbol):
        """The value of an output port (a tuner's frequency, a meter…)."""
        i = self._id(name)
        for p in range(self.h.get_parameter_count(i)):
            if self.h.get_parameter_info(i, p)['symbol'] == symbol:
                return self.h.get_current_parameter_value(i, p)
        raise ValueError(f'no output {symbol}')

    def cmd_volume(self, name, value):
        self.h.set_volume(self._id(name), value)

    def cmd_stats(self):
        info = self.h.get_runtime_engine_info()
        return info['load'], info['xruns']

    def cmd_cpu(self):
        return self.h.get_runtime_engine_info()['load']


def main():
    fd, client_name, lv2_path = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    sock = socket.socket(fileno=fd)
    Server(sock, client_name, lv2_path).run()


if __name__ == '__main__':
    main()
