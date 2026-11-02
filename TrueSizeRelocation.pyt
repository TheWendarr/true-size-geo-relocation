# -*- coding: utf-8 -*-
"""
True Size Relocation toolbox for ArcGIS Pro.

Moves every feature class, raster, and table in a file geodatabase from one
place on Earth to another, keeping true ground size, shape, and orientation
around a chosen origin point.

How it works
------------
A "samples" point feature class holds two points: a source (origin) and a
destination. Each dataset is:

    1. Projected into an Azimuthal Equidistant (AEQD) projection centered on
       the source point. In AEQD, distance and bearing from the center are true.
    2. Relabeled (Define Projection) as AEQD centered on the destination point.
       Only metadata changes here, so the x/y values stay the same.
    3. Projected from that relabeled AEQD into the output coordinate system.

The result is the same data drawn at its real size, centered on the
destination point. Example: source = Bangkok, destination = Pikes Peak.

Requires: ArcGIS Pro 3.x, Basic license or higher.
"""

import datetime
import math
import os

import arcpy

TOOL_VERSION = "1.0.0"

WGS84 = 4326
SAMPLE_FC_DEFAULT = "samples"
PREFERRED_ID_FIELDS = ("role", "type", "point_type", "name", "label", "id")

KIND_FC = "Feature Class"
KIND_RASTER = "Raster"
KIND_TABLE = "Table"
SEP = ": "

