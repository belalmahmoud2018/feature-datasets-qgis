"""GeoPackage logic for Feature Datasets (no QGIS GUI imports here)."""
import contextlib
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime

from osgeo import gdal, ogr, osr

DS_TABLE = "feature_datasets"
MEM_TABLE = "feature_dataset_members"

GPKG_APP_IDS = (1196444487, 1196437808)  # 'GPKG' and 'GP10'

GEOM_TYPES = [
    ("Point", ogr.wkbPoint),
    ("MultiPoint", ogr.wkbMultiPoint),
    ("LineString", ogr.wkbLineString),
    ("MultiLineString", ogr.wkbMultiLineString),
    ("Polygon", ogr.wkbPolygon),
    ("MultiPolygon", ogr.wkbMultiPolygon),
    ("No geometry (table)", ogr.wkbNone),
]

FIELD_TYPES = [
    ("Text", ogr.OFTString),
    ("Integer", ogr.OFTInteger),
    ("Integer64", ogr.OFTInteger64),
    ("Decimal", ogr.OFTReal),
    ("Date", ogr.OFTDate),
    ("DateTime", ogr.OFTDateTime),
]

RESERVED_FIELDS = {"fid", "geom", "geometry"}
BAD_NAME_CHARS = re.compile(r"[\"'`;\\/\[\]]")


class DatasetError(Exception):
    pass


# ----------------------------------------------------------------- helpers
def check_name(name, what="Name"):
    name = (name or "").strip()
    if not name:
        raise DatasetError("%s cannot be empty." % what)
    if BAD_NAME_CHARS.search(name):
        raise DatasetError("%s contains a character that is not allowed: %s" % (what, name))
    low = name.lower()
    if low.startswith(("gpkg_", "sqlite_", "rtree_")):
        raise DatasetError("%s cannot start with gpkg_, sqlite_ or rtree_." % what)
    return name


def is_geopackage(path):
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            if f.read(15) != b"SQLite format 3":
                return False
        con = sqlite3.connect(path, timeout=10)
        try:
            app_id = con.execute("PRAGMA application_id").fetchone()[0]
        finally:
            con.close()
        return app_id in GPKG_APP_IDS
    except Exception:
        return False


def create_empty_geopackage(path):
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(path)
    if ds is None:
        raise DatasetError("Could not create the GeoPackage: %s" % path)
    ds = None  # flush and close


def describe_srs(srs):
    if srs is None:
        return "-"
    with contextlib.suppress(Exception):
        n = srs.GetAuthorityName(None)
        c = srs.GetAuthorityCode(None)
        if n and c:
            return "%s:%s" % (n, c)
    name = None
    with contextlib.suppress(Exception):
        name = srs.GetName()
    return name or "unknown"


def make_srs(authid, wkt):
    srs = None
    if authid and authid.upper().startswith("EPSG:"):
        try:
            cand = osr.SpatialReference()
            if cand.ImportFromEPSG(int(authid.split(":")[1])) == 0:
                srs = cand
        except Exception:
            srs = None
    if srs is None:
        srs = osr.SpatialReference()
        try:
            res = srs.ImportFromWkt(wkt)
        except Exception as e:
            raise DatasetError("Invalid CRS definition: %s" % e)
        if res not in (0, None):
            raise DatasetError("Invalid CRS definition.")
    with contextlib.suppress(Exception):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def same_crs(a, b):
    if a is None or b is None:
        return a is None and b is None
    with contextlib.suppress(Exception):
        na, ca = a.GetAuthorityName(None), a.GetAuthorityCode(None)
        nb, cb = b.GetAuthorityName(None), b.GetAuthorityCode(None)
        if na and ca and nb and cb:
            return (na.upper(), str(ca)) == (nb.upper(), str(cb))
    same = False
    with contextlib.suppress(Exception):
        same = bool(a.IsSame(b))
    return same


# ---------------------------------------------------------------- metadata
def _connect(path):
    return sqlite3.connect(path, timeout=10)


