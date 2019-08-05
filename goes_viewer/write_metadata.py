from functools import partial
import json


from pyproj import transform
import requests


from goes_viewer.constants import WEB_MERCATOR, GEODETIC


def filter_func(filters, item):
    for k, v in filters.items():
        if k not in item:
            return False
        else:
            if item[k] not in v:
                return False
    return True


def parse_metadata(url, filters, auth=()):
    req = requests.get(url, auth=auth)
    req.raise_for_status()
    out = set()
    js = req.json()['Metadata']
    filtered = filter(partial(filter_func, filters), js)
    for site in filtered:
        pt = transform(GEODETIC, WEB_MERCATOR, site['Longitude'], site['Latitude'],
                       always_xy=True)
        out.add({'name': site['Name'], 'x': pt[0], 'y': pt[1]})

    return list(out)




if __name__ == '__main__':
    out = parse_metadata(
        'https://forecasting.energy.arizona.edu/api/v2/public/metadata?db=irradsensors',
        {'Type': 'ghi'},
        auth=())
    with open('figs/metadata.json', 'w') as f:
        json.dump(out, f)
