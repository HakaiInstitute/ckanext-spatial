import logging
import re
import ckantoolkit as tk
from ckan.model import Session, Package
from ckanext.spatial import PackageExtent
from ckanext.spatial.geoalchemy_common import (WKTElement, ST_Transform)
config = tk.config

log = logging.getLogger(__name__)


def get_srid(crs):
    """Returns the SRID for the provided CRS definition
    The CRS can be defined in the following formats
    - urn:ogc:def:crs:EPSG::4326
    - EPSG:4326
    - 4326
    """

    if ":" in crs:
        crs = crs.split(":")
        srid = crs[len(crs) - 1]
    else:
        srid = crs

    return int(srid)


def normalize_polygon(poly_wkt):
    '''
    Ensures a polygon or multipolygon is expressed in well known text.

    poly_wkt may be:
           a polygon string: "POLYGON((x1 y1,x2 y2, ....))"
           or a multipolygon string: "MULTIPOLYGON(((x1 y1,x2 y2, ....)),((x1 y1,x2 y2, ....)))"
           or a box string: "BOX(minx,miny,maxx,maxy)"
    and returns the same WKT or none if the validation failed

    Note that multipolygon internal rings are not supported. external rings only.
      This "MULTIPOLYGON(((...)),((...)))" is valid but "MULTIPOLYGON(((...)),(...))" is not
    '''

    regex_poly = "^POLYGON\\(\\(-?\\d+\\.?\\d* -?\\d+\\.?\\d*(?:, -?\\d+\\.?\\d* -?\\d+\\.?\\d*)*\\)\\)"
    regex_multipoly = "^MULTIPOLYGON\\(\\(\\(-?\\d+\\.?\\d* -?\\d+\\.?\\d*(?:, -?\\d+\\.?\\d* -?\\d+\\.?\\d*)*(?:\\)\\),\\(\\(-?\\d+\\.?\\d* -?\\d+\\.?\\d*(?:, -?\\d+\\.?\\d* -?\\d+\\.?\\d*)*)*\\)\\)\\)"
    regex_box = "^BOX\\(-?\\d+\\.?\\d*,-?\\d+\\.?\\d*,-?\\d+\\.?\\d*,-?\\d+\\.?\\d*\\)"

    if not isinstance(poly_wkt, str):
        return None

    foundPoly = re.match(regex_poly, poly_wkt, re.IGNORECASE)
    foundMultiPoly = re.match(regex_multipoly, poly_wkt, re.IGNORECASE)
    foundBox = re.match(regex_box, poly_wkt, re.IGNORECASE)
    if not foundPoly and not foundMultiPoly and not foundBox:
        return None

    return poly_wkt

def normalize_bbox(bbox_values):
    """
    Ensures a bbox is expressed in a standard dict

    bbox_values may be:
           a string: "-4.96,55.70,-3.78,56.43"
           or a list [-4.96, 55.70, -3.78, 56.43]
           or a list of strings ["-4.96", "55.70", "-3.78", "56.43"]

    ordered as MinX, MinY, MaxX, MaxY.

    Returns a dict with the keys:

       {
            "minx": -4.96,
            "miny": 55.70,
            "maxx": -3.78,
            "maxy": 56.43
        }

    If there are any problems parsing the input it returns None.
    """

    if isinstance(bbox_values, str):
        bbox_values = bbox_values.split(",")

    if len(bbox_values) != 4:
        return None

    try:
        bbox = {}
        bbox["minx"] = float(bbox_values[0])
        bbox["miny"] = float(bbox_values[1])
        bbox["maxx"] = float(bbox_values[2])
        bbox["maxy"] = float(bbox_values[3])
    except ValueError:
        return None

    return bbox


def fit_bbox(bbox_dict):
    """
    Ensures that all coordinates in a bounding box
    fall within -180, -90, 180, 90 degrees

    Accepts a dict with the following keys:

       {
            "minx": -4.96,
            "miny": 55.70,
            "maxx": -3.78,
            "maxy": 56.43
        }

    """

    def _adjust_longitude(value):
        if value < -180 or value > 180:
            value = value % 360
            if value < -180:
                value = 360 + value
            elif value > 180:
                value = -360 + value
        return value

    def _adjust_latitude(value):
        if value < -90 or value > 90:
            value = value % 180
            if value < -90:
                value = 180 + value
            elif value > 90:
                value = -180 + value
        return value

    return {
        "minx": _adjust_longitude(bbox_dict["minx"]),
        "maxx": _adjust_longitude(bbox_dict["maxx"]),
        "miny": _adjust_latitude(bbox_dict["miny"]),
        "maxy": _adjust_latitude(bbox_dict["maxy"]),
    }


