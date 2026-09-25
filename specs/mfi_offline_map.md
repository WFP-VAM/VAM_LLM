# Offline MFI cartography and numbered market labels

The lightweight MFI workflow now draws a country basemap behind the stored market
scores and uses numbered callouts with a side legend. There are no additional
model calls, runtime downloads, cloud services, environment settings or GIS
runtime dependencies. `geographic_map` remains the same PNG artifact.

## Data and rendering

Natural Earth Admin 0 Countries 1:10m v5.1.1 is shipped as compressed GeoJSON.
Its source archive and derived asset checksums, license URL and conversion
procedure are recorded in `app/services/mfi_drafter/assets/basemap/`.
The conversion preserves polygon parts and holes without simplification.
The source uses Natural Earth's default de facto boundaries; the polygons never
determine which assessment records are included or excluded.

The shared country resolver identifies the highlighted country. Exact Natural
Earth country names cover countries outside the resolver's operational list.
Unknown/synthetic names keep the coordinate-based background with a recorded
limitation; no country is guessed. The viewport follows valid market coordinates
with an 8% margin (at least 0.1 degrees per axis), including across the
antimeridian. Source coordinates remain unchanged. Wrapped display longitudes
and unwrapped polygon rings are used only to draw the shortest geographic span.
The view uses equirectangular proportions at its central latitude.

The worker retains explicit Figure/Axes ownership, Agg, existing per-figure
timeouts/retries and response identity checks. Map-specific layout is fixed
before label placement; it does not run `tight_layout` afterwards. Country
geometries are clipped to the fixed map axes. Leaders are drawn below the
white numbered circles, including for coincident markets.

## Identity and presentation

Map records join the analytical profile to source coordinates by `market_key`.
A unique display-name adapter remains for records without stable IDs; ambiguous
or conflicting identities are rejected. Only valid locations are plotted.
All selected markets remain in the legend, including an explicit unavailable
coordinate marker where needed. Callout numbers are the existing selection
order, so missing coordinates never renumber later markets. The cap remains 15.

Colours retain the fixed 0–10 Blues scale. Market names live in the wrapping
side legend. The new map is exported at 6.5 inches with a 9-point legend. The
caption explains the scale, selected-market numbers and coordinate coverage.
There are no inset maps, automatic cluster zooms or new administrative layers.

## Preflight and compatibility

Before scheduling CSV generation, `light_service.validate_submission` checks that
the required local asset is readable, has the expected checksum and contains
valid geometry. Both API and Streamlit return an explicit 503 cartography error
on failure. Direct execution also checks before the graph or any model call.
Runs without any valid coordinate do not require a map.

`mfi-map-v2`, the Natural Earth version and asset checksum form the map's
rendering contract. The contract travels in the map job's data, so it is part of
the job's fingerprint, and rendering refuses a job whose contract does not match
the installed asset. Workflow/model contracts remain unchanged. Completed
reports keep their saved figures and export behavior.

Figure metadata records rendering/asset versions, highlighted and intersecting
countries, original coordinates, coverage, identity-to-number mappings,
placement boxes and limitations. No new public endpoint is required.

## Verification

Tests cover offline drawing; source integrity and polygon holes; Benin, Haiti,
Madagascar and Fiji; unknown countries; Unicode/long names; coincident markets;
missing/nonfinite/out-of-range coordinates; duplicate identities; numbering
gaps; final label-box geometry; concurrent spawned workers; and a changed map
revision after an injected assembly failure with zero repeated model calls.

The lightweight full-country tests retain Benin's 53 markets and Haiti's 68
Full-MFI markets plus six exclusions, stored scores, denominators and selection
ordering. API/Streamlit configuration-error parity and historical report/export
tests cover the shared delivery boundary.

Local QA images compare the previous and revised maps. The Benin, Haiti and
15-coincident-market preview DOCX files were exported through the application's
document renderer and visually checked after Word PDF conversion on Windows.
The packaged DOCX rendering helper could not run because this Windows runtime
does not include LibreOffice. Native Word ran in a separate hidden instance for
the local QA documents; production dependencies and export behavior were not
changed. Assessment-derived preview files remain local under `.tmp/mfi-map/`.

Verification on 2026-09-09 passed on Windows (Python 3.12) and Ubuntu WSL
(Python 3.11, matching the container's Python series): 49 map/visualization/
workflow tests, 56 delivery/compatibility tests (two already-tested country
benchmarks deselected), and 17 final map/preflight tests after the geometry-cache
separation. These groups overlap and are not an aggregate test count. Live model
generation was not needed for this rendering change; the country workflows use
the real analytical engine with model response stubs.
