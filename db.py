"""Storage back-ends for Feature Datasets: GeoPackage, SpatiaLite, File Geodatabase.

No QGIS GUI imports here, only GDAL/OGR and the standard library.
"""
import contextlib
import functools
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime

from osgeo import gdal, ogr, osr

GPKG_APP_IDS = (1196444487, 1196437808)  # 'GPKG' and 'GP10'
MIN_GDAL_FGDB = 3060000  # OpenFileGDB write support

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

RESERVED_FIELDS = {"fid", "geom", "geometry", "objectid", "ogc_fid"}
BAD_NAME_CHARS = re.compile(r"[\"'`;\\/\[\]]")
HIDDEN_TABLES = {"feature_datasets", "feature_dataset_members"}
SPATIALITE_INTERNAL = (
    "sqlite_", "idx_", "spatial_ref_sys", "geometry_columns", "spatialite_",
    "views_", "virts_", "vector_layers", "data_licenses", "sql_statements_log",
    "elementarygeometries", "geom_cols_ref_sys", "raster_coverages", "knn",
    "iso_metadata", "stored_", "topologies", "networks", "se_", "wms_",
)


class DatasetError(Exception):
    pass


def _friendly(err):
    msg = str(err)
    if "permission denied" in msg.lower():
        msg += (
            "\n\nCheck that the folder is writable and that no other program (or a layer "
            "loaded in QGIS / ArcGIS) has the file open."
        )
    return msg


def _guard(fn):
    """Turn GDAL / OS exceptions into DatasetError with a readable message."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DatasetError:
            raise
        except (RuntimeError, OSError) as e:
            raise DatasetError(_friendly(e)) from e

    return wrapper


def make_backup(path):
    """Copy a file (or a .gdb folder) next to the original and return the new path."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(path.rstrip("/\\"))
    dest = "%s_backup_%s%s" % (base, stamp, ext)
    if os.path.isdir(path):
        shutil.copytree(path, dest)
    else:
        shutil.copy2(path, dest)
    return dest


# ----------------------------------------------------------------- helpers
def check_name(name, what="Name"):
    name = (name or "").strip()
    if not name:
        raise DatasetError("%s cannot be empty." % what)
    if BAD_NAME_CHARS.search(name):
        raise DatasetError("%s contains a character that is not allowed: %s" % (what, name))
    if name.lower().startswith(("gpkg_", "sqlite_", "rtree_")):
        raise DatasetError("%s cannot start with gpkg_, sqlite_ or rtree_." % what)
    return name


def validate_fields(fields):
    seen = set()
    for fname, _ftype in fields:
        fname = check_name(fname, "Field name")
        if fname.lower() in RESERVED_FIELDS:
            raise DatasetError("'%s' is a reserved field name." % fname)
        if fname.lower() in seen:
            raise DatasetError("Duplicate field name: %s" % fname)
        seen.add(fname.lower())


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
        if not wkt:
            raise DatasetError("The dataset has no CRS defined.")
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


def layer_info(lyr):
    gt = lyr.GetGeomType()
    srs = lyr.GetSpatialRef()
    return {
        "name": lyr.GetName(),
        "geom": ogr.GeometryTypeToName(gt) if gt != ogr.wkbNone else "No geometry",
        "has_geom": gt != ogr.wkbNone,
        "srs": srs.Clone() if srs is not None else None,
    }


