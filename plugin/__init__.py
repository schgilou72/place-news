"""place-news — placement optimizer, isolation-aware autorouting, aesthetic
check and sourcing for KiCad.

Derived from CadMust-Neo by Remi Blokker (MIT licence).
"""
import os

__version__ = "0.6.0"

if not os.environ.get('PLACE_NEWS_NO_REGISTER'):
    try:
        from .place_news_action import PlaceNewsAction
        PlaceNewsAction().register()
        from .route_action import PlaceNewsRouteAction
        PlaceNewsRouteAction().register()
        from .check_action import PlaceNewsCheckAction
        PlaceNewsCheckAction().register()
        from .sourcing_action import PlaceNewsSourcingAction
        PlaceNewsSourcingAction().register()
    except ImportError:
        # Running outside KiCad (e.g., unit tests) — pcbnew not available
        pass
