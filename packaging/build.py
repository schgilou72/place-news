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
SKIP = {'settings.json', 'route_settings.json', 'sourcing.json', 'sourcing-cache.json', 'debug.log',
        'profile.log'}

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
        "description": "Component placement, Freerouting autorouting and a ground pour that "
                       "respect clearance and creepage rules (power electronics, HV), an "
                       "aesthetic and cost check that learns your taste, and BOM sourcing at "
                       "DigiKey, Mouser, Farnell, TME and LCSC.",
        "description_full": (
            "place-news adds four actions to the PCB editor.\n\n"
            "Placement: simulated annealing that minimises wirelength while keeping "
            "pads as far apart as the net-class clearances and the custom rules "
            "(clearance, creepage, physical_clearance in the .kicad_dru file) "
            "demand, and honouring rule areas (footprint/pad keep-outs, placement "
            "areas by sheet, component class or group). Then a tidy-up: same-type "
            "parts in rows and columns, readable references, an untangled ratsnest "
            "(fewer vias).\n\n"
            "Routing: Specctra round trip with Freerouting (Java 25+, Freerouting "
            "2.4+, not included). The isolation rules become Freerouting class "
            "clearances, rule areas only block what they really forbid; fewer vias, "
            "a try on the two outer layers of multilayer boards, a ground pour on the "
            "top layer that keeps the creepage distances, and a check with KiCad's "
            "DRC.\n\n"
            "Aesthetic check: placement, routing and cost criteria, numbered markers "
            "and an HTML report, a layer-count estimate (routing room per net, BGA "
            "escape), and learning from your opinion and your corrections.\n\n"
            "Sourcing (BOM): stock, price breaks, minimum order and multiple, "
            "lifecycle and replacements of every BOM line at DigiKey, Mouser, "
            "Farnell, TME (beta) and LCSC, with your own free API keys (LCSC needs "
            "none). Search by MPN, by distributor part number or, for resistors, "
            "capacitors and inductors, by value, package and ratings (same package, "
            "ratings kept). The chosen parts are written into the components' fields "
            "(MPN, manufacturer, distributor part numbers, ratings), and saved as a "
            "BOM with the designators, unit prices and minimum orders, plus one order "
            "list per distributor (designators as customer reference).\n\n"
            "Derived from CadMust-Neo by Remi Blokker (MIT)."),
        "identifier": IDENTIFIER,
        "type": "plugin",
        "author": {"name": "Gilles Schweitzer", "contact": {}},
        "license": "MIT",
        "resources": {},
        "tags": ["placement", "autorouter", "freerouting", "creepage", "clearance",
                 "power-electronics", "ground-plane", "design-check", "bom", "sourcing"],
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