# ------------------------------------------------------------- base class
class Backend:
    key = ""
    label = ""
    hint = ""
    drivers = []
    extensions = ()
    open_filter = ""
    save_filter = ""
    is_directory = False

    # -- paths / capabilities
    def normalize_path(self, path):
        path = (path or "").strip()
        if path and not path.lower().endswith(self.extensions):
            path += self.extensions[0]
        return path

    def geom_types(self):
        return GEOM_TYPES

    def field_types(self):
        return FIELD_TYPES

    def check(self, name, what):
        return check_name(name, what)

    def uri(self, path, name):
        return "%s|layername=%s" % (path, name)

    def hidden(self, name):
        return name.lower() in HIDDEN_TABLES

    def dataset_srs(self, row):
        if not row or not row.get("wkt"):
            return None
        return make_srs(row.get("authid"), row["wkt"])

    # -- generic OGR access
    def _open_ro(self, path):
        try:
            ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=self.drivers)
        except RuntimeError as e:
            raise DatasetError(_friendly(e)) from e
        if ds is None:
            raise DatasetError("Could not open: %s" % path)
        return ds

    @_guard
    def list_tables(self, path):
        ds = self._open_ro(path)
        out = []
        for i in range(ds.GetLayerCount()):
            info = layer_info(ds.GetLayerByIndex(i))
            if not self.hidden(info["name"]):
                out.append(info)
        ds = None
        return out

    def get_dataset(self, path, name):
        for d in self.list_datasets(path):
            if d["name"] == name:
                return d
        return None

    def _copy_into(self, path, src_table, new_name, srs, final_format, layer_opts, access_mode="update"):
        """Copy `src_table` into `path` as `new_name`, reprojected to `srs`.

        Goes through a temporary GeoPackage so the source file is never read and
        written at the same time.
        """
        tmpdir = tempfile.mkdtemp(prefix="featds_")
        tmp = os.path.join(tmpdir, "tmp.gpkg")
        try:
            src = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=self.drivers)
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
                    format=final_format,
                    accessMode=access_mode,
                    layerName=new_name,
                    layerCreationOptions=layer_opts,
                ),
            )
            if step2 is None:
                raise DatasetError(
                    "Could not write into the file. Make sure none of its layers "
                    "is loaded or in edit mode."
                )
            step2 = None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------- GeoPackage and SpatiaLite
