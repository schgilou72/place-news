"""place-news — placement optimizer for KiCad, aware of isolation rules.

Derived from CadMust-Neo by Remi Blokker (MIT licence).
"""
__version__ = "0.2.0"

try:
    from .place_news_action import PlaceNewsAction
    PlaceNewsAction().register()
except ImportError:
    # Running outside KiCad (e.g., unit tests) — pcbnew not available
    pass
