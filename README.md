# rig

A text-file guitar rig: edit a `.rig` file, and LV2 plugins running in
Carla's engine (its library, no Carla window) follow along.

    rig                 # open the most recently edited rig
    rig metal_rythm     # open rigs/metal_rythm.rig (creates it if missing)

## File format

    in  = hw_USB_0:capture_AUX1                   # port patterns: any part of the name, * wildcard
    out = Scarlett_Solo*:playback_FL, Scarlett_Solo*:playback_FR
    latency = 64                                  # buffer size in samples
    volume = -3dB                                 # master volume, at the very end

    [amp] tone3000                                # [label] plugin   (blocks run top to bottom)
      preset = metal_rythm-amp                    # full saved plugin state, from presets.lv2/
      toneBass = 6

    [comp] compressor_stereo  off                 # "off" = bypassed
      at = 15
      g_out = -3dB                                # gain settings take dB
      mode = FFT                                  # list settings take their labels

    split                                         # these paths run side by side, then mix
      [delay] gx_digital_delay_st
      [verb] gx_zita_rev1_stereo
    |                                             # nothing here: the dry signal
    join

Plugins are named by the end of their URI (`compressor_stereo`,
`gx_reverb_stereo`) or their bundle (`tone3000`, `aida-x`). File settings
take quoted paths: `model = "~/Music/guitar/models/Jcm800.nam"` on a
`neural_amp_modeler` block, `json = "~/models/amp.json"` on AIDA-X.

Blocks are wired top to bottom. `split` starts paths that run in parallel,
`|` starts the next path, and `join` mixes them back together. An empty path is
the dry signal, so a split with one effect path and one empty path is a
parallel wet/dry. Splits can nest. Each path's level comes from its plugins'
own output or wet/dry settings. The TUI draws the branches; wiring only ever
comes from the file.

## Workflow

Run `rig` in one window and edit the `.rig` file in another (nvim). Every time
the file is saved, the TUI applies it: values, bypass, added/removed/reordered
plugins, and in/out wiring.

Keys in the TUI also write to the file, so nvim needs to notice outside
changes. Put this in your nvim config:

    vim.o.autoread = true
    vim.api.nvim_create_autocmd({ 'FocusGained', 'BufEnter', 'CursorHold' },
      { pattern = '*.rig', command = 'checktime' })

`?` in the TUI lists every key. They follow vim where there's a vim habit:

| key | does |
|---|---|
| h l (← →) | previous / next block; IN and OUT sit at the two ends |
| j k (↓ ↑) | previous / next setting |
| gg G, ctrl+d ctrl+u | first / last setting, half a page down / up |
| 0 $ | jump to IN / OUT |
| / n N | find a setting by name, next / previous match |
| + − (`]` `[` fine) | nudge the setting; on OUT this is the master volume |
| enter or i | type a value (esc cancels) |
| dd | remove the setting's line from the file (back to the preset or default) |
| u, ctrl+r | undo / redo changes made from the TUI |
| b | bypass on/off |
| o | open the plugin's own window; changes are saved when you close it |
| m | capture picker (on a block that loads files, like `neural_amp_modeler`) |
| t | tuner (mutes the output while open; space unmutes) |
| p | looper (below) |
| B | backing tracks (below) |
| M | metronome on / off |
| R | record / stop (below) |
| T | timing trainer (below) |
| space | stop / play the loop (with no loop: the backing track) |
| a | write every setting of the plugin into the file |
| s | save the plugin's full internal state as a preset |
| e | edit the rig file in `$EDITOR` without leaving |
| < > | previous / next rig in `rigs/` |
| r | restart the audio engine |
| q | quit (stops the audio) |

Undo only covers changes made in the TUI. Saving the file from nvim clears it,
since nvim has its own undo.

## Meters

Under the chain are three meters. The bars are painted by zone (blue quiet,
green fine, gold hot, red too much), a marker holds the latest peak for a
moment, and `CLIP` stays lit for a few seconds after a peak reaches 0 dB.

- **in**: what arrives from the interface. If it goes red or shows CLIP, turn
  the gain knob on the Scarlett down.
- **amp**: what goes into the amp block (the one labelled `amp`, otherwise the
  first amp sim in the chain), with a verdict. About -36 to -12 dB is *good*.
  Above that it's *hot*, and above -3 dB it's *too hot*: amp sims sound harsher
  and squashed when driven that hard. Lower whatever comes before the amp, or
  the interface gain.