class SqliteBackend(Backend):
    """GeoPackage and SpatiaLite: both are SQLite files, so the dataset
    definitions are kept in two small helper tables inside the file."""

    def __init__(self, kind):
        self.kind = kind
        if kind == "gpkg":
            self.key = "gpkg"
            self.label = "GeoPackage (.gpkg)"
            self.hint = "One .gpkg file. Datasets are stored in two helper tables inside the file."
            self.drivers = ["GPKG"]
            self.extensions = (".gpkg",)
            self.open_filter = "GeoPackage (*.gpkg)"
            self.save_filter = "GeoPackage (*.gpkg)"
            self.driver_name = "GPKG"
            self.layer_opts = ["OVERWRITE=NO"]
            self.copy_opts = []
        else:
            self.key = "spatialite"
            self.label = "SpatiaLite (.sqlite)"
            self.hint = "One SpatiaLite database file. Datasets are stored in two helper tables inside the file."
            self.drivers = ["SQLite"]
            self.extensions = (".sqlite", ".db", ".sqlite3", ".spatialite")
            self.open_filter = "SpatiaLite (*.sqlite *.db *.sqlite3 *.spatialite)"
            self.save_filter = "SpatiaLite (*.sqlite)"
            self.driver_name = "SQLite"
            self.layer_opts = ["FORMAT=SPATIALITE"]
            self.copy_opts = ["FORMAT=SPATIALITE"]

    def hidden(self, name):
        low = name.lower()
        if low in HIDDEN_TABLES:
            return True
        return self.kind == "spatialite" and low.startswith(SPATIALITE_INTERNAL)

    # -- container
    def is_valid(self, path):
        if not path or not os.path.isfile(path):
            return False
        try:
            with open(path, "rb") as f:
                if f.read(15) != b"SQLite format 3":
                    return False
            con = sqlite3.connect(path, timeout=10)
            try:
                app_id = con.execute("PRAGMA application_id").fetchone()[0]
                if self.kind == "gpkg":
                    return app_id in GPKG_APP_IDS
                if app_id in GPKG_APP_IDS:
                    return False
                row = con.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name IN "
                    "('spatial_ref_sys', 'geometry_columns')"
                ).fetchone()
                return row[0] == 2
            finally:
                con.close()
        except Exception:
            return False

    @_guard
    def create_container(self, path):
        drv = ogr.GetDriverByName(self.driver_name)
        if drv is None:
            raise DatasetError("The %s driver is not available in this QGIS/GDAL build." % self.label)
        opts = ["SPATIALITE=YES"] if self.kind == "spatialite" else []
        ds = drv.CreateDataSource(path, opts)
        if ds is None:
            raise DatasetError(
                "Could not create %s. For SpatiaLite, this GDAL build must include SpatiaLite support."
                % path
            )
        ds = None  # flush and close

    # -- helper tables
    def ensure_meta(self, path):
        con = sqlite3.connect(path, timeout=10)
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

    @staticmethod
    def _has_meta(con):
        row = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN "
            "('feature_datasets', 'feature_dataset_members')"
        ).fetchone()
        return row[0] == 2

    @_guard
    def list_datasets(self, path):
        if not self.is_valid(path):
            return []
        con = sqlite3.connect(path, timeout=10)
        try:
            if not self._has_meta(con):
                return []
            rows = con.execute(
                "SELECT name, crs_authid, crs_wkt, description FROM feature_datasets ORDER BY name"
            ).fetchall()
        finally:
            con.close()
        return [
            {"name": r[0], "authid": r[1], "wkt": r[2], "description": r[3], "pending": False}
            for r in rows
        ]

    @_guard
    def create_dataset(self, path, name, authid, wkt, description=""):
        name = self.check(name, "Dataset name")
        if not wkt:
            raise DatasetError("Please choose a valid CRS.")
        if not os.path.isfile(path):
            self.create_container(path)
        elif not self.is_valid(path):
            raise DatasetError("The selected file is not a valid %s file." % self.label)
        self.ensure_meta(path)
        if self.get_dataset(path, name):
            raise DatasetError("A dataset named '%s' already exists in this file." % name)
        con = sqlite3.connect(path, timeout=10)
        try:
            con.execute(
                "INSERT INTO feature_datasets (name, crs_authid, crs_wkt, description, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, authid or "", wkt, description or "",
                 datetime.now().isoformat(timespec="seconds")),
            )
            con.commit()
        finally:
            con.close()

    @_guard
    def list_members(self, path, dataset):
        con = sqlite3.connect(path, timeout=10)
        try:
            if not self._has_meta(con):
                return []
            rows = con.execute(
                "SELECT table_name FROM feature_dataset_members WHERE dataset_name=? ORDER BY table_name",
                (dataset,),
            ).fetchall()
        finally:
            con.close()
        return [r[0] for r in rows]

    def all_member_tables(self, path):
        con = sqlite3.connect(path, timeout=10)
        try:
            if not self._has_meta(con):
                return set()
            rows = con.execute("SELECT table_name FROM feature_dataset_members").fetchall()
        finally:
            con.close()
        return {r[0] for r in rows}

    def _add_member(self, path, dataset, table):
        self.ensure_meta(path)
        con = sqlite3.connect(path, timeout=10)
        try:
            con.execute(
                "INSERT OR IGNORE INTO feature_dataset_members (dataset_name, table_name) VALUES (?, ?)",
                (dataset, table),
            )
            con.commit()
        finally:
            con.close()

    # -- edit / delete
    def edit_capabilities(self, path, name):
        return {"rename": True, "crs": True, "reproject": True, "description": True, "note": ""}

    @_guard
    def edit_dataset(self, path, name, new_name, authid, wkt, description, reproject_layers):
        row = self.get_dataset(path, name)
        if row is None:
            raise DatasetError("Dataset '%s' was not found." % name)
        new_name = self.check(new_name or name, "Dataset name")
        if new_name != name and self.get_dataset(path, new_name):
            raise DatasetError("A dataset named '%s' already exists in this file." % new_name)
        if not wkt:
            raise DatasetError("Please choose a valid CRS.")
        old_srs = make_srs(row["authid"], row["wkt"])
        new_srs = make_srs(authid, wkt)
        changed = not same_crs(old_srs, new_srs)
        notes = []
        if changed and reproject_layers:
            members = set(self.list_members(path, name))
            todo = [
                t["name"] for t in self.list_tables(path)
                if t["name"] in members and t["has_geom"] and not same_crs(t["srs"], new_srs)
            ]
            if todo:
                notes.append("Backup saved: %s" % make_backup(path))
                for table in todo:
                    self._copy_into(path, table, table, new_srs, self.driver_name,
                                    self.copy_opts, access_mode="overwrite")
                notes.append("%d layer(s) reprojected to the new CRS." % len(todo))
        elif changed:
            notes.append("The layers keep their old CRS. Run Validate to see which ones differ.")
        con = sqlite3.connect(path, timeout=10)
        try:
            con.execute(
                "UPDATE feature_datasets SET name=?, crs_authid=?, crs_wkt=?, description=? WHERE name=?",
                (new_name, authid or "", wkt, description or "", name),
            )
            if new_name != name:
                con.execute(
                    "UPDATE feature_dataset_members SET dataset_name=? WHERE dataset_name=?",
                    (new_name, name),
                )
            con.commit()
        finally:
            con.close()
        return "Dataset '%s' updated. %s" % (new_name, " ".join(notes))

    @_guard
    def delete_dataset(self, path, name, delete_layers=False):
        members = self.list_members(path, name)
        deleted = 0
        if delete_layers and members:
            drv = ogr.GetDriverByName(self.driver_name)
            ds = drv.Open(path, 1) if drv is not None else None
            if ds is None:
                raise DatasetError(
                    "Could not open the file for writing. Remove its layers from the QGIS project "
                    "and make sure none is in edit mode."
                )
            try:
                for m in members:
                    if ds.GetLayerByName(m) is not None:
                        ds.DeleteLayer(m)
                        deleted += 1
            finally:
                ds = None
        con = sqlite3.connect(path, timeout=10)
        try:
            con.execute("DELETE FROM feature_dataset_members WHERE dataset_name=?", (name,))
            con.execute("DELETE FROM feature_datasets WHERE name=?", (name,))
            con.commit()
        finally:
            con.close()
        extra = " %d layer(s) deleted." % deleted if delete_layers else " Its layers were kept in the file."
        return "Dataset '%s' deleted.%s" % (name, extra)

    # -- layers
    @_guard
    def create_layer(self, path, dataset, name, geom_type, fields):
        name = self.check(name, "Layer name")
        row = self.get_dataset(path, dataset)
        if row is None:
            raise DatasetError("Dataset '%s' was not found." % dataset)
        validate_fields(fields)
        srs = make_srs(row["authid"], row["wkt"])
        drv = ogr.GetDriverByName(self.driver_name)
        ds = drv.Open(path, 1) if drv is not None else None
        if ds is None:
            raise DatasetError(
                "Could not open the file for writing. Make sure none of its layers "
                "is in edit mode in QGIS."
            )
        try:
            if ds.GetLayerByName(name) is not None:
                raise DatasetError("A layer named '%s' already exists in this file." % name)
            lyr = ds.CreateLayer(
                name, srs if geom_type != ogr.wkbNone else None, geom_type, self.layer_opts
            )
            if lyr is None:
                raise DatasetError("The layer could not be created.")
            for fname, ftype in fields:
                if lyr.CreateField(ogr.FieldDefn(fname.strip(), ftype)) != 0:
                    raise DatasetError("Could not create field '%s'." % fname)
            lyr = None
        finally:
            ds = None
        self._add_member(path, dataset, name)
        return name

    @_guard
    def candidate_layers(self, path, dataset):
        members = set(self.list_members(path, dataset))
        return [t for t in self.list_tables(path) if t["name"] not in members]

    @_guard
    def add_existing(self, path, dataset, table, new_name=None, reproject=False):
        if not reproject:
            self._add_member(path, dataset, table)
            return table
        row = self.get_dataset(path, dataset)
        if row is None:
            raise DatasetError("Dataset '%s' was not found." % dataset)
        new = self.check(new_name or "%s_%s" % (table, dataset), "Layer name")
        if any(t["name"] == new for t in self.list_tables(path)):
            raise DatasetError("A layer named '%s' already exists in this file." % new)
        srs = make_srs(row["authid"], row["wkt"])
        self._copy_into(path, table, new, srs, self.driver_name, self.copy_opts)
        self._add_member(path, dataset, new)
        return new

    @_guard
    def validate(self, path):
        lines, problems = [], 0
        datasets = self.list_datasets(path)
        if not datasets:
            return ["No feature datasets found in this file."], 0
        tables = {t["name"]: t for t in self.list_tables(path)}
        for d in datasets:
            srs = make_srs(d["authid"], d["wkt"])
            lines.append("Dataset: %s  (CRS: %s)" % (d["name"], describe_srs(srs)))
            members = self.list_members(path, d["name"])
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
        used = self.all_member_tables(path)
        loose = [n for n in tables if n not in used]
        if loose:
            lines.append("Layers not in any dataset: " + ", ".join(sorted(loose)))
        return lines, problems


