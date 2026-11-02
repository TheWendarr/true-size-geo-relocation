# True Size Relocation toolbox (ArcGIS Pro)

Moves every feature class, raster and table in a file geodatabase to another place on Earth at true ground size, then writes the result to a new file geodatabase.

## Install
1. Unzip and keep these three files in the same folder:
   - `TrueSizeRelocation.pyt`
   - `TrueSizeRelocation.pyt.xml` (toolbox description)
   - `TrueSizeRelocation.RelocateGeodatabase.pyt.xml` (tool help and parameter hints)
2. In ArcGIS Pro, open the Catalog pane, right-click **Toolboxes**, choose **Add Toolbox**, and pick `TrueSizeRelocation.pyt`.
3. Open **Relocation > Relocate Geodatabase (True Size)**.

## Inputs
- **Input File Geodatabase**: the source data (for example, Thailand).
- **Samples Feature Class**: a point feature class, `samples` by default, holding two points: the source (origin) and the destination.
- **Point Identifier Field**: chosen automatically (Role, Type, Name, Label, Id, or ObjectID).
- **Source / Destination Point**: dropdowns filled from that field's values.
- **Output Coordinate System**: defaults to the WGS 84 UTM zone of the destination point.
- **Datasets to Relocate**: a checklist of everything in the geodatabase; all are checked except `samples`.
- **Raster Options**: resampling (AUTO uses NEAREST for integer rasters and BILINEAR for floating-point) and cell size.
- **Vector Options**: maximum shape deviation for densifying edges.

## Method
For each dataset the tool:
1. Runs **Project** or **Project Raster** into Azimuthal Equidistant centered on the source point.
2. Runs **Define Projection** to relabel the result as Azimuthal Equidistant centered on the destination point. Only metadata changes in this step.
3. Runs **Project** or **Project Raster** into the output coordinate system.

Distance and bearing from the source point are exact, and distortion grows with distance from it. A warning is shown for data reaching more than 2,500 km from the source.

The output geodatabase includes a `RELOCATION_INFO` table recording the points, coordinate systems and settings used.

## Limits
- Supported: simple feature classes (including those inside feature datasets), raster datasets and tables.
- Skipped with a warning: annotation, dimension, mosaic datasets, topologies, networks and relationship classes.
- Rasters are resampled twice. Use NEAREST or MAJORITY for categorical rasters.
- Attribute and elevation values are not changed.

## Test data
`test_data/thailand_test.gdb` holds the following, all at their real locations:
- `samples`: Bangkok and Pikes Peak, with Name and Role fields.
- `thailand_boundary`: the Natural Earth 1:10m boundary.
- `thai_cities`: 1,072 GeoNames places.
- `admin/provinces`: Thai provinces inside a feature dataset.

It has no raster; add one, such as a DEM clip, to test raster handling.