RESAMPLING_CHOICES = ["AUTO", "NEAREST", "BILINEAR", "CUBIC", "MAJORITY"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def aeqd_sr(lat, lon, name):
    """Azimuthal Equidistant spatial reference on WGS 84, centered at lat/lon."""
    wkt = (
        'PROJCS["{name}",'
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
        'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Azimuthal_Equidistant"],'
        'PARAMETER["False_Easting",0.0],'
        'PARAMETER["False_Northing",0.0],'
        'PARAMETER["Central_Meridian",{lon:.10f}],'
        'PARAMETER["Latitude_Of_Origin",{lat:.10f}],'
        'UNIT["Meter",1.0]]'
    ).format(name=name, lat=lat, lon=lon)
    sr = arcpy.SpatialReference()
    sr.loadFromString(wkt)
    return sr


def utm_sr_for(lat, lon):
    """WGS 84 UTM zone spatial reference for a point (standard 6-degree zones)."""
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    zone = min(max(zone, 1), 60)
    code = (32600 if lat >= 0 else 32700) + zone
    return arcpy.SpatialReference(code)


def same_gcs(sr_a, sr_b):
    """True if two spatial references share a geographic coordinate system."""
    try:
        return sr_a.GCS.factoryCode == sr_b.GCS.factoryCode and sr_a.GCS.factoryCode != 0
    except Exception:
        return False


def pick_transform(from_sr, to_sr, extent=None):
    """First suggested geographic transformation, or '' if none is needed."""
    if from_sr is None or to_sr is None or same_gcs(from_sr, to_sr):
        return ""
    try:
        if extent is not None:
            options = arcpy.ListTransformations(from_sr, to_sr, extent)
        else:
            options = arcpy.ListTransformations(from_sr, to_sr)
    except Exception:
        options = []
    return options[0] if options else ""


def list_gdb_contents(gdb):
    """Return [(kind, relative_path, feature_dataset_or_None), ...] for a GDB."""
    items = []
    if not gdb or not arcpy.Exists(gdb):
        return items
    prev_ws = arcpy.env.workspace
    try:
        arcpy.env.workspace = gdb
        for fc in sorted(arcpy.ListFeatureClasses() or []):
            items.append((KIND_FC, fc, None))
        for fd in sorted(arcpy.ListDatasets(feature_type="Feature") or []):
            for fc in sorted(arcpy.ListFeatureClasses(feature_dataset=fd) or []):
                items.append((KIND_FC, fd + "\\" + fc, fd))
        for ras in sorted(arcpy.ListRasters() or []):
            items.append((KIND_RASTER, ras, None))
        for tbl in sorted(arcpy.ListTables() or []):
            items.append((KIND_TABLE, tbl, None))
    finally:
        arcpy.env.workspace = prev_ws
    return items


def item_label(kind, rel_path):
    return kind + SEP + rel_path


def parse_item_label(label):
    kind, _, rel_path = label.partition(SEP)
    return kind, rel_path


def read_sample_points(samples_fc, id_field):
    """Return {str(value): PointGeometry} for every point in the samples FC."""
    out = {}
    with arcpy.da.SearchCursor(samples_fc, [id_field, "SHAPE@"]) as cur:
        for val, geom in cur:
            if geom is None:
                continue
            out[str(val)] = geom
    return out


def to_wgs84_latlon(point_geom):
    """Project a PointGeometry to WGS 84 and return (lat, lon)."""
    wgs = arcpy.SpatialReference(WGS84)
    src_sr = point_geom.spatialReference
    if src_sr is None or src_sr.name == "Unknown":
        raise ValueError("Samples feature class has no spatial reference.")
    if src_sr.factoryCode == WGS84:
        p = point_geom.firstPoint
        return p.Y, p.X
    xform = pick_transform(src_sr, wgs)
    pg = point_geom.projectAs(wgs, xform) if xform else point_geom.projectAs(wgs)
    p = pg.firstPoint
    return p.Y, p.X


def auto_id_field(samples_fc):
    """Pick a sensible identifier field: a preferred name, else the OID field."""
    fields = arcpy.ListFields(samples_fc)
    by_lower = {f.name.lower(): f.name for f in fields}
    for cand in PREFERRED_ID_FIELDS:
        if cand in by_lower:
            return by_lower[cand]
    for f in fields:
        if f.type == "OID":
            return f.name
    return None


def safe_unique_scratch_gdb():
    folder = arcpy.env.scratchFolder
    name = "tsr_scratch_{}.gdb".format(datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    arcpy.management.CreateFileGDB(folder, name)
    return os.path.join(folder, name)


def max_distance_from_center_km(dataset):
    """Largest distance (km) from an AEQD center to a corner of the dataset extent."""
    try:
        ext = arcpy.Describe(dataset).extent
        corners = [(ext.XMin, ext.YMin), (ext.XMin, ext.YMax),
                   (ext.XMax, ext.YMin), (ext.XMax, ext.YMax)]
        return max(math.hypot(x, y) for x, y in corners) / 1000.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Toolbox
# ---------------------------------------------------------------------------
class Toolbox(object):
    def __init__(self):
        self.label = "True Size Relocation"
        self.alias = "truesize"
        self.description = (
            "Tools that move geospatial data to a different place on Earth at its "
            "true size, for size comparisons and scenario planning."
        )
        self.tools = [RelocateGeodatabase]


class RelocateGeodatabase(object):
    # Parameter indexes
    P_IN_GDB = 0
    P_SAMPLES = 1
    P_ID_FIELD = 2
    P_SOURCE = 3
    P_DEST = 4
    P_OUT_FOLDER = 5
    P_OUT_NAME = 6
    P_OUT_SR = 7
    P_DATASETS = 8
    P_RESAMPLING = 9
    P_CELL_SIZE = 10
    P_MAX_DEV = 11
    P_OUT_GDB = 12

    def __init__(self):
        self.label = "Relocate Geodatabase (True Size)"
        self.description = (
            "Copies every selected feature class, raster, and table from a file "
            "geodatabase into a new file geodatabase, moving the data so the "
            "source sample point lands on the destination sample point while "
            "keeping true ground size, shape, and orientation."
        )
        self.category = "Relocation"
        self.canRunInBackground = False

    # ------------------------------------------------------------------ params
    def getParameterInfo(self):
        in_gdb = arcpy.Parameter(
            displayName="Input File Geodatabase",
            name="in_gdb",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input",
        )
        in_gdb.filter.list = ["Local Database"]

        samples = arcpy.Parameter(
            displayName="Samples Feature Class",
            name="samples_fc",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Input",
        )
        samples.filter.list = ["Point"]

        id_field = arcpy.Parameter(
            displayName="Point Identifier Field",
            name="id_field",
            datatype="Field",
            parameterType="Required",
            direction="Input",
        )
        id_field.parameterDependencies = [samples.name]
        id_field.filter.list = ["Short", "Long", "Text", "OID"]

        source = arcpy.Parameter(
            displayName="Source (Origin) Point",
            name="source_point",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        source.filter.type = "ValueList"
        source.filter.list = []

        dest = arcpy.Parameter(
            displayName="Destination Point",
            name="destination_point",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        dest.filter.type = "ValueList"
        dest.filter.list = []

        out_folder = arcpy.Parameter(
            displayName="Output Folder",
            name="out_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input",
        )

        out_name = arcpy.Parameter(
            displayName="Output Geodatabase Name",
            name="out_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )

        out_sr = arcpy.Parameter(
            displayName="Output Coordinate System",
            name="out_coor_system",
            datatype="GPSpatialReference",
            parameterType="Required",
            direction="Input",
        )

        datasets = arcpy.Parameter(
            displayName="Datasets to Relocate",
            name="datasets",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
            multiValue=True,
        )
        datasets.filter.type = "ValueList"
        datasets.filter.list = []

        resampling = arcpy.Parameter(
            displayName="Raster Resampling Technique",
            name="resampling_type",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Raster Options",
        )
        resampling.filter.type = "ValueList"
        resampling.filter.list = RESAMPLING_CHOICES
        resampling.value = "AUTO"

        cell_size = arcpy.Parameter(
            displayName="Output Raster Cell Size",
            name="cell_size",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Raster Options",
        )
        cell_size.filter.type = "Range"
        cell_size.filter.list = [1e-9, 1e12]

        max_dev = arcpy.Parameter(
            displayName="Maximum Shape Deviation",
            name="max_deviation",
            datatype="GPLinearUnit",
            parameterType="Optional",
            direction="Input",
            category="Vector Options",
        )
        max_dev.value = "10 Meters"

        out_gdb = arcpy.Parameter(
            displayName="Output Geodatabase",
            name="out_gdb",
            datatype="DEWorkspace",
            parameterType="Derived",
            direction="Output",
        )

        return [in_gdb, samples, id_field, source, dest, out_folder, out_name,
                out_sr, datasets, resampling, cell_size, max_dev, out_gdb]

    def isLicensed(self):
        return True

    # -------------------------------------------------------------- validation
    def updateParameters(self, parameters):
        p = parameters
        gdb = p[self.P_IN_GDB].valueAsText

        # New input GDB: default samples FC, output name, folder, dataset list
        if p[self.P_IN_GDB].altered and not p[self.P_IN_GDB].hasBeenValidated and gdb:
            cand = os.path.join(gdb, SAMPLE_FC_DEFAULT)
            if arcpy.Exists(cand):
                p[self.P_SAMPLES].value = cand
            base = os.path.splitext(os.path.basename(gdb.rstrip("\\/")))[0]
            p[self.P_OUT_NAME].value = base + "_relocated.gdb"
            if not p[self.P_OUT_FOLDER].value:
                p[self.P_OUT_FOLDER].value = os.path.dirname(gdb.rstrip("\\/"))

            items = list_gdb_contents(gdb)
            labels = [item_label(k, r) for k, r, _ in items]
            p[self.P_DATASETS].filter.list = labels
            samples_path = (p[self.P_SAMPLES].valueAsText or "").lower()
            defaults = []
            for k, r, _ in items:
                full = os.path.join(gdb, r).lower()
                if k == KIND_FC and full == samples_path:
                    continue
                defaults.append(item_label(k, r))
            p[self.P_DATASETS].values = defaults
        elif gdb and not p[self.P_DATASETS].filter.list:
            items = list_gdb_contents(gdb)
            p[self.P_DATASETS].filter.list = [item_label(k, r) for k, r, _ in items]

        # Identifier field default
        samples = p[self.P_SAMPLES].valueAsText
        if samples and arcpy.Exists(samples):
            field_names = [f.name for f in arcpy.ListFields(samples)]
            if not p[self.P_ID_FIELD].valueAsText or p[self.P_ID_FIELD].valueAsText not in field_names:
                auto = auto_id_field(samples)
                if auto:
                    p[self.P_ID_FIELD].value = auto

        # Source / destination dropdowns from the identifier field values
        field = p[self.P_ID_FIELD].valueAsText
        if samples and field and arcpy.Exists(samples):
            try:
                pts = read_sample_points(samples, field)
                values = list(pts.keys())
            except Exception:
                values = []
            p[self.P_SOURCE].filter.list = values
            p[self.P_DEST].filter.list = values
            if values:
                if p[self.P_SOURCE].valueAsText not in values:
                    p[self.P_SOURCE].value = values[0]
                if p[self.P_DEST].valueAsText not in values:
                    p[self.P_DEST].value = values[1] if len(values) > 1 else values[0]

        # Default output coordinate system: UTM zone of the destination point
        dest_changed = (p[self.P_DEST].altered and not p[self.P_DEST].hasBeenValidated)
        if samples and field and p[self.P_DEST].valueAsText and (dest_changed or not p[self.P_OUT_SR].value):
            try:
                pts = read_sample_points(samples, field)
                geom = pts.get(p[self.P_DEST].valueAsText)
                if geom is not None:
                    lat, lon = to_wgs84_latlon(geom)
                    p[self.P_OUT_SR].value = utm_sr_for(lat, lon)
            except Exception:
                pass

        # Output name should end with .gdb
        name = p[self.P_OUT_NAME].valueAsText
        if name and not name.lower().endswith(".gdb"):
            p[self.P_OUT_NAME].value = name + ".gdb"
        return

    def updateMessages(self, parameters):
        p = parameters
        gdb = p[self.P_IN_GDB].valueAsText
        if gdb and not gdb.lower().rstrip("\\/").endswith(".gdb"):
            p[self.P_IN_GDB].setErrorMessage("Input must be a file geodatabase (.gdb).")

        samples = p[self.P_SAMPLES].valueAsText
        if samples and arcpy.Exists(samples):
            desc = arcpy.Describe(samples)
            if desc.shapeType != "Point":
                p[self.P_SAMPLES].setErrorMessage("Samples must be a point feature class.")
            elif desc.spatialReference is None or desc.spatialReference.name == "Unknown":
                p[self.P_SAMPLES].setErrorMessage("Samples feature class has no spatial reference defined.")
            else:
                count = int(arcpy.management.GetCount(samples)[0])
                if count < 2:
                    p[self.P_SAMPLES].setErrorMessage(
                        "Samples must contain at least 2 points (source and destination); found {}.".format(count))
                elif count > 2:
                    p[self.P_SAMPLES].setWarningMessage(
                        "Samples contains {} points; only the chosen source and destination are used.".format(count))

        field = p[self.P_ID_FIELD].valueAsText
        if samples and field and arcpy.Exists(samples):
            try:
                vals = []
                with arcpy.da.SearchCursor(samples, [field]) as cur:
                    vals = [str(r[0]) for r in cur]
                if len(set(vals)) != len(vals):
                    p[self.P_ID_FIELD].setErrorMessage(
                        "Values in this field are not unique, so the source and destination points cannot be told apart. Choose another field.")
            except Exception:
                pass

        src = p[self.P_SOURCE].valueAsText
        dst = p[self.P_DEST].valueAsText
        if src and dst and src == dst:
            p[self.P_DEST].setErrorMessage("Destination point must be different from the source point.")

        folder = p[self.P_OUT_FOLDER].valueAsText
        name = p[self.P_OUT_NAME].valueAsText
        if folder and name:
            out_path = os.path.join(folder, name)
            if arcpy.Exists(out_path) and not arcpy.env.overwriteOutput:
                p[self.P_OUT_NAME].setErrorMessage(
                    "{} already exists. Choose a new name or enable overwriting outputs.".format(out_path))
            if gdb and os.path.normcase(os.path.abspath(out_path)) == os.path.normcase(os.path.abspath(gdb)):
                p[self.P_OUT_NAME].setErrorMessage("Output geodatabase cannot be the input geodatabase.")

        if p[self.P_DATASETS].values is not None and len(p[self.P_DATASETS].values) == 0:
            p[self.P_DATASETS].setErrorMessage("Select at least one dataset to relocate.")

        out_sr = p[self.P_OUT_SR].value
        if out_sr is not None:
            try:
                if out_sr.name == "Unknown":
                    p[self.P_OUT_SR].setErrorMessage("Choose a defined output coordinate system.")
            except Exception:
                pass

        cell = p[self.P_CELL_SIZE].value
        if cell is not None and cell <= 0:
            p[self.P_CELL_SIZE].setErrorMessage("Cell size must be greater than zero.")
        return

    # --------------------------------------------------------------- execute
    def execute(self, parameters, messages):
        p = parameters
        in_gdb = p[self.P_IN_GDB].valueAsText
        samples = p[self.P_SAMPLES].valueAsText
        id_field = p[self.P_ID_FIELD].valueAsText
        src_val = p[self.P_SOURCE].valueAsText
        dst_val = p[self.P_DEST].valueAsText
        out_folder = p[self.P_OUT_FOLDER].valueAsText
        out_name = p[self.P_OUT_NAME].valueAsText
        out_sr = p[self.P_OUT_SR].value
        selected = list(p[self.P_DATASETS].values or [])
        resampling = (p[self.P_RESAMPLING].valueAsText or "AUTO").upper()
        user_cell = p[self.P_CELL_SIZE].value
        max_dev = p[self.P_MAX_DEV].valueAsText or "10 Meters"

        # --- anchor points
        pts = read_sample_points(samples, id_field)
        if src_val not in pts or dst_val not in pts:
            raise arcpy.ExecuteError("Could not find the chosen source and destination points in {}.".format(samples))
        src_lat, src_lon = to_wgs84_latlon(pts[src_val])
        dst_lat, dst_lon = to_wgs84_latlon(pts[dst_val])
        arcpy.AddMessage("Source point '{}': {:.6f}, {:.6f}".format(src_val, src_lat, src_lon))
        arcpy.AddMessage("Destination point '{}': {:.6f}, {:.6f}".format(dst_val, dst_lat, dst_lon))

        sr_src = aeqd_sr(src_lat, src_lon, "AEQD_Source_{}".format(src_val)[:60])
        sr_dst = aeqd_sr(dst_lat, dst_lon, "AEQD_Destination_{}".format(dst_val)[:60])
        arcpy.AddMessage("Output coordinate system: {}".format(out_sr.name))

        # --- output and scratch workspaces
        out_gdb = os.path.join(out_folder, out_name)
        if arcpy.Exists(out_gdb):
            arcpy.management.Delete(out_gdb)
        arcpy.management.CreateFileGDB(out_folder, out_name)
        scratch = safe_unique_scratch_gdb()

        out_meters_per_unit = None
        try:
            if out_sr.type == "Projected":
                out_meters_per_unit = out_sr.metersPerUnit
        except Exception:
            pass

        created_fds = set()
        done, skipped = [], []
        arcpy.SetProgressor("step", "Relocating datasets...", 0, max(len(selected), 1), 1)
        try:
            for i, label in enumerate(selected):
                kind, rel = parse_item_label(label)
                in_path = os.path.join(in_gdb, rel)
                arcpy.SetProgressorLabel("Relocating {} ({}/{})".format(rel, i + 1, len(selected)))
                try:
                    if not arcpy.Exists(in_path):
                        raise RuntimeError("dataset not found")
                    if kind == KIND_FC:
                        self._relocate_fc(in_path, rel, out_gdb, scratch, sr_src, sr_dst, out_sr,
                                          max_dev, created_fds)
                    elif kind == KIND_RASTER:
                        self._relocate_raster(in_path, rel, out_gdb, scratch, sr_src, sr_dst, out_sr,
                                              resampling, user_cell, out_meters_per_unit)
                    elif kind == KIND_TABLE:
                        arcpy.management.Copy(in_path, os.path.join(out_gdb, rel))
                        arcpy.AddMessage("Copied table {}".format(rel))
                    else:
                        raise RuntimeError("unsupported item type")
                    done.append(label)
                except Exception as exc:
                    skipped.append("{} ({})".format(label, exc))
                    arcpy.AddWarning("Skipped {}: {}".format(label, exc))
                arcpy.SetProgressorPosition()

            self._write_info_table(out_gdb, src_val, src_lat, src_lon, dst_val, dst_lat, dst_lon,
                                   sr_src, sr_dst, out_sr, resampling, max_dev, done, skipped)
        finally:
            arcpy.ResetProgressor()
            try:
                arcpy.management.Delete(scratch)
            except Exception:
                arcpy.AddWarning("Could not delete scratch geodatabase {}".format(scratch))

        arcpy.AddMessage("Relocated {} dataset(s); skipped {}.".format(len(done), len(skipped)))
        p[self.P_OUT_GDB].value = out_gdb
        return

    # --------------------------------------------------------------- vectors
    def _relocate_fc(self, in_fc, rel, out_gdb, scratch, sr_src, sr_dst, out_sr, max_dev, created_fds):
        desc = arcpy.Describe(in_fc)
        in_sr = desc.spatialReference
        if in_sr is None or in_sr.name == "Unknown":
            raise RuntimeError("no spatial reference defined")
        if getattr(desc, "featureType", "Simple") not in ("Simple",):
            raise RuntimeError("feature type '{}' is not supported".format(desc.featureType))

        base = rel.replace("/", "\\").split("\\")[-1]
        tmp = arcpy.CreateUniqueName("fc_" + base, scratch)

        # 1. into AEQD centered on the source point
        xform_in = pick_transform(in_sr, sr_src, desc.extent)
        if xform_in:
            arcpy.AddMessage("  {}: geographic transformation {}".format(rel, xform_in))
        arcpy.management.Project(in_fc, tmp, sr_src, xform_in or None, None,
                                 "PRESERVE_SHAPE", max_dev, "NO_VERTICAL")
        self._distance_warning(tmp, rel)

        # 2. relabel as AEQD centered on the destination point
        arcpy.management.DefineProjection(tmp, sr_dst)

        # 3. into the output coordinate system
        fd = rel.split("\\")[0] if "\\" in rel else None
        if fd:
            if fd not in created_fds:
                arcpy.management.CreateFeatureDataset(out_gdb, fd, out_sr)
                created_fds.add(fd)
            out_fc = os.path.join(out_gdb, fd, base)
        else:
            out_fc = os.path.join(out_gdb, base)
        xform_out = pick_transform(sr_dst, out_sr, arcpy.Describe(tmp).extent)
        arcpy.management.Project(tmp, out_fc, out_sr, xform_out or None, None,
                                 "PRESERVE_SHAPE", max_dev, "NO_VERTICAL")
        arcpy.management.Delete(tmp)
        arcpy.AddMessage("Relocated feature class {}".format(rel))

    # --------------------------------------------------------------- rasters
    def _relocate_raster(self, in_ras, rel, out_gdb, scratch, sr_src, sr_dst, out_sr,
                         resampling, user_cell, out_meters_per_unit):
        desc = arcpy.Describe(in_ras)
        in_sr = desc.spatialReference
        if in_sr is None or in_sr.name == "Unknown":
            raise RuntimeError("no spatial reference defined")

        ras = arcpy.Raster(in_ras)
        if resampling == "AUTO":
            method = "NEAREST" if ras.isInteger else "BILINEAR"
        else:
            method = resampling

        # Native cell size in meters, used to keep resolution through the AEQD step
        try:
            if in_sr.type == "Geographic":
                # north-south cell spacing in meters (1 degree of latitude ~ 111,320 m)
                cell_m = ras.meanCellHeight * 111320.0
            else:
                cell_m = ras.meanCellHeight * in_sr.metersPerUnit
        except Exception:
            cell_m = None

        base = rel.replace("/", "\\").split("\\")[-1]
        tmp = arcpy.CreateUniqueName("ras_" + base, scratch)

        # 1. into AEQD centered on the source point
        xform_in = pick_transform(in_sr, sr_src, desc.extent)
        arcpy.management.ProjectRaster(in_ras, tmp, sr_src, method,
                                       "{0} {0}".format(cell_m) if cell_m else None,
                                       xform_in or None)
        self._distance_warning(tmp, rel)

        # 2. relabel as AEQD centered on the destination point
        arcpy.management.DefineProjection(tmp, sr_dst)

        # 3. into the output coordinate system
        if user_cell:
            out_cell = "{0} {0}".format(user_cell)
        elif cell_m and out_meters_per_unit:
            c = cell_m / out_meters_per_unit
            out_cell = "{0} {0}".format(c)
        else:
            out_cell = None
        xform_out = pick_transform(sr_dst, out_sr, arcpy.Describe(tmp).extent)
        arcpy.management.ProjectRaster(tmp, os.path.join(out_gdb, base), out_sr, method,
                                       out_cell, xform_out or None)
        arcpy.management.Delete(tmp)
        arcpy.AddMessage("Relocated raster {} ({} resampling)".format(rel, method))

    # --------------------------------------------------------------- utility
    @staticmethod
    def _distance_warning(aeqd_dataset, rel):
        km = max_distance_from_center_km(aeqd_dataset)
        if km is not None and km > 2500:
            arcpy.AddWarning(
                "  {} reaches about {:,.0f} km from the source point. Size and shape are exact at "
                "the source point; distortion grows with distance, so far edges are less exact.".format(rel, km))

    @staticmethod
    def _write_info_table(out_gdb, src_val, src_lat, src_lon, dst_val, dst_lat, dst_lon,
                          sr_src, sr_dst, out_sr, resampling, max_dev, done, skipped):
        tbl = os.path.join(out_gdb, "RELOCATION_INFO")
        arcpy.management.CreateTable(out_gdb, "RELOCATION_INFO")
        arcpy.management.AddField(tbl, "ITEM", "TEXT", field_length=64)
        arcpy.management.AddField(tbl, "VALUE", "TEXT", field_length=2000)
        rows = [
            ("tool_version", TOOL_VERSION),
            ("run_time", datetime.datetime.now().isoformat(timespec="seconds")),
            ("source_point", src_val),
            ("source_lat_lon_wgs84", "{:.8f}, {:.8f}".format(src_lat, src_lon)),
            ("destination_point", dst_val),
            ("destination_lat_lon_wgs84", "{:.8f}, {:.8f}".format(dst_lat, dst_lon)),
            ("method", "Project to AEQD(source) > Define Projection AEQD(destination) > Project to output"),
            ("aeqd_source_wkt", sr_src.exportToString()[:2000]),
            ("aeqd_destination_wkt", sr_dst.exportToString()[:2000]),
            ("output_coordinate_system", out_sr.name),
            ("raster_resampling", resampling),
            ("vector_max_deviation", max_dev),
            ("relocated", "; ".join(done)[:2000]),
            ("skipped", "; ".join(skipped)[:2000]),
        ]
        with arcpy.da.InsertCursor(tbl, ["ITEM", "VALUE"]) as cur:
            for r in rows:
                cur.insertRow(r)