def fit_linear_ring(lr):

    bbox = {
        "minx": lr[0][0],
        "maxx": lr[2][0],
        "miny": lr[0][1],
        "maxy": lr[2][1],
    }

    bbox = fit_bbox(bbox)

    return [
        (bbox["minx"], bbox["maxy"]),
        (bbox["minx"], bbox["miny"]),
        (bbox["maxx"], bbox["miny"]),
        (bbox["maxx"], bbox["maxy"]),
        (bbox["minx"], bbox["maxy"]),
    ]


def _bbox_2_wkt(bbox, srid):
    '''
    Given a bbox dictionary, return a WKTSpatialElement, transformed
    into the database\'s CRS if necessary.

    returns e.g. WKTSpatialElement("POLYGON ((2 0, 2 1, 7 1, 7 0, 2 0))", 4326)
    '''
    db_srid = int(config.get('ckan.spatial.srid', '4326'))

    bbox_template = Template(
        'POLYGON (($minx $miny, $minx $maxy, $maxx $maxy, $maxx $miny, $minx $miny))')

    wkt = bbox_template.substitute(minx=bbox['minx'],
                                   miny=bbox['miny'],
                                   maxx=bbox['maxx'],
                                   maxy=bbox['maxy'])

    if srid and srid != db_srid:
        # Input geometry needs to be transformed to the one used on the database
        input_geometry = ST_Transform(WKTElement(wkt, srid), db_srid)
    else:
        input_geometry = WKTElement(wkt, db_srid)
    return input_geometry


def polygon_query(wkt, srid=None):
    '''
    Performs a spatial query of a bounding box.

    poly - WKT polygon or multipolygon
        POLYGON((x1 y1,x2 y2,x3 y3,x4 y4,x5 y5))
        MULTIPOLYGON(((x1 y1,x2 y2, ....)),((x1 y1,x2 y2, ....)))

    Returns a query object of PackageExtents, which each reference a package
    by ID.
    '''

    db_srid = int(config.get('ckan.spatial.srid', '4326'))

    if srid and srid != db_srid:
        # Input geometry needs to be transformed to the one used on the database
        input_geometry = ST_Transform(WKTElement(wkt, srid), db_srid)
    else:
        input_geometry = WKTElement(wkt, db_srid)

    if input_geometry is None:
        return None

    extents = Session.query(PackageExtent) \
        .filter(PackageExtent.package_id == Package.id) \
        .filter(PackageExtent.the_geom.ST_Intersects(input_geometry)) \
        .filter(Package.state == u'active')

    return extents


def bbox_query(bbox, srid=None):
    '''
    Performs a spatial query of a bounding box.

    bbox - bounding box dict

    Returns a query object of PackageExtents, which each reference a package
    by ID.
    '''

    input_geometry = _bbox_2_wkt(bbox, srid)

    extents = Session.query(PackageExtent) \
        .filter(PackageExtent.package_id == Package.id) \
        .filter(PackageExtent.the_geom.intersects(input_geometry)) \
        .filter(Package.state == u'active')
    return extents


def bbox_query_ordered(bbox, srid=None):
    '''
    Performs a spatial query of a bounding box. Returns packages in order
    of how similar the data\'s bounding box is to the search box (best first).

    bbox - bounding box dict

    Returns a query object of PackageExtents, which each reference a package
    by ID.
    '''

    input_geometry = _bbox_2_wkt(bbox, srid)

    params = {'query_bbox': str(input_geometry),
              'query_srid': input_geometry.srid}

    # First get the area of the query box
    sql = "SELECT ST_Area(ST_GeomFromText(:query_bbox, :query_srid));"
    params['search_area'] = Session.execute(sql, params).fetchone()[0]

    # Uses spatial ranking method from "USGS - 2006-1279" (Lanfear)
    sql = """SELECT ST_AsBinary(package_extent.the_geom) AS package_extent_the_geom,
                    POWER(ST_Area(ST_Intersection(package_extent.the_geom, ST_GeomFromText(:query_bbox, :query_srid))),2)/ST_Area(package_extent.the_geom)/:search_area as spatial_ranking,
                    package_extent.package_id AS package_id
             FROM package_extent, package
             WHERE package_extent.package_id = package.id
                AND ST_Intersects(package_extent.the_geom, ST_GeomFromText(:query_bbox, :query_srid))
                AND package.state = 'active'
             ORDER BY spatial_ranking desc"""
    extents = Session.execute(sql, params).fetchall()
    log.debug('Spatial results: %r',
              [('%.2f' % extent.spatial_ranking, extent.package_id) for extent in extents[:20]])
    return extents
