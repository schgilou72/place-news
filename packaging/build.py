"""Build the installable archives into dist/.

    python3 packaging/build.py

- place-news-<version>-pcm.zip     KiCad Plugin and Content Manager, "Install from File..."
- place-news-<version>-plugin.zip  manual install: unzip into the scripting plugins folder
"""
import json
import os
import re
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, 'plugin')
DIST = os.path.join(ROOT, 'dist')

# Files of plugin/ that are not shipped (runtime state, caches).
SKIP = {'settings.json', 'route_settings.json', 'debug.log', 'profile.log'}

IDENTIFIER = 'local.place-news'   # set the final reverse-DNS id before publishing


def version() -> str:
    with open(os.path.join(PLUGIN, '__init__.py'), encoding='utf-8') as f:
        return re.search(r'__version__\s*=\s*"([^"]+)"', f.read()).group(1)


def plugin_files():
    for name in sorted(os.listdir(PLUGIN)):
        path = os.path.join(PLUGIN, name)
        if name in SKIP or name.startswith('.') or not os.path.isfile(path):
            continue
        if name.endswith(('.py', '.png')):
            yield name, path


def metadata(ver: str) -> dict:
    return {
        "$schema": "https://go.kicad.org/pcm/schemas/v1",
        "name": "place-news",
        "description": "Component placement and Freerouting autorouting that respect "
                       "clearance and creepage rules (power electronics, HV).",
        "description_full": (
            "place-news adds two actions to the PCB editor.\n\n"
            "Placement: simulated annealing that minimises wirelength while keeping "
            "pads as far apart as the net-class clearances and the custom rules "
            "(clearance, creepage, physical_clearance in the .kicad_dru file) "
            "demand, and honouring rule areas (footprint/pad keep-outs, placement "
            "areas by sheet, component class or group).\n\n"
            "Routing: Specctra round trip with Freerouting (Java 25+, Freerouting "
            "2.4+, not included). The isolation rules become Freerouting class "
            "clearances, rule areas only block what they really forbid, and the "
            "result is checked with KiCad's DRC.\n\n"
            "Derived from CadMust-Neo by Remi Blokker (MIT)."),
        "identifier": IDENTIFIER,
        "type": "plugin",
        "author": {"name": "Gilles Schweitzer", "contact": {}},
        "license": "MIT",
        "resources": {},
        "tags": ["placement", "autorouter", "freerouting", "creepage", "clearance",
                 "power-electronics"],
        "versions": [{"version": ver, "status": "testing", "kicad_version": "10.0"}],
    }


def build() -> list:
    ver = version()
    os.makedirs(DIST, exist_ok=True)
    out = []

    pcm = os.path.join(DIST, f'place-news-{ver}-pcm.zip')
    with zipfile.ZipFile(pcm, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('metadata.json', json.dumps(metadata(ver), indent=2) + '\n')
        z.write(os.path.join(ROOT, 'packaging', 'icon.png'), 'resources/icon.png')
        for name, path in plugin_files():
            z.write(path, f'plugins/{name}')
        z.write(os.path.join(ROOT, 'LICENSE'), 'plugins/LICENSE')
        z.write(os.path.join(ROOT, 'README.md'), 'plugins/README.md')
    out.append(pcm)

    manual = os.path.join(DIST, f'place-news-{ver}-plugin.zip')
    with zipfile.ZipFile(manual, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, path in plugin_files():
            z.write(path, f'place_news/{name}')
        z.write(os.path.join(ROOT, 'LICENSE'), 'place_news/LICENSE')
        z.write(os.path.join(ROOT, 'README.md'), 'place_news/README.md')
    out.append(manual)
    return out


if __name__ == '__main__':
    for p in build():
        print(p, os.path.getsize(p), 'bytes')