# ------------------------------------------------------ File Geodatabase
class FileGdbBackend(Backend):
    """File Geodatabase: uses the REAL feature datasets of the geodatabase
    (folders inside the .gdb), written through GDAL's OpenFileGDB driver.

    GDAL cannot create an EMPTY feature dataset, so a newly created dataset is
    kept as a 'pending' definition (name + CRS) in a small .json file next to
    the .gdb. It becomes a real feature dataset when its first layer is created.
    """

    key = "filegdb"
    label = "File Geodatabase (.gdb)"
    hint = (
        "A .gdb folder with real ArcGIS feature datasets. Needs GDAL 3.6+ (QGIS 3.28+). "
        "A new dataset appears in the geodatabase when its first layer is created."
    )
    drivers = ["OpenFileGDB"]
    extensions = (".gdb",)
    open_filter = ""
    save_filter = "File Geodatabase (*.gdb)"
    is_directory = True

    def geom_types(self):
        return [g for g in GEOM_TYPES if g[1] != ogr.wkbNone]

    def field_types(self):
        return [f for f in FIELD_TYPES if f[1] != ogr.OFTInteger64]

    def check(self, name, what):
        name = check_name(name, what)
        if not re.match(r"^[^\W\d_]\w*$", name):
            raise DatasetError(
                "%s must start with a letter and contain only letters, digits and underscores "
                "(File Geodatabase naming rules): %s" % (what, name)
            )
        return name

    @staticmethod
    def _require_gdal():
        try:
            version = int(gdal.VersionInfo())
        except Exception:
            version = 0
        if version < MIN_GDAL_FGDB:
            raise DatasetError(
                "Editing a File Geodatabase needs GDAL 3.6 or newer (QGIS 3.28 or newer)."
            )
        if ogr.GetDriverByName("OpenFileGDB") is None:
            raise DatasetError("The OpenFileGDB driver is not available in this QGIS/GDAL build.")

    def is_valid(self, path):
        if not path or not path.lower().endswith(".gdb") or not os.path.isdir(path):
            return False
        with contextlib.suppress(OSError):
            return any(n == "gdb" or n.startswith("a0000000") for n in os.listdir(path))
        return False

    @_guard
    def create_container(self, path):
        self._require_gdal()
        ds = ogr.GetDriverByName("OpenFileGDB").CreateDataSource(path)
        if ds is None:
            raise DatasetError("Could not create the geodatabase: %s" % path)
        ds = None

    # -- pending dataset definitions (sidecar json)
    @staticmethod
    def _pending_file(path):
        return path.rstrip("/\\") + ".datasets.json"

    def _read_pending(self, path):
        with contextlib.suppress(OSError, ValueError):
            with open(self._pending_file(path), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        return {}

    def _write_pending(self, path, data):
        target = self._pending_file(path)
        if not data:
            with contextlib.suppress(OSError):
                os.remove(target)
            return
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # -- reading the real feature datasets (GDAL exposes them as groups)
    def _open_ro(self, path):
        for flag in (gdal.OF_READONLY, gdal.OF_UPDATE):
            ds = None
            with contextlib.suppress(RuntimeError):
                ds = gdal.OpenEx(path, gdal.OF_VECTOR | flag, allowed_drivers=self.drivers)
            if ds is not None:
                return ds
        raise DatasetError(
            "Could not open the geodatabase: %s. Check that it is a valid .gdb folder "
            "and that the OpenFileGDB driver is available." % path
        )

    def _scan(self, path):
        ds = self._open_ro(path)
        out = {"datasets": {}, "root": [], "supported": False}
        root = None
        with contextlib.suppress(Exception):
            root = ds.GetRootGroup()
        if root is None:
            for i in range(ds.GetLayerCount()):
                out["root"].append(layer_info(ds.GetLayerByIndex(i)))
            ds = None
            return out
        out["supported"] = True
        for lname in root.GetVectorLayerNames() or []:
            lyr = root.OpenVectorLayer(lname)
            if lyr is not None:
                out["root"].append(layer_info(lyr))
        for gname in root.GetGroupNames() or []:
            grp = root.OpenGroup(gname)
            layers = []
            if grp is not None:
                for lname in grp.GetVectorLayerNames() or []:
                    lyr = grp.OpenVectorLayer(lname)
                    if lyr is not None:
                        layers.append(layer_info(lyr))
            out["datasets"][gname] = layers
        ds = None
        return out

    @staticmethod
    def _first_srs(layers):
        for t in layers:
            if t["has_geom"] and t["srs"] is not None:
                return t["srs"]
        return None

    @_guard
    def list_datasets(self, path):
        if not self.is_valid(path):
            return []
        real = self._scan(path)["datasets"]
        pend = self._read_pending(path)
        out = []
        for name in sorted(set(real) | set(pend)):
            entry = pend.get(name, {})
            if name in real:
                srs = self._first_srs(real[name])
                out.append(
                    {
                        "name": name,
                        "authid": describe_srs(srs) if srs is not None else entry.get("authid", ""),
                        "wkt": srs.ExportToWkt() if srs is not None else entry.get("wkt", ""),
                        "description": entry.get("description", ""),
                        "pending": False,
                    }
                )
            else:
                out.append(
                    {
                        "name": name,
                        "authid": entry.get("authid", ""),
                        "wkt": entry.get("wkt", ""),
                        "description": entry.get("description", ""),
                        "pending": True,
                    }
                )
        return out

    @_guard
    def list_members(self, path, dataset):
        layers = self._scan(path)["datasets"].get(dataset, [])
        return sorted(t["name"] for t in layers)

    @_guard
    def candidate_layers(self, path, dataset):
        return [t for t in self._scan(path)["root"] if t["has_geom"]]

    def _create_layer_in(self, path, dataset, name, geom_type, srs, fields):
        name = self.check(name, "Layer name")
        validate_fields(fields)
        ds = ogr.GetDriverByName("OpenFileGDB").Open(path, 1)
        if ds is None:
            raise DatasetError("Could not open the geodatabase for writing.")
        try:
            if ds.GetLayerByName(name) is not None:
                raise DatasetError("A layer named '%s' already exists in this geodatabase." % name)
            lyr = ds.CreateLayer(name, srs, geom_type, ["FEATURE_DATASET=" + dataset])
            if lyr is None:
                raise DatasetError("The layer could not be created.")
            for fname, ftype in fields:
                if lyr.CreateField(ogr.FieldDefn(fname.strip(), ftype)) != 0:
                    raise DatasetError("Could not create field '%s'." % fname)
            lyr = None
        finally:
            ds = None
        return name

    @_guard
    def create_dataset(self, path, name, authid, wkt, description=""):
        self._require_gdal()
        name = self.check(name, "Dataset name")
        if not wkt:
            raise DatasetError("Please choose a valid CRS.")
        if os.path.isdir(path) and not self.is_valid(path):
            if os.listdir(path):
                raise DatasetError("The folder exists but is not a File Geodatabase: %s" % path)
            os.rmdir(path)  # leftover empty folder from an earlier attempt
        if self.is_valid(path) and self.get_dataset(path, name):
            raise DatasetError("A dataset named '%s' already exists in this geodatabase." % name)
        if not os.path.isdir(path):
            self.create_container(path)
        pend = self._read_pending(path)
        pend[name] = {"authid": authid or "", "wkt": wkt, "description": description or ""}
        self._write_pending(path, pend)

    @_guard
    def create_layer(self, path, dataset, name, geom_type, fields):
        self._require_gdal()
        row = self.get_dataset(path, dataset)
        if row is None:
            raise DatasetError("Dataset '%s' was not found." % dataset)
        if not row["wkt"]:
            raise DatasetError(
                "This dataset has no layers yet, so its CRS is unknown. "
                "Create a new dataset (with its CRS) instead."
            )
        return self._create_layer_in(path, dataset, name, geom_type, make_srs(row["authid"], row["wkt"]), fields)

    @_guard
    def add_existing(self, path, dataset, table, new_name=None, reproject=True):
        """A layer can live in one dataset only, so the layer is copied into it."""
        self._require_gdal()
        row = self.get_dataset(path, dataset)
        if row is None or not row["wkt"]:
            raise DatasetError("Dataset '%s' has no CRS yet." % dataset)
        new = self.check(new_name or "%s_%s" % (table, dataset), "Layer name")
        if any(t["name"] == new for t in self.list_tables(path)):
            raise DatasetError("A layer named '%s' already exists in this geodatabase." % new)
        srs = make_srs(row["authid"], row["wkt"])
        self._copy_into(path, table, new, srs, "OpenFileGDB", ["FEATURE_DATASET=" + dataset])
        return new

    # -- edit / delete
    def edit_capabilities(self, path, name):
        row = self.get_dataset(path, name)
        if row is not None and row.get("pending"):
            return {"rename": True, "crs": True, "reproject": False, "description": True,
                    "note": "This dataset has no layers yet, so it can be freely renamed or given another CRS."}
        return {
            "rename": False, "crs": False, "reproject": False, "description": True,
            "note": (
                "A feature dataset that already exists in the geodatabase cannot be renamed or "
                "re-projected through GDAL. To change its CRS: create a new dataset with the new CRS, "
                "use 'Add Existing Layer...' (it copies layers reprojected), then delete the old one."
            ),
        }

    @_guard
    def edit_dataset(self, path, name, new_name, authid, wkt, description, reproject_layers):
        row = self.get_dataset(path, name)
        if row is None:
            raise DatasetError("Dataset '%s' was not found." % name)
        caps = self.edit_capabilities(path, name)
        pend = self._read_pending(path)
        if caps["rename"]:
            new_name = self.check(new_name or name, "Dataset name")
            if new_name != name and self.get_dataset(path, new_name):
                raise DatasetError("A dataset named '%s' already exists." % new_name)
            if not wkt:
                raise DatasetError("Please choose a valid CRS.")
            pend.pop(name, None)
            pend[new_name] = {"authid": authid or "", "wkt": wkt, "description": description or ""}
            self._write_pending(path, pend)
            return "Dataset '%s' updated." % new_name
        if (new_name or name) != name or not same_crs(make_srs(row["authid"], row["wkt"]), make_srs(authid, wkt)):
            raise DatasetError(caps["note"])
        entry = pend.get(name, {"authid": row["authid"], "wkt": row["wkt"]})
        entry["description"] = description or ""
        pend[name] = entry
        self._write_pending(path, pend)
        return "Description of '%s' updated." % name

    @_guard
    def delete_dataset(self, path, name, delete_layers=True):
        row = self.get_dataset(path, name)
        if row is None:
            raise DatasetError("Dataset '%s' was not found." % name)
        deleted = 0
        if not row.get("pending"):
            members = self.list_members(path, name)
            ds = ogr.GetDriverByName("OpenFileGDB").Open(path, 1)
            if ds is None:
                raise DatasetError(
                    "Could not open the geodatabase for writing. Remove its layers from the QGIS "
                    "project and close other programs using it."
                )
            try:
                for m in members:
                    if ds.GetLayerByName(m) is not None:
                        ds.DeleteLayer(m)
                        deleted += 1
            finally:
                ds = None
        pend = self._read_pending(path)
        pend.pop(name, None)
        self._write_pending(path, pend)
        msg = "Dataset '%s' deleted (%d layer(s) removed)." % (name, deleted)
        if any(d["name"] == name for d in self.list_datasets(path)):
            msg += (
                " An empty dataset folder still exists in the geodatabase "
                "(GDAL cannot remove it); delete it in ArcGIS Pro if you do not want it."
            )
        return msg

    @_guard
    def validate(self, path):
        scan = self._scan(path)
        datasets = self.list_datasets(path)
        if not datasets:
            return ["No feature datasets found in this geodatabase."], 0
        if not scan["supported"] and not any(d["pending"] for d in datasets):
            return [
                "This GDAL version cannot list the feature datasets of a File Geodatabase "
                "(a newer QGIS is needed)."
            ], 0
        lines, problems = [], 0
        for d in datasets:
            layers = scan["datasets"].get(d["name"], [])
            srs = self._first_srs(layers) if layers else (
                make_srs(d["authid"], d["wkt"]) if d["wkt"] else None
            )
            suffix = "  - pending, created with its first layer" if d["pending"] else ""
            lines.append("Dataset: %s  (CRS: %s)%s" % (d["name"], describe_srs(srs), suffix))
            if not layers and not d["pending"]:
                lines.append("    (no layers)")
            for t in layers:
                if not t["has_geom"] or same_crs(t["srs"], srs):
                    lines.append("    [OK] %s" % t["name"])
                else:
                    lines.append(
                        "    [X] %s - CRS is %s, dataset CRS is %s"
                        % (t["name"], describe_srs(t["srs"]), describe_srs(srs))
                    )
                    problems += 1
            lines.append("")
        loose = [t["name"] for t in scan["root"]]
        if loose:
            lines.append("Layers not in any dataset: " + ", ".join(sorted(loose)))
        return lines, problems


BACKENDS = [SqliteBackend("gpkg"), SqliteBackend("spatialite"), FileGdbBackend()]