- **out**: what goes to the speakers, after the master volume.

The status line shows the DSP load and the number of xruns (dropouts). If xruns
keep climbing while you play, raise `latency`.

## Looper

`p` opens the looper. No footswitch needed: set the tempo and the number of
bars, press space, and it clicks one bar in, records exactly that many bars, and
starts looping by itself. Close it with esc and the loop keeps playing while you
play over it, change settings, or switch rigs.

| key | does |
|---|---|
| space | count in and record; then stop / play |
| r | record a new take (count-in again) |
| o | overdub on / off: each overdub is a layer |
| u, ctrl+r | undo / redo the newest layer |
| x | clear |
| + − / ] [ | tempo ±1 / ±10 bpm |
| t | tap the tempo |
| h l | fewer / more bars |
| b | beats per bar (2–7) |
| m | metronome on / off |
| , . | metronome volume −2 / +2 dB |
| w | save the loop as a WAV in `~/Music/guitar/loops/` |

It clicks through the count-in and the take, and stops clicking when the loop
starts playing; for a click while you play over it, turn on the metronome (`M`).
With the metronome running, the looper doesn't click at all: the metronome is
the count-in, and the take starts on its next bar line, so the loop stays in
time with it. Neither click is ever recorded.

It records what goes to the speakers, after the master volume, and plays into
the same outputs, so a loop sounds the way the rig did when you played it.
Timing is corrected by the round-trip latency PipeWire reports, so takes and
overdubs land on the beat. Tempo, bars and beats are remembered between
sessions in `~/.cache/rig/looper.json`, not in the rig file. The looper is
its own JACK client (`rig-looper`, `rig_app/looper_engine.py`) and only starts
the first time you press `p`.

## Recording

`R` starts recording and `R` again stops it; there's no time limit (a WAV
holds about 4 hours). Each take is two files in `~/Music/guitar/recordings/`,
named by the time and the rig:

- `… - mix.wav`: what you hear, your guitar with any backing track or loop
  (never the metronome or loop clicks)
- `… - guitar.wav`: the rig's output on its own, after the master volume

They're written to disk as you play, so long sessions don't fill memory, and
quitting mid-take still leaves complete files. The guitar is moved earlier by
the round-trip latency, so it lines up with the backing track in the mix.
A line under the meters shows the time while it records.

## Timing trainer

`T` practices your internal clock. The click plays for a couple of bars, then
goes silent for a couple more: keep counting, and come in on beat 1 after the
gap. The click comes back on that beat too, so you hear whether you're
together, and rig shows how far off your first note was: `+23 ms` late or
`-12 ms` early, where it landed, your average, and whether you tend to rush or
drag. It goes round after round until you stop it.