def ensure_meta(path):
    con = _connect(path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS feature_datasets ("
            "name TEXT PRIMARY KEY, crs_authid TEXT, crs_wkt TEXT NOT NULL, "
            "description TEXT, created_at TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS feature_dataset_members ("
            "dataset_name TEXT NOT NULL, table_name TEXT NOT NULL, "
            "PRIMARY KEY (dataset_name, table_name))"
        )
        con.commit()
    finally:
        con.close()


def _has_meta(con):
    row = con.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
        (DS_TABLE, MEM_TABLE),
    ).fetchone()
    return row[0] == 2


def list_datasets(path):
    if not is_geopackage(path):
        return []
    con = _connect(path)
    try:
        if not _has_meta(con):
            return []
        rows = con.execute(
            "SELECT name, crs_authid, crs_wkt, description FROM feature_datasets ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    return [
        {"name": r[0], "authid": r[1], "wkt": r[2], "description": r[3]} for r in rows
    ]


def get_dataset(path, name):
    for d in list_datasets(path):
        if d["name"] == name:
            return d
    return None


def create_dataset(path, name, authid, wkt, description=""):
    name = check_name(name, "Dataset name")
    if not wkt:
        raise DatasetError("Please choose a valid CRS.")
    if not os.path.isfile(path):
        create_empty_geopackage(path)
    elif not is_geopackage(path):
        raise DatasetError("The selected file is not a GeoPackage.")
    ensure_meta(path)
    if get_dataset(path, name):
        raise DatasetError("A dataset named '%s' already exists in this file." % name)
    con = _connect(path)
    try:
        con.execute(
            "INSERT INTO feature_datasets (name, crs_authid, crs_wkt, description, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, authid or "", wkt, description or "", datetime.now().isoformat(timespec="seconds")),
        )
        con.commit()
    finally:
        con.close()


