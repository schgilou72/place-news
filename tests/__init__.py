"""Test package. Tests import the plugin outside KiCad's editor: do not let
the package register its actions (KiCad aborts when that happens without the
editor running)."""
import os

os.environ.setdefault('PLACE_NEWS_NO_REGISTER', '1')
