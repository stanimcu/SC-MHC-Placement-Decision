# South Carolina Mobile Health Clinic Placement Decision Tool

A decision-support web tool for identifying high-impact deployment locations for Mobile Health Clinics (MHCs) in South Carolina. The tool uses geospatial demand data, candidate community sites, and travel-time thresholds to recommend MHC locations that maximize coverage of a selected target population.

This repository operationalizes the mobile health clinic placement framework described in:

> Tanim SH, White DL, Witrick B, Rennert L. **Optimizing mobile health clinic placement via geospatial modeling.** *Public Health in Practice.* 2026;11:100805. https://doi.org/10.1016/j.puhip.2026.100805

The published study demonstrated that optimized MHC placement can substantially increase the uninsured population reached within practical driving and walking thresholds. This web tool translates that framework into an interactive planning workflow for comparing deployment alternatives, reviewing feasibility, and exporting field-ready site lists.

## Using the hosted web tool

No installation is required to use the hosted version of the tool. Users can open the web app link, select the target variable, county or ZIP code, travel threshold, site types, number of MHCs, and then click **Calculate Optimal Sites**.

Web tool link: https://sc-mhc-placement-decision.streamlit.app/

## Table of contents

- [Using the hosted web tool](#using-the-hosted-web-tool)
- [Overview](#overview)
- [Key features](#key-features)
- [Optimization method](#optimization-method)
- [Travel-time modes](#travel-time-modes)
- [Data inputs](#data-inputs)
- [Target population variables](#target-population-variables)
- [Using the web tool](#using-the-web-tool)
- [Outputs and exports](#outputs-and-exports)
- [Methodological notes and limitations](#methodological-notes-and-limitations)
- [Citation](#citation)
- [Optional local setup for developers](#optional-local-setup-for-developers)
- [Acknowledgments](#acknowledgments)
- [License](#license)

## Overview

Mobile Health Clinics can reduce geographic and transportation barriers by bringing services directly to communities. However, choosing deployment sites based only on convenience can miss high-need populations or create redundant service areas. This tool supports systematic placement decisions by combining:

- candidate MHC deployment locations;
- census block-based demand points;
- a user-selected target population or need measure;
- drive or walk travel-time thresholds;
- a Maximum Coverage Location Problem (MCLP) optimization model;
- ranked and site-distinct alternative deployment plans;
- interactive maps and exportable site tables.

The app is currently configured for South Carolina ZIP-code-based planning, but the workflow can be adapted to other geographies by preparing an input JSON file that follows the same required data structure and field names.

## Key features

### 1. Target-guided county and ZIP planning

Users first choose the target category and target measure that define the population or need to optimize. They then select a South Carolina county and ZIP code, or search for any ZIP code directly. When a county is selected, ZIP options are filtered to that county and ranked by the selected target variable when demand data are available.

The selected ZIP code defines where candidate sites come from. Demand is not clipped to the ZIP boundary: every census block centroid in the loaded dataset that can reach an eligible site within the travel-time threshold is included, even when it lies in a neighboring ZIP code or county.

### 2. Flexible target population selection

The tool can optimize for multiple need measures, including uninsured population, age-specific uninsured groups, total population, age groups, non-white population, Hispanic population, zero-vehicle households, non-English-speaking households, veterans, workers, and other demographic indicators available in the input data. Although disease burden is currently included as a placeholder, the workflow can support disease-specific burden measures if the corresponding data field is added to the input JSON and mapped in the target-variable settings.

### 3. MCLP-based site selection

The core model is a Maximum Coverage Location Problem. It selects a fixed number of MHC deployment sites that maximize the selected target population covered within the chosen travel-time threshold. For example, if the user selects 3 MHCs and a 5-minute drive threshold, the tool recommends a three-site deployment plan that maximizes covered demand within 5 minutes.

### 4. Coverage and travel-time ranking

Deployment plans are ranked lexicographically among generated alternatives:

1. highest covered target population first;
2. lowest weighted average nearest travel time among plans with the same covered target population, when travel-time estimates are available.

This keeps population coverage as the primary objective and uses travel time only as a secondary tie-breaker when plans achieve the same covered target population.

### 5. Ranked and distinct alternative deployment plans

The tool separates **Number of MHCs to deploy** from **Alternative plans to show**. For one MHC, it ranks individual candidate sites. For two or more MHCs, it repeatedly solves the MCLP and applies no-good or diversity constraints to generate practical backup configurations.

By default, backup plans prefer more site-distinct alternatives so decision-makers do not receive several plans that repeat most of the same locations. Because this diversity rule intentionally creates more different backup options, lower-ranked plans may sometimes cover slightly fewer people than the top plan.

### 6. Previous-deployment extension planning

When the selected area already has one or more MHC deployment locations, users can enter those prior sites in **Advanced settings**. The tool can remove demand already covered by previous deployments and optimize the next plan for newly reachable demand. Previous deployment locations can be entered by selecting one or more candidate sites or by entering custom latitude and longitude coordinates.

### 7. Candidate exclusion and recommended-site review

The app supports both pre-run and post-run exclusion workflows:

- **Pre-run exclusion:** remove known infeasible or unavailable candidate sites before optimization.
- **Post-run review:** inspect recommended sites, mark infeasible or unavailable locations, and rerun the optimizer using the remaining candidates.

### 8. Operational feasibility fields

If the candidate site data include feasibility fields, the app displays and exports them for field verification. Supported optional fields include:

`feasibility_status` , `parking`, `restroom`, `wifi`, `ada`, `permission`

These fields are not required for optimization, but they help translate model-selected sites into real-world deployment decisions.

### 9. Fleet-size scenario analysis

The tool can run exact best-plan coverage analysis for multiple fleet sizes, such as 1 through 5 MHCs. This helps users compare marginal coverage gains and understand the potential benefit of adding another mobile clinic. It is off by default to keep runs fast; turn it on under **Advanced settings**.

### 10. Travel-time and distance modes

The app uses **road-network travel times precomputed with ArcGIS** by default, with OpenStreetMap routing and then a fast **Manhattan-style distance** approximation as fallbacks. Users can also turn routing off to use the approximation directly. Drive and walk scenarios are supported with 5, 10, 15, or 20-minute thresholds.

### 11. Interactive maps and plan comparison outputs

The app displays county overview maps, ZIP-level candidate sites, selected MHC locations, covered and uncovered demand points, muted non-selected candidates after optimization, and previous deployment locations when extension planning is active.

### 12. Exportable results

Users can export best-plan site lists, all-plan summary tables, field-verification files, and GeoJSON files for reporting, GIS use, or operational follow-up.

## Optimization method

The core optimization model is a Maximum Coverage Location Problem (MCLP).

Let:

- `i` index candidate MHC sites;
- `j` index demand points;
- `w_j` be the target population weight at demand point `j`;
- `p` be the number of MHCs to deploy;
- `a_ij = 1` if candidate site `i` covers demand point `j` within the selected travel-time threshold, and `0` otherwise;
- `x_i = 1` if candidate site `i` is selected;
- `y_j = 1` if demand point `j` is covered by at least one selected site.

The primary objective is:

```text
maximize sum_j w_j * y_j
```

Subject to:

```text
sum_i x_i = p

y_j <= sum_i a_ij * x_i    for every demand point j

x_i, y_j in {0, 1}
```

The app first maximizes covered demand. When a valid travel-time matrix is available and the tie-breaker problem is computationally feasible for an interactive run, a second-stage optimization keeps the same maximum coverage value and minimizes weighted travel time among tied solutions.

For multiple alternative plans, the app repeatedly solves the model while adding constraints that prevent duplicate or overly similar site sets.

### Study area and coverage percentage

Candidate sites `i` are limited to the selected ZIP code after site-type filters and exclusions. Demand points `j` include every census block centroid in the loaded dataset that at least one eligible candidate site can reach within the travel-time threshold, including blocks in neighboring ZIP codes and counties.

Coverage percentage is covered demand divided by the total target value of that reachable demand set. In previous-deployment mode, demand already covered by prior sites is removed from both the objective and the denominator. Because the denominator depends on the candidate set and travel time, coverage percentages are best compared within one scenario rather than across ZIP codes or thresholds.

## Travel-time modes

The app supports two accessibility modes.

### Road-network routing, default

Road-network routing is enabled by default. Travel times from every census block centroid to every candidate site in South Carolina were precomputed with ArcGIS Network Analyst (time-neutral Driving Time and Walking Time, up to 20 minutes) and are stored in `data/arcgis_travel_times/`, so results load without downloading roads. If the input data change, these travel times must be rebuilt; until then the app uses the OpenStreetMap routing described below.

### OpenStreetMap routing, fallback

The app uses OSMnx and NetworkX to build an OpenStreetMap drive or walk network around the selected ZIP's candidate sites, then computes free-flow travel time from each census block centroid to each candidate site.

- Driving speeds use OpenStreetMap speed limits where tagged. Missing speeds are filled by road class using South Carolina defaults, for example 105 km/h on motorways, 72 km/h on primary roads, and 40 km/h on residential streets.
- Walking uses 5 km/h.
- A 5-second penalty per road segment approximates intersection delay.
- Block centroids and sites are snapped to the nearest network node. Points more than 2 km from the network are treated as unreachable.
- The download area is sized from the travel-time threshold and widened automatically if the network contains faster roads than assumed, so coverage is not cut off at the edge of the area.

Road networks are downloaded from public Overpass API servers on the first run for an area and then cached on disk. Later runs for the same area, including shorter travel times, different site-type filters, and review-and-rerun exclusions, load the cached network without contacting a server. If no Overpass server is available, the app shows a warning and uses Manhattan-style distance for that run.

Important notes:

- Road-network travel time is not live traffic.
- Time-of-day congestion is not modeled.
- OpenStreetMap coverage and road attributes may vary by location.
- The published paper used ArcGIS Network Analyst and Esri StreetMap Premium. The web tool's default travel times also come from ArcGIS Network Analyst.

### Manhattan-style distance

This fast approximation computes projected rectilinear distance between each candidate site and demand point, multiplies by a 1.2 circuity factor, and converts the result to travel time at 25 mph for driving or 5 km/h for walking. It is used when road-network routing is turned off or cannot be loaded.

Use this mode for quick screening, demonstrations, or situations where road-network routing is not needed.

## Data inputs

The app expects a JSON file referenced by `JSON_PATH` in `config.py`.

### Required elements

The app requires:

- `counties`
- `zip_boundaries` or `zips`
- `candidate_facilities` or `facilities`
- `demand_points` or `demand`
- `latitude` and `longitude` for every candidate site
- `latitude` and `longitude` for every demand point
- at least one demand weight variable used by the target selector

### Coordinate format for boundaries

Boundary coordinates in the JSON are expected as:

```text
[latitude, longitude]
```

The app converts these to the longitude-latitude order used by Shapely.

### Candidate facility fields

Required:

| Field | Description |
|---|---|
| `latitude` | Site latitude |
| `longitude` | Site longitude |

Recommended:

| Field | Description |
|---|---|
| `facility_id` | Stable site identifier |
| `name` | Site name |
| `type` | Site category, such as clinic, school, faith-based organization, community center, grocery/food retail, or other community-accessible site |
| `address` | Site address |

Optional feasibility fields:

| Field | Description |
|---|---|
| `feasibility_status` | Feasible, infeasible, unavailable, unknown, etc. Sites marked infeasible, not feasible, unavailable, rejected, closed, or do not use can be excluded before a run. |
| `parking` | Parking availability |
| `restroom` | Restroom availability |
| `wifi` | WiFi availability |
| `ada` | ADA accessibility |
| `permission` | Permission or venue approval status |

### Demand point fields

Required:

| Field | Description |
|---|---|
| `latitude` | Demand point latitude |
| `longitude` | Demand point longitude |

At least one target variable column should be present, such as `uninsured_pop` or `tot_pop`.

## Target population variables

The tool displays only target variables that are present in the demand point data.

| UI label | JSON column |
|---|---|
| Uninsured Population | `uninsured_pop` |
| Total Population | `tot_pop` |
| Disease burden (placeholder) | `tot_hh` |
| Male Adult Population (20+) | `male_adult` |
| Female Adult Population (20+) | `female_adult` |
| Uninsured Under 19 | `uninsured_under19` |
| Uninsured 20-34 | `uninsured_20_34` |
| Uninsured 35-64 | `uninsured_35_64` |
| Uninsured 65+ | `uninsured_65plus` |
| Population 0-5 | `pop_0_5` |
| Population 0-19 | `pop_0_19` |
| Population 20-34 | `pop_20_34` |
| Population 35-64 | `pop_35_64` |
| Population 65+ | `pop_65plus` |
| Non-White Population | `nonwhite_pop` |
| Hispanic Population | `hispanic_pop` |
| Zero-Vehicle Households | `zero_vehicle_hh` |
| Enrolled in School | `enrolled_school` |
| Non-English at Home | `non_english_home` |
| Worker Population | `worker_pop` |
| Veteran Population | `veteran_pop` |

## Using the web tool

1. Choose a **target category** and target measure, such as uninsured population.
2. Select a **county** or search any South Carolina ZIP code. When a county is selected, ZIP choices are filtered and ranked using the selected target variable. The ZIP defines where candidate sites come from; covered demand can extend into neighboring ZIP codes and counties.
3. Choose candidate **site types** to include.
4. Open **Advanced settings** when needed to:
   - add previous deployment locations;
   - prefer or disable site-distinct backup plans;
   - exclude known infeasible sites;
   - enable fleet-size scenario analysis;
   - adjust map display settings.
5. Choose the **number of MHCs to deploy** (default 1).
6. Choose the number of **alternative plans to show** (default 3, up to 10).
7. Select travel mode: **drive** or **walk**.
8. Select the maximum travel-time threshold: 5, 10, 15, or 20 minutes.
9. Road-network routing is on by default. Turn it off for faster Manhattan-style estimates.
10. Click **Calculate Optimal Sites**.
11. Review the plan comparison table, plan maps, and site-level metrics.
12. Mark infeasible or unavailable recommended sites and rerun, if needed.
13. Export CSV or GeoJSON outputs for reporting or field verification.

## Outputs and exports

### On-screen outputs

The tool displays:

- summary statistics for the candidate ZIP code and the demand within reach;
- a plan set summary with best coverage, covered demand points, and the travel-time method used;
- ranked deployment plan comparison table;
- selected-site maps for each plan;
- detailed coverage map for each plan;
- site-level metrics;
- fleet-size scenario chart and table under **Optional analytics**, when enabled.

### Plan comparison table

The plan comparison table includes:

- plan rank;
- number of sites in the plan;
- covered target population;
- coverage percentage;
- coverage denominator, the total reachable target value used for the percentage;
- average nearest travel time;
- loss compared with best plan;
- selected site names;
- selected site types.

### Site-level metrics

For each selected site, the tool reports:

- **Gross covered demand**: demand the site could reach by itself.
- **Marginal contribution**: coverage lost if that site is removed from the selected plan.

The marginal contribution metric is especially useful for multi-site plans because it identifies which selected sites add unique non-overlapping coverage.

### Downloadable files

The app can export:

| Export | Format | Description |
|---|---|---|
| Best Plan Sites | CSV | Site list for the top-ranked plan |
| All Plan Summary | CSV | Summary table for all ranked plans |
| Field Verification | CSV | All plan sites with any feasibility fields from the data, plus blank `field_feasibility_status` and `field_notes` columns for site visits |
| Best Plan Sites | GeoJSON | Selected sites from the best plan for GIS use |

## Methodological notes and limitations

This tool is intended to support planning decisions, not replace local knowledge or community engagement.

Important limitations:

- The model optimizes spatial accessibility and selected demand weights; it does not guarantee site permission, staffing feasibility, community trust, or expected utilization.
- Candidate site quality depends on the completeness and accuracy of the input dataset.
- Demand estimates based on ACS or disaggregated census data can contain uncertainty.
- Coverage is evaluated at census block centroids, not block boundaries.
- Only census blocks in the loaded dataset are counted, so demand across state lines is excluded unless it is added to the input data.
- Coverage percentage uses the demand reachable from eligible sites, so it changes with site filters and travel time.
- Manhattan-style distance is a fast approximation, not a true road-network route.
- Road-network travel times are time-neutral and do not reflect live traffic.
- Live traffic, time-of-day effects, transit access, seasonal demand, and patient preferences are not currently modeled.
- The app evaluates driving and walking separately.

Recommended use:

- Use the tool to shortlist high-potential deployment sites.
- Review recommendations with local partners.
- Check operational feasibility in the field.
- Use exports for site verification, partner discussion, and deployment planning.
- Update candidate and demand data regularly.

## Citation

If you use this work, please cite both the published methodology paper and the web tool/source code.

Cite the paper for the methodological framework, including the location-allocation/MCLP approach, demand weighting, and travel-threshold-based coverage model.

Cite the web tool/source code for the software implementation, user interface, deployment-plan ranking, scenario analysis, review/rerun workflow, and export tools.

### Methodology paper

Tanim, S. H., White, D. L., Witrick, B., & Rennert, L. (2026). Optimizing mobile health clinic placement via geospatial modeling. *Public Health in Practice, 11*, 100805. https://doi.org/10.1016/j.puhip.2026.100805

### Web tool and source code

Tanim, S. H., Iuricich, F., & Rennert, L. (2026). *South Carolina Mobile Health Clinic Placement Decision Tool* (Version v0.5) [Web application and source code].

- Web tool: https://sc-mhc-placement-decision.streamlit.app/
- Source code: https://github.com/DMA-PRIME/SC-MHC-Placement-Decision
- Accessed: [date accessed]

### BibTeX

```bibtex
@article{tanim2026optimizing_mhc,
  title = {Optimizing mobile health clinic placement via geospatial modeling},
  author = {Tanim, Shakhawat H. and White, David L. and Witrick, Brian and Rennert, Lior},
  journal = {Public Health in Practice},
  volume = {11},
  pages = {100805},
  year = {2026},
  doi = {10.1016/j.puhip.2026.100805},
  url = {https://doi.org/10.1016/j.puhip.2026.100805}
}

@software{tanim2026south_carolina_mhc_tool,
  title = {South Carolina Mobile Health Clinic Placement Decision Tool},
  author = {Tanim, Shakhawat H. and Iuricich, Federico and Rennert, Lior},
  year = {2026},
  version = {v0.5},
  url = {https://github.com/DMA-PRIME/SC-MHC-Placement-Decision},
  note = {Web application: https://sc-mhc-placement-decision.streamlit.app/ accessed [date accessed]}
}
```

## Optional local setup for developers

Regular users do not need to install anything. The setup instructions below are only for developers or collaborators who want to run the Streamlit app locally, modify the source code, update the input dataset, or deploy another instance of the tool.

Python 3.11 or 3.12 is recommended. Current releases of OSMnx and GeoPandas require Python 3.11 or newer.

### Option 1: pip

```bash
git clone https://github.com/DMA-PRIME/SC-MHC-Placement-Decision.git
cd SC-MHC-Placement-Decision
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install streamlit pandas numpy geopandas shapely folium streamlit-folium osmnx networkx scikit-learn requests "pulp<4" highspy
```

### Option 2: conda, recommended on Windows for geospatial dependencies

```bash
git clone https://github.com/DMA-PRIME/SC-MHC-Placement-Decision.git
cd SC-MHC-Placement-Decision
conda create -n mhc-placement python=3.11 -y
conda activate mhc-placement
conda install -c conda-forge streamlit pandas numpy geopandas shapely folium osmnx networkx scikit-learn requests "pulp<4" -y
python -m pip install streamlit-folium highspy
```

If the CBC solver is not available through PuLP in your environment, install CBC through conda-forge:

```bash
conda install -c conda-forge coincbc -y
```

Dependency notes:

- **PuLP must stay below version 4.** PuLP 4.0 removed `LpStatus` and the bundled CBC solver, so the app fails at startup with that version.
- **scikit-learn is required for road-network routing.** OSMnx uses it to snap points to the road network. Without it, routing reports no reachable pairs.
- **highspy is required on Apple Silicon Macs.** PuLP's bundled CBC solver does not run there, so the app uses the HiGHS solver instead.

### Requirements file for deployment

Streamlit Community Cloud installs packages from `requirements.txt`. Keep the PuLP pin and `highspy` there as well:

```text
streamlit
pandas
numpy
geopandas
shapely
folium
streamlit-folium
osmnx
networkx
scikit-learn
requests
pulp<4
highspy
```

### Configuration

Create a file named `config.py` in the repository root:

```python
from pathlib import Path

JSON_PATH = Path("data/sc_mhc_input.json")
```

Place the input JSON file at that path, or update `JSON_PATH` to point to the correct file location.

### Road-network settings (optional)

These environment variables change how the app downloads and caches road networks. On Streamlit Community Cloud, or in a local `.streamlit/secrets.toml`, they can be set as root-level secrets, which Streamlit exposes as environment variables.

| Variable | Purpose |
|---|---|
| `MHC_OVERPASS_CONTACT` | Email or project URL added to the app's user agent so Overpass operators can contact you instead of blocking you. Recommended. |
| `MHC_OVERPASS_URLS` | Comma-separated Overpass API base URLs to try first, such as an institutional or self-hosted server. |
| `MHC_GRAPH_CACHE_DIR` | Folder for cached road networks. |
| `MHC_OSMNX_CACHE_DIR` | Folder for the OSMnx download cache. |

Example `.streamlit/secrets.toml`:

```toml
MHC_OVERPASS_CONTACT = "your.name@university.edu"
```

Without overrides, the app tries these Overpass servers in order and remembers the last one that worked:

1. `https://overpass-api.de/api`
2. `https://maps.mail.ru/osm/tools/overpass/api`
3. `https://overpass.private.coffee/api`
4. `https://overpass.kumi.systems/api`

Default road-network cache locations:

- Windows: `%LOCALAPPDATA%\MHC_Placement_Tool\road_networks`
- macOS and Linux: `~/.cache/mhc_placement_tool/road_networks`, or under `$XDG_CACHE_HOME` when set

Delete the `road_networks` folder to force fresh downloads. Editing the road speed table in `app.py` invalidates cached networks automatically. On Streamlit Community Cloud, the cache lasts until the app restarts.

### Troubleshooting road-network routing

**"Road network failed, so the app is using Manhattan-style distance instead"** means no Overpass server could provide the network. The message lists what each server returned.

- `connection refused`, `HTTP 406`, or `HTTP 429` usually means the server is refusing the network's IP address, which is more common on shared campus and cloud networks. Check whether https://overpass-api.de/api/status opens in a browser on the same network, set `MHC_OVERPASS_CONTACT`, or try another network.
- `blocked by network proxy` means a firewall or proxy is stopping the request before it reaches the server.
- Areas already in the cache keep working without any server.

**"Road-network routing produced no routable candidate-to-demand pairs"** usually means `scikit-learn` is missing, or site and block coordinates are more than 2 km from a mapped road.

### Running the app

```bash
streamlit run app.py
```

The app will open in a browser window. If it does not open automatically, Streamlit will print a local URL in the terminal.

### Suggested repository structure

```text
.
|-- app.py
|-- arcgis_travel_times.py  # reads the precomputed travel times
|-- config.py              # local configuration; do not commit sensitive paths
|-- requirements.txt       # dependency list used by Streamlit Community Cloud
|-- README.md
|-- LICENSE
|-- .streamlit/
|   `-- secrets.toml       # optional road-network settings; do not commit
|-- data/
|   |-- arcgis_travel_times/  # precomputed ArcGIS travel times (drive, walk)
|   `-- sc_mhc_input.json  # local data; avoid committing confidential data
|-- docs/
|   `-- screenshot.png     # optional screenshot for GitHub README
`-- outputs/               # optional exported results
```

## Acknowledgments

This project builds on collaborative mobile health clinic deployment research with Clemson Rural Health and Prisma Health. The underlying research was supported by the National Library of Medicine, the National Institute on Drug Abuse, the CDC Center for Forecasting and Outbreak Analytics, Gilead Sciences, and the South Carolina Center for Rural and Primary Healthcare, as described in the associated publication.

## License

This repository is licensed under the MIT License. See the `LICENSE` file for details.

The associated methodology paper is published separately and should be cited as described above.
