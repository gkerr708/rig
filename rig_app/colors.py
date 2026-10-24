"""Rosé Pine palette, on a black background: nothing turns gray when focused or selected."""
from dataclasses import replace

from textual.theme import BUILTIN_THEMES

TEXT, SUBTLE, MUTED = '#e0def4', '#908caa', '#6e6a86'
LOVE, GOLD, ROSE, PINE, FOAM, IRIS = '#eb6f92', '#f6c177', '#ebbcba', '#31748f', '#9ccfd8', '#c4a7e7'
OVERLAY, HIGHLIGHT = '#26233a', '#403d52'
BLACK = '#000000'

_base = BUILTIN_THEMES['rose-pine']
THEME = replace(_base, name='rose-pine-black', background=BLACK, surface=BLACK, panel=BLACK,
                variables={**_base.variables,
                           'footer-background': BLACK, 'footer-item-background': BLACK,
                           'footer-key-background': BLACK, 'footer-description-background': BLACK,
                           'block-cursor-background': BLACK, 'block-cursor-blurred-background': BLACK,
                           'block-cursor-foreground': IRIS, 'block-cursor-blurred-foreground': IRIS,
                           'block-cursor-text-style': 'bold underline',
                           'block-cursor-blurred-text-style': 'underline',
                           'block-hover-background': BLACK, 'surface-active': BLACK,
                           'scrollbar-background': BLACK, 'scrollbar-background-hover': BLACK,
                           'scrollbar-background-active': BLACK, 'scrollbar-corner-color': BLACK,
                           'scrollbar': OVERLAY, 'scrollbar-hover': HIGHLIGHT, 'scrollbar-active': HIGHLIGHT,
                           'input-selection-background': HIGHLIGHT})

# Widgets add a light tint when focused; on black that reads as gray, so drop it everywhere.
BLACK_CSS = """
Screen, ModalScreen { background: #000000; }
DataTable, DataTable:focus, OptionList, OptionList:focus, Input, Input:focus {
    background: #000000; background-tint: 0%;
}
DataTable > .datatable--header, DataTable:focus > .datatable--header {
    background: #000000; background-tint: 0%;
}
Toast { background: #000000; border-left: outer $primary; }
Toast.-error { border-left: outer $error; }
Toast.-warning { border-left: outer $warning; }
"""
