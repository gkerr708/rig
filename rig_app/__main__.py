import difflib
import sys
from pathlib import Path

from .tui import ROOT, RigApp

NEW_RIG = """# New rig. Run `rig` with no arguments to open the most recent one.
in  = capture_AUX1
out = Scarlett_Solo*:playback_FL, Scarlett_Solo*:playback_FR

[amp] tone3000
"""


def main():
    rigs = ROOT / 'rigs'
    args = sys.argv[1:]
    if args and args[0] == '--tracks':          # render tracks/*.track to backing tracks
        from .tracks import main as render_tracks
        render_tracks(force='--all' in args)
        return
    new = '--new' in args
    args = [a for a in args if a != '--new']
    if args:
        path = Path(args[0])
        if path.suffix != '.rig':
            path = path.with_suffix('.rig')
        if not path.exists() and path.parent == Path('.'):
            path = rigs / path.name
        names = [p.stem for p in rigs.glob('*.rig')]
        close = difflib.get_close_matches(path.stem, names, n=3, cutoff=0.6)
        if not path.exists() and close and not new:
            sys.exit(f"no rig called '{path.stem}'. Did you mean: {', '.join(close)}?\n"
                     f"To make a new rig with that name: rig --new {path.stem}")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(NEW_RIG)
    else:
        existing = sorted(rigs.glob('*.rig'), key=lambda p: p.stat().st_mtime)
        if not existing:
            sys.exit('usage: rig <name>   (rigs live in ~/rig/rigs/)')
        path = existing[-1]
    RigApp(path).run()


main()