| key | does |
|---|---|
| space | start / stop |
| + − / ] [ | tempo ±1 / ±10 |
| h l | fewer / more silent bars |
| j k | fewer / more click bars |
| b | beats per bar |
| , . | click volume (shared with the metronome) |

Under 15 ms is right on it; 15–35 ms is a little off. The screen doesn't
show where you are during the gap, so you can't count by watching. Your
interface's latency is taken out, so 0 ms means you hit the string on the
beat. Stop playing during the gap: rig listens just before the beat to learn
the noise, and says so if you were still playing. Tempo and beats are shared
with the looper and metronome; it stops the metronome while it runs.

## Metronome

`M` starts and stops a metronome, accented on the first beat of each bar. It
uses the looper's tempo and beats per bar: open the looper with `p` and change
them there (`+ − ] [`, tap `t`, `b`) and a running metronome follows. Its volume
is its own: `,` and `.` in the looper lower and raise it by 2 dB (remembered
between sessions). `m`
inside the looper toggles it too. It runs on its own, separate from any loop,
and isn't recorded into loops.

## Backing tracks

`B` lists the audio files in `~/Music/guitar/backing/` (mp3, wav, flac, ogg,
m4a…; anything ffmpeg reads). Enter plays one, space pauses, `h` `l` jump
5 s back / ahead, `0` goes back to the start, `+` `−` set its volume. Close
it and the track keeps playing; the line under the meters shows where it is.

The track plays from the looper's process straight to the outputs: loops don't
record it, and the master volume doesn't change it.

A track with a tempo in its name (`… - 92bpm.mp3`) sets the looper's tempo when
it starts, so the metronome and a new loop line up with it.

### Making your own

`tracks/` holds the charts the included tracks come from. A chart becomes
MIDI, and FluidSynth plays it with sampled instruments from the GeneralUser GS
SoundFont (`~/.local/share/soundfonts/GeneralUser-GS.sf2`, free to use for
your own music): drums, bass, and organ, piano, Rhodes, clavinet or a
palm-muted rhythm guitar.

    title = Blues shuffle in A
    bpm   = 92
    style = shuffle        # rock | shuffle | slowblues | metal | funk | punk | halftime | swing
    keys  = organ          # organ | piano | epiano | clav | guitar | none (default: the style's)
    minutes = 3            # roughly; the chart repeats to fill it

    | A7 | D7 | A7 | A7 |  # one bar each; | A7 D7 | splits a bar
    | D7 | D7 | A7 | A7 |
    | E7 | D7 | A7 | E7 |

A file with no chart is a drum track. Chords: major, `m`, `7`, `m7`, `maj7`,
`9`, `m9`, `5`, `6`, `sus2`, `sus4`, `dim`, `dim7`, `m7b5`, `7b9`, with `#` or `b`.
`swing` walks the bass and comps the piano in shell voicings (3rd, 7th, 9th). Every track
counts in with a bar of sticks, puts a tom fill every 8 bars, and ends on
the first chord. `rig --tracks` renders the charts that changed into
`~/Music/guitar/backing/` (`rig --tracks --all` redoes every one).

| track | style | play over it with |
|---|---|---|
| Blues shuffle in A, 92 | shuffle, organ | A minor / major pentatonic |
| Slow blues in G, 60 | 12/8, Rhodes | G minor pentatonic, G blues |
| Rock in E minor, 112 | Em C G D | E minor pentatonic, E aeolian |
| Pop rock in G, 120 | G D Em C, piano | G major pentatonic |
| Dorian jam in A, 100 | Am7 D9 vamp | A dorian |
| Funk vamp in E9, 96 | E9 A9, clavinet | E dorian, E minor pentatonic |
| Punk in D, 180 | D G A G, rhythm guitar | D major |
| Jazz blues in F, 132 | swing: walking upright bass, piano, jazz kit | F blues, then the changes (ii–Vs in bars 4 and 8–12) |
| Ballad in A minor, 70 | Am F C G, halftime, piano | A minor pentatonic |
| Drums: rock 100, shuffle 84, metal 140, funk 96, punk 180, halftime 70 | | anything |

## Master volume

`volume = -3dB` at the top of a rig sets the level at the very end of the chain
(-inf up to +2 dB). Select OUT (`$`, or `l` past the last block) and use `+` / `−`,
or press enter to type it.

## Rigs

| rig | amp | chain |
|---|---|---|
| `default` | TONE3000 Fender Super Reverb | full pedalboard, all off except comp → amp → reverb: DS-1, tremolo, chorus, phaser, flanger, dotted-8th delay, 1/4 delay |
| `metal_rythm` | TONE3000 Fender Super Reverb | your Carla setup |
| `fender_clean` | TONE3000 Fender Super Reverb | comp → amp → chorus → dotted-8th delay → reverb |
| `jcm800_crunch` | Guitarix JCM800 preamp | gate → TS9 boost → amp → Marshall cab → EQ → slapback → reverb |
| `metal_highgain` | GxMetalAmp | gate → TS9 tightener → amp → high-gain cab → mud-cut EQ → tiny reverb |
| `metal_lead` | GxMetalHead | gate → amp → Mesa cab → phaser (off) → 1/4 delay → big reverb |
| `amp_x_mesa` | GxAmplifier-X, Mesa tonestack | gate → amp (built-in 4x12) → flanger (off) → delay → reverb |
| `chump_blues` | Redeye Chump | comp → amp → 1x12 → tube echo → reverb |
| `vibrochump_surf` | Redeye Vibro Chump | amp (vibrato on) → Twin cab → tube slapback → lots of reverb |
| `bigchump_fuzz` | Redeye Big Chump | Fuzz Face → amp → 2x12 → phaser → reverb |
| `studio_clean` | Gx Studio Preamp | comp → amp → Twin cab → tremolo (off) → chorus → ping-pong delay → reverb |
| `ds1_punk` | Gx Alembic preamp | gate → Boss DS-1 → amp → 4x12 → touch of room |

Every new rig ends in `[limit]`: a limiter that keeps peaks below -1 dB. Set the
overall level with the master volume (above).

`rig <name>` opens `rigs/<name>.rig`. A name close to an existing rig (a typo)
stops with a suggestion; `rig --new <name>` makes a new rig anyway.

## Plugin windows

`o` opens the selected plugin's own window (TONE3000, LSP, Guitarix…). When
you close it, rig keeps what you changed: settings that are already in the
file get their new values written in place, and everything else (the
plugin's full internal state) is saved as `preset = <rig>-<label>`, the same
as `s` does.

## Capture picker

`m` on an amp block lists `~/Music/guitar/models` (`~/Music/guitar/irs` when
the selected setting is an impulse response or cabinet). Moving the
highlight loads each capture live so you can hear it, Enter writes
`model = …`, and Esc puts back what was there.

NAM captures record their loudness, so switching captures also moves the
block's `output_level` by the difference and the volume stays where it was.
The level you set is kept relative to the capture, so turn a capture up or
down with `output_level` as usual.

## Latency

`latency = 64` (top of the file) is the buffer size in samples: 64 is about
1.3 ms per buffer at 48 kHz, and 128 is about 2.7 ms. A rig without a
`latency` line uses 64. PipeWire runs at the
smallest buffer any app asks for, so this only applies while rig is running.
Your usual setting (1024) comes back when you quit. Changing it restarts the
audio engine. If you hear crackles, raise it.

## Notes

- The audio engine is Carla's `libcarla_standalone2`, run from
  `rig_app/carla_engine.py` in its own process. Every plugin is its own JACK
  client (`rig.0/<label>`), so `pw-link` and any patchbay show the chain.
  Its log is at `~/.cache/rig/engine.log`. Plugin info is cached in
  `~/.cache/rig/`.
- Presets in `presets.lv2/` are ordinary LV2 presets. Carla lists them as
  programs (through a link in `~/.cache/rig/lv2/`) and only reads the list at
  startup, so a rig that uses a preset saved during the session restarts the
  engine by itself.
- File settings (NAM models, AIDA-X models) reach the plugin as a state
  restore, because Carla has no call for setting them directly.
- Neural Amp Modeler is built from `~/src/neural-amp-modeler-lv2` into
  `~/.lv2/neural_amp_modeler.lv2`. It plays TONE3000's "A2" captures
  (`SlimmableContainer` models). The captures inside your TONE3000 presets
  are in `~/Music/guitar/models/TONE3000 *.nam`. The two Mesa ones are DI
  captures with no cabinet, so give them a cab block or IR.

## To do

Next up (together): pick a backing track, set its tempo and key, and play along
with the chords on screen.

1. **Tempo and key in rig**: change a backing track's tempo and key from the
   backing screen. Tracks come from charts, so this re-renders them (about
   2 s) rather than stretching the audio: no artifacts at any speed or key.
2. **Chord chart in time with the track**: the current and next chord in the
   TUI as the track plays, with the notes to aim for (chord tones, a scale).
   Most useful on the jazz blues.

Later:

3. **Play back takes in rig**: list `recordings/` and `loops/`, play them,
   compare takes, without leaving the app.
4. **Take mode**: one key starts a backing track and the recorder together,
   for a clean, lined-up take of a whole song.
5. **More timing practice** (the gap trainer is done, `T`): the metronome
   speeds up a few bpm every N bars; a mode where you keep playing through the
   gap and it shows how far you drifted; the gap growing as you get it right.
6. **Re-amp**: keep the last minute of dry guitar; replay a riff (or a whole
   take) through the rig while you tweak with your hands off the guitar.
7. **Loudness-matched A/B**: switch between two settings (or the last saved
   file) at matched volume, so the louder one doesn't win by default.
8. **nvim support for `.rig` files**: plugin and setting names complete as you
   type, ranges show on hover, typos and bad values get flagged.
9. **Better drums**: dedicated multi-sampled kits instead of the general-purpose
   SoundFont ones (needs a package install).
# rig