def list_members(path, dataset):
    con = _connect(path)
    try:
        if not _has_meta(con):
            return []
        rows = con.execute(
            "SELECT table_name FROM feature_dataset_members WHERE dataset_name=? ORDER BY table_name",
            (dataset,),
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def all_member_tables(path):
    con = _connect(path)
    try:
        if not _has_meta(con):
            return set()
        rows = con.execute("SELECT table_name FROM feature_dataset_members").fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


def add_member(path, dataset, table):
    ensure_meta(path)
    con = _connect(path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO feature_dataset_members (dataset_name, table_name) VALUES (?, ?)",
            (dataset, table),
        )
        con.commit()
    finally:
        con.close()


def remove_member(path, dataset, table):
    con = _connect(path)
    try:
        con.execute(
            "DELETE FROM feature_dataset_members WHERE dataset_name=? AND table_name=?",
            (dataset, table),
        )
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------ layer access
def list_tables(path):
    """Return [{'name','geom','srs'}] for every layer/table in the GeoPackage."""
    ds = ogr.Open(path)
    if ds is None:
        raise DatasetError("Could not open the GeoPackage.")
    out = []
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayerByIndex(i)
        gt = lyr.GetGeomType()
        out.append(
            {
                "name": lyr.GetName(),
                "geom": ogr.GeometryTypeToName(gt) if gt != ogr.wkbNone else "No geometry",
                "has_geom": gt != ogr.wkbNone,
                "srs": lyr.GetSpatialRef(),
            }
        )
    ds = None
    return out


def table_info(path, table):
    for t in list_tables(path):
        if t["name"] == table:
            return t
    return None


def create_layer(path, dataset, name, geom_type, fields):
    """Create a new layer inside `dataset`; its CRS is the dataset CRS."""
    name = check_name(name, "Layer name")
    ds_row = get_dataset(path, dataset)
    if ds_row is None:
        raise DatasetError("Dataset '%s' was not found." % dataset)
    seen = set()
    for fname, _ftype in fields:
        fname = check_name(fname, "Field name")
        if fname.lower() in RESERVED_FIELDS:
            raise DatasetError("'%s' is a reserved field name." % fname)
        if fname.lower() in seen:
            raise DatasetError("Duplicate field name: %s" % fname)
        seen.add(fname.lower())

    srs = make_srs(ds_row["authid"], ds_row["wkt"])
    ds = ogr.Open(path, 1)
    if ds is None:
        raise DatasetError(
            "Could not open the GeoPackage for writing. Make sure none of its layers "
            "is in edit mode in QGIS."
        )
    try:
        if ds.GetLayerByName(name) is not None:
            raise DatasetError("A layer named '%s' already exists in this file." % name)
        lyr = ds.CreateLayer(
            name, srs if geom_type != ogr.wkbNone else None, geom_type, ["OVERWRITE=NO"]
        )
        if lyr is None:
            raise DatasetError("The layer could not be created.")
        for fname, ftype in fields:
            if lyr.CreateField(ogr.FieldDefn(fname.strip(), ftype)) != 0:
                raise DatasetError("Could not create field '%s'." % fname)
        lyr = None
    finally:
        ds = None
    add_member(path, dataset, name)
    return name


def reproject_copy(path, src_table, new_name, dataset):
    """Copy `src_table` into the same GeoPackage, reprojected to the dataset CRS."""
    new_name = check_name(new_name, "Layer name")
    ds_row = get_dataset(path, dataset)
    srs = make_srs(ds_row["authid"], ds_row["wkt"])
    if table_info(path, new_name) is not None:
        raise DatasetError("A layer named '%s' already exists in this file." % new_name)
    tmpdir = tempfile.mkdtemp(prefix="featds_")
    tmp = os.path.join(tmpdir, "tmp.gpkg")
    try:
        src = gdal.OpenEx(path, gdal.OF_VECTOR)
        if src is None:
            raise DatasetError("Could not read the source layer.")
        step1 = gdal.VectorTranslate(
            tmp,
            src,
            options=gdal.VectorTranslateOptions(
                format="GPKG",
                layers=[src_table],
                layerName=new_name,
                dstSRS=srs.ExportToWkt(),
                reproject=True,
            ),
        )
        src = None
        if step1 is None:
            raise DatasetError("Reprojection failed.")
        step1 = None
        step2 = gdal.VectorTranslate(
            path,
            tmp,
            options=gdal.VectorTranslateOptions(
                format="GPKG", accessMode="update", layerName=new_name
            ),
        )
        if step2 is None:
            raise DatasetError(
                "Could not write into the GeoPackage. Make sure none of its layers "
                "is in edit mode in QGIS."
            )
        step2 = None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    add_member(path, dataset, new_name)
    return new_name


def validate(path):
    """Return (lines, problem_count)."""
    lines, problems = [], 0
    datasets = list_datasets(path)
    if not datasets:
        return ["No feature datasets found in this GeoPackage."], 0
    tables = {t["name"]: t for t in list_tables(path)}
    for d in datasets:
        srs = make_srs(d["authid"], d["wkt"])
        lines.append("Dataset: %s  (CRS: %s)" % (d["name"], describe_srs(srs)))
        members = list_members(path, d["name"])
        if not members:
            lines.append("    (no layers)")
        for m in members:
            t = tables.get(m)
            if t is None:
                lines.append("    [X] %s - table is missing from the file" % m)
                problems += 1
            elif not t["has_geom"]:
                lines.append("    [OK] %s - no geometry" % m)
            elif same_crs(t["srs"], srs):
                lines.append("    [OK] %s" % m)
            else:
                lines.append(
                    "    [X] %s - CRS is %s, dataset CRS is %s"
                    % (m, describe_srs(t["srs"]), describe_srs(srs))
                )
                problems += 1
        lines.append("")
    member_all = all_member_tables(path)
    loose = [n for n in tables if n not in member_all]
    if loose:
        lines.append("Layers not in any dataset: " + ", ".join(sorted(loose)))
    return lines, problems
