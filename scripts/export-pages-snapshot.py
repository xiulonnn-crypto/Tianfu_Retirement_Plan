"""Export the read-only overview data consumed by the GitHub Pages fallback."""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from server import app


OUTPUT=ROOT/'assets'/'overview-snapshot.json'
ROUTES=('overview','market','data-health')


def main():
    app.config['TESTING']=True
    with app.test_client() as client:
        snapshot={route:client.get(f'/api/{route}').get_json() for route in ROUTES}
    OUTPUT.write_text(json.dumps(snapshot,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print(f'Wrote {OUTPUT.relative_to(ROOT)}')


if __name__=='__main__':
    main()
