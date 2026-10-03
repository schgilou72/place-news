"""place-news — placement optimizer and isolation-aware autorouting for KiCad.

Derived from CadMust-Neo by Remi Blokker (MIT licence).
"""
import os

__version__ = "0.4.0"

if not os.environ.get('PLACE_NEWS_NO_REGISTER'):
    try:
        from .place_news_action import PlaceNewsAction
        PlaceNewsAction().register()
        from .route_action import PlaceNewsRouteAction
        PlaceNewsRouteAction().register()
    except ImportError:
        # Running outside KiCad (e.g., unit tests) — pcbnew not available
        pass
