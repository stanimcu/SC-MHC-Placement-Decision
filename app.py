"""
South Carolina MHC Placement Decision Tool (v9.4 advanced settings + multi previous deployment)
===============================================================

Best combined version composed from the two v9 drafts.

Core changes retained and strengthened
--------------------------------------
- Separates "Number of MHCs to deploy" from "Alternative deployment plans to show".
- Generates ranked deployment alternatives:
  - 1 MHC: ranks individual candidate sites by coverage.
  - 2+ MHCs: repeatedly solves MCLP with no-good/diversity constraints.
- Adds stacked all-plans-visible comparison cards with larger, interactive, site-focused maps and plan/site-level metrics.
- Adds a simplified exact-only fleet size scenario analysis for 1..configured maximum MHCs with coverage and marginal gains.
- Mutes non-selected candidate sites after analysis so selected sites and covered/uncovered
  census blocks remain prominent.
- Adds advanced extension-planning mode so prior deployment coverage can be excluded
  and the model can focus on newly reachable demand.
- Adds a post-run recommended-site review workflow so users can mark suggested
  sites infeasible/unavailable, exclude them, and rerun the optimizer.
- Keeps advanced pre-run candidate exclusion and optional feasibility-column
  display/export for parking, restroom, WiFi, ADA, permission, and feasibility
  status fields when present.
- Adds exports for the best plan, all plans, GeoJSON, and field-verification workflow.
- Resets stale analysis when model-defining controls change.

Author: Tanim
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
import warnings
from pathlib import Path

import folium
from folium.plugins import Fullscreen
import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from pulp import LpMaximize, LpMinimize, LpProblem, LpStatus, LpVariable, lpSum, value
try:
    from pulp import COIN_CMD
except Exception:  # PuLP versions before/after COIN_CMD availability
    COIN_CMD = None
try:
    from pulp import PULP_CBC_CMD
except Exception:  # PuLP 4 removes the legacy bundled CBC solver name
    PULP_CBC_CMD = None
from shapely import wkt
from shapely.geometry import Polygon
from streamlit_folium import st_folium

from config import JSON_PATH

warnings.filterwarnings("ignore")

APP_VERSION = "v9.6 coverage + travel-time ranking + distinct backups"


# ===========================
# OSMNX CACHE CONFIGURATION
# ===========================
def configure_osmnx_cache():
    """
    Configure OSMnx to use a writable absolute cache directory.

    OSMnx defaults to a relative ./cache folder. On some Windows installs,
    Streamlit sessions, OneDrive/network folders, or locked project folders can
    make that relative cache path fail with errors such as WinError 433. This
    function moves the cache to a user/temp-local folder and falls back to no
    disk cache if no writable folder is available.
    """
    candidate_dirs = []

    custom_dir = os.environ.get("MHC_OSMNX_CACHE_DIR")
    if custom_dir:
        candidate_dirs.append(Path(custom_dir))

    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidate_dirs.append(Path(local_appdata) / "MHC_Placement_Tool" / "osmnx_cache")
    else:
        xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
        if xdg_cache_home:
            candidate_dirs.append(Path(xdg_cache_home) / "mhc_placement_tool" / "osmnx")
        else:
            candidate_dirs.append(Path.home() / ".cache" / "mhc_placement_tool" / "osmnx")

    candidate_dirs.append(Path(tempfile.gettempdir()) / "mhc_placement_tool" / "osmnx_cache")

    for cache_dir in candidate_dirs:
        try:
            cache_dir = cache_dir.expanduser().resolve()
            cache_dir.mkdir(parents=True, exist_ok=True)
            test_file = cache_dir / ".write_test"
            test_file.write_text("ok", encoding="utf-8")
            try:
                test_file.unlink()
            except FileNotFoundError:
                pass

            ox.settings.use_cache = True
            # Forward slashes are accepted by Windows and avoid fragile escaped
            # backslash display such as cache\b....json in error messages.
            ox.settings.cache_folder = cache_dir.as_posix()
            ox.settings.requests_timeout = 180
            return cache_dir
        except Exception:
            continue

    ox.settings.use_cache = False
    ox.settings.requests_timeout = 180
    return None


OSMNX_CACHE_DIR = configure_osmnx_cache()

# ===========================
# PAGE CONFIG
# ===========================
st.set_page_config(
    page_title="SC MHC Placement Decision Tool",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ===========================
# TARGET VARIABLE OPTIONS
# ===========================
TARGET_VARIABLE_OPTIONS = {
    "Uninsured Population": "uninsured_pop",
    "Total Population": "tot_pop",
    "Disease burden (placeholder)": "tot_hh",
    "Male Adult Population (20+)": "male_adult",
    "Female Adult Population (20+)": "female_adult",
    "Uninsured Under 19": "uninsured_under19",
    "Uninsured 20-34": "uninsured_20_34",
    "Uninsured 35-64": "uninsured_35_64",
    "Uninsured 65+": "uninsured_65plus",
    "Population 0-5": "pop_0_5",
    "Population 0-19": "pop_0_19",
    "Population 20-34": "pop_20_34",
    "Population 35-64": "pop_35_64",
    "Population 65+": "pop_65plus",
    "Non-White Population": "nonwhite_pop",
    "Hispanic Population": "hispanic_pop",
    "Zero-Vehicle Households": "zero_vehicle_hh",
    "Enrolled in School": "enrolled_school",
    "Non-English at Home": "non_english_home",
    "Worker Population": "worker_pop",
    "Veteran Population": "veteran_pop",
}

TARGET_CATEGORIES = {
    "Health insurance": [
        "Uninsured Population",
        "Uninsured Under 19",
        "Uninsured 20-34",
        "Uninsured 35-64",
        "Uninsured 65+",
    ],
    "General population": [
        "Total Population",
        "Population 0-5",
        "Population 0-19",
        "Population 20-34",
        "Population 35-64",
        "Population 65+",
    ],
    "Demographics": [
        "Male Adult Population (20+)",
        "Female Adult Population (20+)",
        "Non-White Population",
        "Hispanic Population",
        "Non-English at Home",
    ],
    "Socio economic": [
        "Zero-Vehicle Households",
        "Enrolled in School",
        "Worker Population",
        "Veteran Population",
    ],
    "Disease burden": [
        "Disease burden (placeholder)",
    ],
}

# ===========================
# OPTIONAL OPERATIONAL FEASIBILITY FIELDS
# ===========================
# These fields are not required in the JSON. If present in candidate facilities, they
# are surfaced in tables and exports so decision makers can screen backup sites for
# real-world deployment constraints.
FEASIBILITY_COLUMNS = {
    "feasibility_status": "Feasibility status",
    "parking": "Parking",
    "restroom": "Restroom",
    "wifi": "WiFi",
    "ada": "ADA",
    "permission": "Venue permission",
}

# ===========================
# FACILITY TYPE COLORS
# ===========================
FACILITY_COLOR_PALETTE = [
    "#800080", "#DC143C", "#228B22", "#4169E1", "#8B0000", "#FF69B4",
    "#FF8C00", "#008080", "#6A5ACD", "#2E8B57", "#DAA520", "#708090",
    "#CD853F", "#4682B4", "#D2691E", "#9370DB", "#3CB371", "#BC8F8F",
    "#5F9EA0", "#E9967A", "#8FBC8F", "#B8860B", "#483D8B", "#2F4F4F",
    "#C71585", "#006400", "#191970", "#8B4513", "#556B2F", "#A0522D",
]

# Demand indicator colors — chosen to not overlap with site type palette
COVERED_COLOR = "#2ECC71"      # green
UNCOVERED_COLOR = "#E74C3C"    # red
MUTED_CANDIDATE_COLOR = "#7A8793"
PREVIOUS_COVERED_COLOR = "#95A5A6"
PREVIOUS_SITE_COLOR = "#34495E"


def get_type_color_map(facility_types):
    """Build a color map for facility types from the palette."""
    sorted_types = sorted(set(facility_types))
    return {
        t: FACILITY_COLOR_PALETTE[i % len(FACILITY_COLOR_PALETTE)]
        for i, t in enumerate(sorted_types)
    }


def build_target_selector(available_targets):
    """Two-step selector: category first, then target within that category."""
    available_labels = set(available_targets.keys())

    category_map = {
        category: [label for label in labels if label in available_labels]
        for category, labels in TARGET_CATEGORIES.items()
    }
    category_map = {
        category: labels
        for category, labels in category_map.items()
        if labels
    }

    if not category_map:
        st.error("No target variables are available in the loaded dataset.")
        st.stop()

    target_category = st.selectbox(
        "Target category",
        options=list(category_map.keys()),
    )

    target_options = category_map[target_category]

    default_target = (
        "Uninsured Population"
        if "Uninsured Population" in target_options
        else target_options[0]
    )

    target_label = st.selectbox(
        f"{target_category} measure",
        options=target_options,
        index=target_options.index(default_target),
    )

    return target_label, available_targets[target_label], target_category


def build_result_title(
    selected_zip_display,
    target_label,
    time_threshold,
    travel_mode,
    num_mhcs,
    num_alternative_plans,
):
    mode_label = "Drive" if travel_mode == "drive" else "Walk"
    mhc_label = "MHC" if int(num_mhcs) == 1 else "MHCs"
    plan_label = "plan" if int(num_alternative_plans) == 1 else "plans"
    return (
        f"Ranked Deployment Plans based on {target_label} "
        f"in {int(time_threshold)}-Min {mode_label}, "
        f"for {selected_zip_display} "
        f"({int(num_mhcs)} {mhc_label}, {int(num_alternative_plans)} {plan_label})"
    )


# ===========================
# CSS
# ===========================
def local_css():
    st.markdown(
        """
        <style>
            #MainMenu {visibility: hidden;}
            footer {visibility: hidden;}
            header {visibility: hidden;}
            [data-testid="stToolbar"] {visibility: hidden;}

            .main {
                background-color: #f8f9fb;
            }

            .stButton>button {
                width: 100%;
                border-radius: 10px;
                height: 3em;
                background-color: #004b98;
                color: white;
                font-weight: 700;
                border: 0;
                box-shadow: 0 2px 8px rgba(0, 75, 152, 0.18);
            }

            .stMultiSelect [data-baseweb="tag"] {
                background-color: #004b98 !important;
                border-radius: 20px !important;
                padding: 5px 10px !important;
                margin: 2px !important;
                color: white !important;
            }

            .instruction-box {
                background-color: #eaf4ff;
                padding: 14px 16px;
                border-radius: 12px;
                border: 1px solid #cfe4ff;
                margin-bottom: 14px;
            }

            .small-note {
                font-size: 0.88rem;
                color: #4d5b6a;
            }

            [data-testid="stMetricValue"] {
                font-size: 22px !important;
            }

            [data-testid="stSidebar"] {
                padding-top: 0.25rem;
            }

            [data-testid="stSidebar"] .stButton > button {
                height: 2.7rem;
                margin-top: 0.15rem;
                margin-bottom: 0.35rem;
            }

            [data-testid="stSidebar"] [data-testid="stExpander"] {
                margin-bottom: 0.35rem;
            }

            [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
                gap: 0.35rem;
            }

            [data-testid="stSidebar"] label {
                margin-bottom: 0.1rem;
            }

            .plan-pill {
                display: inline-block;
                padding: 3px 8px;
                margin: 2px 4px 2px 0;
                border-radius: 999px;
                background: #eef4fb;
                border: 1px solid #d7e3f1;
                font-size: 0.85rem;
            }

            .plan-compare-header {
                display: flex;
                justify-content: space-between;
                gap: 10px;
                align-items: flex-start;
                flex-wrap: wrap;
                margin-bottom: 6px;
            }

            .plan-rank-title {
                font-size: 1.08rem;
                font-weight: 800;
                color: #1f2d3d;
                margin: 0;
            }

            .plan-rank-badge {
                display: inline-block;
                padding: 4px 9px;
                border-radius: 999px;
                background: #004b98;
                color: white;
                font-size: 0.8rem;
                font-weight: 700;
                margin-right: 6px;
            }

            .plan-alt-badge {
                display: inline-block;
                padding: 4px 9px;
                border-radius: 999px;
                background: #eef4fb;
                color: #334e68;
                border: 1px solid #d7e3f1;
                font-size: 0.8rem;
                font-weight: 700;
            }

            .plan-best-badge {
                background: #e8f7ed;
                color: #1f6e43;
                border-color: #c7ebd3;
            }

            .plan-site-list {
                margin-top: 8px;
                color: #263341;
                font-size: 0.92rem;
            }

            .plan-site-list ol {
                margin: 0.25rem 0 0.25rem 1.25rem;
                padding-left: 0;
            }

            .plan-site-list li {
                margin-bottom: 0.35rem;
            }

            .plan-muted-text {
                color: #617184;
                font-size: 0.88rem;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


local_css()

# ===========================
# SAFE MAP RENDERING
# ===========================
def render_folium_map(m: folium.Map, key: str = "map", height: int = 640):
    try:
        st_folium(
            m,
            key=key,
            height=height,
            use_container_width=True,
            returned_objects=[],
        )
    except Exception:
        components.html(
            m.get_root().render(),
            height=height,
            scrolling=True,
        )


# ===========================
# CONSTANTS
# ===========================
DEFAULT_USE_NETWORK = False
DEFAULT_NUM_MHCS = 3
DEFAULT_NUM_ALTERNATIVE_PLANS = 3
MAX_ALTERNATIVE_PLANS = 10
DEFAULT_SWEEP_MAX_MHCS = 5
DEFAULT_SHOW_DEMAND_PREVIEW = False
DEFAULT_SHOW_MUTED_CANDIDATES_AFTER_ANALYSIS = True
COMPACT_PLAN_MAP_HEIGHT = 430
COMPACT_PLAN_SINGLE_SITE_ZOOM = 14
COMPACT_PLAN_MIN_FOCUS_SPAN_DEGREES = 0.028
COMPACT_PLAN_MAX_MIN_FOCUS_SPAN_DEGREES = 0.10
COMPACT_PLAN_FOCUS_PADDING_RATIO = 0.40
COVERAGE_DISTINCTNESS_THRESHOLD = 0.15
WALKING_SPEED_KMH = 5.0
ZIP_COUNTY_OVERLAP_THRESHOLD = 0.10

SC_HIGHWAY_SPEEDS_KMH = {
    "motorway": 105,
    "motorway_link": 72,
    "trunk": 89,
    "trunk_link": 64,
    "primary": 72,
    "primary_link": 56,
    "secondary": 56,
    "secondary_link": 48,
    "tertiary": 48,
    "tertiary_link": 40,
    "residential": 40,
    "living_street": 24,
    "service": 24,
    "unclassified": 40,
    "road": 40,
}
SC_FALLBACK_SPEED_KMH = 40
TURN_PENALTY_SECONDS = 5
CIRCUITY_FACTOR = 1.20
DEFAULT_DRIVING_SPEED = 25
MAX_SNAP_DIST_M = 2000
NETWORK_TRAVEL_TIME_WEIGHT = "travel_time_min"
NETWORK_ACCESS_DIRECTION = "demand_to_site"  # people traveling from census block centroids to the MHC site
NETWORK_QUERY_BUFFER_M_DRIVE = 5000
NETWORK_QUERY_BUFFER_M_WALK = 2000
MAX_TIEBREAKER_ASSIGNMENT_PAIRS = 60000

COVERAGE_ONLY_DIVERSITY_MODE = "Rank by coverage only"
SITE_DISTINCT_DIVERSITY_MODE = "Prefer distinct alternatives"
DEFAULT_DIVERSITY_MODE = SITE_DISTINCT_DIVERSITY_MODE

INFEASIBLE_STATUS_VALUES = {
    "infeasible",
    "not feasible",
    "unavailable",
    "rejected",
    "closed",
    "do not use",
    "do_not_use",
}


# ===========================
# SESSION STATE
# ===========================
for skey, default in [
    ("analysis_complete", False),
    ("alternative_plans", []),
    ("selected_facilities", None),
    ("coverage_matrix", None),
    ("travel_time_matrix", None),
    ("demand_reset", None),
    ("candidates_reset", None),
    ("covered_pop", 0.0),
    ("covered_mask", None),
    ("method_used", "Manhattan-style Distance"),
    ("selected_cand_ids", None),
    ("covered_dem_ids", None),
    ("target_variable", "uninsured_pop"),
    ("target_label", "Uninsured Population"),
    ("analysis_title", None),
    ("analysis_params", None),
    ("scenario_sweep_df", None),
    ("prior_deployment_active", False),
    ("previous_deployment_locations", []),
    ("previous_covered_dem_ids", set()),
    ("previous_covered_value", 0.0),
    ("remaining_target", None),
    ("review_excluded_cand_ids", set()),
    ("review_exclusion_zip", None),
    ("review_edit_version", 0),
    ("force_run_analysis", False),
    ("prev_county", None),
    ("prev_zip", None),
    ("view_mode", "county"),
    ("site_metrics_lookup", {}),
]:
    if skey not in st.session_state:
        st.session_state[skey] = default


# ===========================
# HELPERS
# ===========================
def reset_analysis_state():
    for key, value in {
        "analysis_complete": False,
        "alternative_plans": [],
        "selected_facilities": None,
        "coverage_matrix": None,
        "travel_time_matrix": None,
        "demand_reset": None,
        "candidates_reset": None,
        "covered_pop": 0.0,
        "covered_mask": None,
        "method_used": "Manhattan-style Distance",
        "selected_cand_ids": None,
        "covered_dem_ids": None,
        "site_metrics_lookup": {},
        "analysis_title": None,
        "analysis_params": None,
        "scenario_sweep_df": None,
        "prior_deployment_active": False,
        "previous_deployment_locations": [],
        "previous_covered_dem_ids": set(),
        "previous_covered_value": 0.0,
        "remaining_target": None,
    }.items():
        st.session_state[key] = value


def get_county_union_geometry(county_gdf, county_fips):
    """Return one combined geometry for the selected county."""
    if county_gdf is None or county_fips is None:
        return None

    county_rows = county_gdf[
        county_gdf["COUNTY_FIPS"].astype(str) == str(county_fips)
    ]

    if len(county_rows) == 0:
        return None

    try:
        return county_rows.geometry.union_all()
    except Exception:
        try:
            return county_rows.geometry.unary_union
        except Exception:
            return None


def calculate_zip_overlap_pcts(zip_geometries, county_geom):
    """Calculate ZIP-county overlap percentage (intersection area / ZIP area)."""
    pcts = []

    for geom in zip_geometries:
        try:
            if geom is None or geom.is_empty:
                pcts.append(0.0)
            elif county_geom is None or county_geom.is_empty:
                pcts.append(0.0)
            elif geom.area <= 0:
                pcts.append(0.0)
            else:
                pct = geom.intersection(county_geom).area / geom.area
                pcts.append(float(pct) if np.isfinite(pct) else 0.0)
        except Exception:
            pcts.append(0.0)

    return np.asarray(pcts, dtype=float)


def get_zips_in_county(
    zip_gdf,
    zip_county_map,
    county_fips,
    county_gdf=None,
    min_overlap_pct=ZIP_COUNTY_OVERLAP_THRESHOLD,
):
    if county_gdf is not None:
        county_geom = get_county_union_geometry(county_gdf, county_fips)
        if county_geom is not None:
            pcts = calculate_zip_overlap_pcts(zip_gdf["geometry"], county_geom)
            return zip_gdf[pcts >= min_overlap_pct].copy()

    if zip_county_map is not None and len(zip_county_map) > 0:
        zip_codes = zip_county_map[
            zip_county_map["COUNTY_FIPS"].astype(str) == str(county_fips)
        ]["ZIP_CODE"].astype(str).str.zfill(5).unique()
        return zip_gdf[
            zip_gdf["ZIP_CODE"].astype(str).str.zfill(5).isin(zip_codes)
        ].copy()

    return zip_gdf[zip_gdf["COUNTY_FIPS"].astype(str) == str(county_fips)].copy()


def build_zip_display(zip_row):
    po_name = str(zip_row.get("po_name", "")).strip()
    zip_code = str(zip_row.get("ZIP_CODE", "")).zfill(5)
    return f"{zip_code} ({po_name})" if po_name else zip_code


def get_ordered_zip_choices(
    zip_gdf,
    selected_county_fips=None,
    county_gdf=None,
    demand_df=None,
    target_var=None,
):
    """
    Build ZIP dropdown choices.

    When a county is selected, show only ZIPs with 10%+ area overlap and rank
    them by the selected target variable. When no county is selected, show all
    South Carolina ZIPs.
    """
    zip_choices = (
        zip_gdf[["ZIP_CODE", "po_name", "geometry"]]
        .drop_duplicates(subset=["ZIP_CODE"])
        .copy()
    )
    zip_choices["ZIP_CODE"] = zip_choices["ZIP_CODE"].astype(str).str.zfill(5)

    if selected_county_fips is not None and county_gdf is not None:
        county_geom = get_county_union_geometry(county_gdf, selected_county_fips)

        if county_geom is None:
            zip_choices = zip_choices.sort_values("ZIP_CODE").reset_index(drop=True)
            zip_choices["zip_label"] = zip_choices.apply(build_zip_display, axis=1)
            return zip_choices.drop(columns=["geometry"], errors="ignore")

        zip_choices["_pct"] = calculate_zip_overlap_pcts(
            zip_choices["geometry"], county_geom
        )

        county_zips = zip_choices[
            zip_choices["_pct"] >= ZIP_COUNTY_OVERLAP_THRESHOLD
        ].copy()

        if county_zips.empty:
            county_zips = zip_choices.sort_values("ZIP_CODE").reset_index(drop=True)
            county_zips["zip_label"] = county_zips.apply(build_zip_display, axis=1)
            return county_zips.drop(columns=["_pct", "geometry"], errors="ignore")

        if (
            demand_df is not None
            and target_var is not None
            and target_var in demand_df.columns
            and "zip_join" in demand_df.columns
            and demand_df["zip_join"].notna().any()
        ):
            demand_tmp = (
                demand_df[["zip_join", target_var]]
                .dropna(subset=["zip_join"])
                .copy()
            )
            demand_tmp["zip_join"] = demand_tmp["zip_join"].astype(str).str.zfill(5)
            demand_tmp[target_var] = pd.to_numeric(
                demand_tmp[target_var], errors="coerce"
            ).fillna(0)

            zip_target_sum = (
                demand_tmp.groupby("zip_join", as_index=False)[target_var]
                .sum()
                .rename(columns={"zip_join": "ZIP_CODE", target_var: "_target_sum"})
            )

            county_zips = county_zips.merge(zip_target_sum, on="ZIP_CODE", how="left")
            county_zips["_target_sum"] = county_zips["_target_sum"].fillna(0.0)
            county_zips = (
                county_zips
                .sort_values(["_target_sum", "ZIP_CODE"], ascending=[False, True])
                .reset_index(drop=True)
            )
            county_zips["_rank"] = range(1, len(county_zips) + 1)

            def build_zip_display_ranked(row):
                po_name = str(row.get("po_name", "")).strip()
                zip_code = str(row.get("ZIP_CODE", "")).zfill(5)
                rank = int(row["_rank"])
                name_part = f" ({po_name})" if po_name else ""
                return f"#{rank} {zip_code}{name_part}"

            county_zips["zip_label"] = county_zips.apply(build_zip_display_ranked, axis=1)
            county_zips = county_zips.drop(
                columns=["_target_sum", "_rank", "_pct", "geometry"], errors="ignore"
            )
        else:
            county_zips = county_zips.sort_values("ZIP_CODE").reset_index(drop=True)
            county_zips["zip_label"] = county_zips.apply(build_zip_display, axis=1)
            county_zips = county_zips.drop(columns=["_pct", "geometry"], errors="ignore")

        return county_zips

    zip_choices = zip_choices.drop(columns=["geometry"], errors="ignore")
    zip_choices = zip_choices.sort_values("ZIP_CODE").reset_index(drop=True)
    zip_choices["zip_label"] = zip_choices.apply(build_zip_display, axis=1)
    return zip_choices


def get_candidates_in_zip(candidates_df, selected_zip, zip_geom):
    if "zip_join" in candidates_df.columns and candidates_df["zip_join"].notna().any():
        return candidates_df[candidates_df["zip_join"] == selected_zip].copy()
    return candidates_df[candidates_df.geometry.intersects(zip_geom)].copy()


def get_demand_in_zip(demand_df, selected_zip, zip_geom):
    if "zip_join" in demand_df.columns and demand_df["zip_join"].notna().any():
        return demand_df[demand_df["zip_join"] == selected_zip].copy()
    return demand_df[demand_df.geometry.intersects(zip_geom)].copy()


def build_type_rows_html(types_in_zip, type_colors):
    """Use colored squares for site types."""
    rows = []
    for ftype in types_in_zip:
        fcolor = type_colors.get(ftype, "#808080")
        rows.append(
            f'<p style="margin:2px 0 2px 10px; display:flex; align-items:center;">'
            f'<span style="display:inline-block; width:12px; height:12px; '
            f'background-color:{fcolor}; border-radius:2px; margin-right:6px; '
            f'flex-shrink:0;"></span>'
            f'{ftype}</p>'
        )
    return "\n".join(rows)


def build_candidate_choice_label(row):
    cand_idx = int(row.get("cand_idx", -1))
    name = str(row.get("name", "")).strip() or f"Candidate {cand_idx}"
    ftype = str(row.get("type", "")).strip()
    address = str(row.get("address", "")).strip()
    parts = [name]
    if ftype:
        parts.append(ftype)
    if address:
        parts.append(address)
    return f"{cand_idx}: " + " | ".join(parts)


def candidate_status_is_infeasible(value):
    status = str(value).strip().lower()
    return status in INFEASIBLE_STATUS_VALUES


def pick_feasibility_columns(df):
    """Return feasibility columns that are present in a dataframe, mapped to display labels."""
    return {col: label for col, label in FEASIBILITY_COLUMNS.items() if col in df.columns}


def build_previous_deployment_location_df(
    is_first_deployment,
    previous_deployment_mode,
    previous_deployment_cand_idxs=None,
    previous_deployment_coordinates=None,
    candidates_zip_all=None,
):
    """Return dataframe rows for prior deployment locations, if configured."""
    if bool(is_first_deployment):
        return pd.DataFrame()

    mode = str(previous_deployment_mode or "").strip()

    if mode == "Select candidate site(s)":
        cand_idxs = []
        for cand_idx in previous_deployment_cand_idxs or []:
            try:
                cand_idxs.append(int(cand_idx))
            except Exception:
                continue

        if not cand_idxs or candidates_zip_all is None or len(candidates_zip_all) == 0:
            return pd.DataFrame()

        out = candidates_zip_all[
            candidates_zip_all["cand_idx"].astype(int).isin(set(cand_idxs))
        ].copy()
        if out.empty:
            return pd.DataFrame()

        # Preserve the order selected in the multiselect.
        order_lookup = {int(cand_idx): pos for pos, cand_idx in enumerate(cand_idxs)}
        out["_previous_order"] = out["cand_idx"].astype(int).map(order_lookup).fillna(999999)
        out = out.sort_values(["_previous_order", "name", "cand_idx"]).drop(columns=["_previous_order"], errors="ignore")

        if "name" not in out.columns:
            out["name"] = out["cand_idx"].apply(lambda x: f"Previous deployment {int(x)}")
        else:
            out["name"] = out["name"].fillna("").astype(str)
        if "type" not in out.columns:
            out["type"] = "Previous deployment"
        else:
            out["type"] = out["type"].fillna("Previous deployment").astype(str)
        out["previous_deployment_source"] = "candidate_site"
        return out

    if mode == "Enter coordinates":
        if previous_deployment_coordinates is None:
            return pd.DataFrame()

        coord_df = pd.DataFrame(previous_deployment_coordinates).copy()
        if coord_df.empty:
            return pd.DataFrame()

        rows = []
        for idx, row in coord_df.iterrows():
            try:
                lat = float(row.get("latitude", np.nan))
                lon = float(row.get("longitude", np.nan))
            except Exception:
                continue

            if not (np.isfinite(lat) and np.isfinite(lon)):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue

            raw_name = str(row.get("name", "")).strip()
            name = raw_name or f"Previous deployment location {len(rows) + 1}"
            rows.append({
                "cand_idx": -999999 - len(rows),
                "facility_id": f"previous_deployment_custom_{len(rows) + 1}",
                "name": name,
                "type": "Previous deployment",
                "address": "Custom coordinate",
                "latitude": lat,
                "longitude": lon,
                "previous_deployment_source": "custom_coordinates",
            })

        return pd.DataFrame(rows)

    return pd.DataFrame()


def previous_deployment_records_from_df(previous_deployment_df):
    """Convert previous deployment locations to lightweight records for session state/maps."""
    records = []
    if previous_deployment_df is None or len(previous_deployment_df) == 0:
        return records

    for _, row in previous_deployment_df.iterrows():
        lat = pd.to_numeric(row.get("latitude", np.nan), errors="coerce")
        lon = pd.to_numeric(row.get("longitude", np.nan), errors="coerce")
        if not (np.isfinite(lat) and np.isfinite(lon)):
            continue
        records.append({
            "cand_idx": int(row.get("cand_idx", -999999)) if pd.notna(row.get("cand_idx", np.nan)) else -999999,
            "name": str(row.get("name", "Previous deployment location")).strip() or "Previous deployment location",
            "type": str(row.get("type", "Previous deployment")).strip() or "Previous deployment",
            "address": str(row.get("address", "")).strip(),
            "latitude": float(lat),
            "longitude": float(lon),
        })
    return records


def apply_previous_deployment_adjustment(
    coverage_matrix,
    demand_weights,
    demand_reset,
    previous_deployment_df,
    time_threshold,
    travel_mode,
    use_network,
    G,
    network_access_direction=NETWORK_ACCESS_DIRECTION,
):
    """Zero out demand already covered by a previous deployment.

    The optimization then maximizes only newly reachable demand. The returned
    coverage matrix also has previous-covered demand columns zeroed out, so plan
    maps show new coverage rather than counting overlapping demand as newly
    covered.
    """
    n_dem = coverage_matrix.shape[1] if coverage_matrix is not None else 0
    base_weights = np.asarray(demand_weights, dtype=float)

    adjusted_matrix = np.asarray(coverage_matrix, dtype=np.uint8).copy()
    adjusted_weights = base_weights.copy()
    previous_mask = np.zeros(n_dem, dtype=bool)

    if previous_deployment_df is None or len(previous_deployment_df) == 0 or n_dem == 0:
        return adjusted_matrix, adjusted_weights, previous_mask, 0.0, float(adjusted_weights.sum())

    try:
        previous_coverage, _, _, _ = build_coverage_matrix(
            previous_deployment_df,
            demand_reset,
            time_threshold,
            network_type=travel_mode,
            use_network=use_network,
            G=G,
            return_travel_time=True,
            network_access_direction=network_access_direction,
        )
        if previous_coverage.shape[1] == n_dem and previous_coverage.shape[0] > 0:
            previous_mask = previous_coverage.astype(bool).any(axis=0)
    except Exception:
        previous_mask = np.zeros(n_dem, dtype=bool)

    previous_covered_value = float(base_weights[previous_mask].sum()) if np.any(previous_mask) else 0.0
    if np.any(previous_mask):
        adjusted_matrix[:, previous_mask] = 0
        adjusted_weights[previous_mask] = 0.0

    return adjusted_matrix, adjusted_weights, previous_mask, previous_covered_value, float(adjusted_weights.sum())


def make_analysis_params(
    selected_zip,
    target_var,
    selected_types,
    excluded_cand_ids,
    travel_mode,
    time_threshold,
    use_network,
    num_mhcs,
    num_alternative_plans,
    diversity_mode,
    run_resource_overview,
    sweep_max_mhcs,
    is_first_deployment,
    previous_deployment_mode,
    previous_deployment_cand_idxs,
    previous_deployment_coordinates,
    exclude_previous_deployment_site,
):
    """A hashable-ish dictionary used to detect stale analysis results."""
    cand_idx_key = tuple(sorted({int(x) for x in (previous_deployment_cand_idxs or [])}))

    coord_key_rows = []
    if previous_deployment_coordinates is not None:
        coord_df = pd.DataFrame(previous_deployment_coordinates)
        for _, row in coord_df.iterrows():
            try:
                lat = float(row.get("latitude", np.nan))
                lon = float(row.get("longitude", np.nan))
            except Exception:
                continue
            if not (np.isfinite(lat) and np.isfinite(lon)):
                continue
            name = str(row.get("name", "")).strip()
            coord_key_rows.append((name, round(lat, 6), round(lon, 6)))
    coord_key = tuple(coord_key_rows)

    return {
        "selected_zip": str(selected_zip),
        "target_var": str(target_var),
        "selected_types": tuple(sorted(map(str, selected_types))),
        "excluded_cand_ids": tuple(sorted(map(int, excluded_cand_ids))),
        "travel_mode": str(travel_mode),
        "time_threshold": int(time_threshold),
        "use_network": bool(use_network),
        "num_mhcs": int(num_mhcs),
        "num_alternative_plans": int(num_alternative_plans),
        "diversity_mode": str(diversity_mode),
        "run_resource_overview": bool(run_resource_overview),
        "sweep_max_mhcs": int(sweep_max_mhcs),
        "is_first_deployment": bool(is_first_deployment),
        "previous_deployment_mode": str(previous_deployment_mode),
        "previous_deployment_cand_idxs": cand_idx_key,
        "previous_deployment_coordinates": coord_key,
        "exclude_previous_deployment_site": bool(exclude_previous_deployment_site),
    }


def coverage_for_selection(selected_indices, coverage_matrix, demand_weights):
    """Return covered value and covered-demand mask for a selected set of candidate row indices."""
    n_dem = coverage_matrix.shape[1] if coverage_matrix is not None else 0

    if not selected_indices or n_dem == 0:
        return 0.0, np.zeros(n_dem, dtype=bool)

    covered_mask = coverage_matrix[list(selected_indices), :].astype(bool).any(axis=0)
    covered_value = float(np.asarray(demand_weights, dtype=float)[covered_mask].sum())
    return covered_value, covered_mask


def weighted_average_min_travel_time(
    selected_indices,
    coverage_matrix,
    demand_weights,
    travel_time_matrix=None,
):
    """Weighted average nearest travel time for demand covered by a selected plan.

    This is used both as a display metric and as the secondary ranking/tie-break
    criterion. The primary objective remains MCLP covered demand. Among plans
    with the same covered demand, lower weighted average nearest travel time is
    preferred. Demand weights are the selected target variable, so a high-need
    demand point has more influence on the average.
    """
    n_dem = coverage_matrix.shape[1] if coverage_matrix is not None else 0
    if not selected_indices or travel_time_matrix is None or n_dem == 0:
        return np.nan

    selected_indices = [int(i) for i in selected_indices]
    demand_weights = np.asarray(demand_weights, dtype=float)
    travel_time_matrix = np.asarray(travel_time_matrix, dtype=float)

    if travel_time_matrix.shape != coverage_matrix.shape:
        return np.nan

    _, covered_mask = coverage_for_selection(selected_indices, coverage_matrix, demand_weights)
    if not np.any(covered_mask):
        return np.nan

    selected_times = travel_time_matrix[selected_indices, :]
    with np.errstate(invalid="ignore"):
        nearest_times = np.nanmin(selected_times, axis=0)

    valid_mask = covered_mask & np.isfinite(nearest_times)
    if not np.any(valid_mask):
        return np.nan

    weights = demand_weights[valid_mask]
    times = nearest_times[valid_mask]

    if np.sum(weights) > 0:
        return float(np.average(times, weights=weights))

    return float(np.mean(times))


def add_plan_selection_constraints(model, x, n_fac, p, previous_solutions, diversity_mode):
    """Add facility-count and alternative-plan diversity constraints to a PuLP model."""
    model += lpSum(x[i] for i in range(n_fac)) == p

    normalized_previous = [tuple(sorted(map(int, s))) for s in (previous_solutions or [])]

    if diversity_mode == "No site reuse until necessary" and normalized_previous:
        used_sites = set()
        for sol in normalized_previous:
            used_sites.update(sol)

        remaining_count = n_fac - len(used_sites)
        if remaining_count >= p:
            model += lpSum(x[i] for i in used_sites) == 0
        else:
            for sol in normalized_previous:
                model += lpSum(x[i] for i in sol) <= max(0, p - 1)

    elif normalized_previous:
        overlap_limit = get_overlap_limit(p, diversity_mode)
        for sol in normalized_previous:
            model += lpSum(x[i] for i in sol) <= overlap_limit


def add_coverage_constraints(model, x, y, coverage_matrix):
    """Add standard MCLP coverage-linking constraints."""
    n_fac, n_dem = coverage_matrix.shape
    for j in range(n_dem):
        coverers = np.where(coverage_matrix[:, j] == 1)[0]
        if coverers.size:
            model += y[j] <= lpSum(x[int(i)] for i in coverers)
        else:
            model += y[j] == 0


def compute_site_metrics(selected_indices, coverage_matrix, demand_weights, candidates_reset):
    """
    Compute site-level coverage metrics.

    gross_covered_value:
        Demand covered by the site by itself.
    marginal_value:
        Coverage lost if this site is removed from the selected plan.
    covered_value:
        Backward-compatible alias for gross_covered_value.
    """
    site_metrics = {}
    selected_indices = [int(i) for i in selected_indices]

    if len(selected_indices) == 0:
        return site_metrics

    demand_weights = np.asarray(demand_weights, dtype=float)
    plan_covered_value, _ = coverage_for_selection(
        selected_indices, coverage_matrix, demand_weights
    )

    for row_idx in selected_indices:
        own_mask = coverage_matrix[row_idx, :].astype(bool)
        gross_value = float(demand_weights[own_mask].sum())

        others = [i for i in selected_indices if int(i) != int(row_idx)]
        if others:
            others_mask = coverage_matrix[others, :].astype(bool).any(axis=0)
        else:
            others_mask = np.zeros_like(own_mask, dtype=bool)

        without_value = float(demand_weights[others_mask].sum())
        marginal_value = float(max(plan_covered_value - without_value, 0.0))

        cand_idx = int(candidates_reset.iloc[row_idx]["cand_idx"])
        site_metrics[cand_idx] = {
            "gross_covered_value": gross_value,
            "marginal_value": marginal_value,
            "covered_value": gross_value,
        }

    return site_metrics


def build_deployment_plan(
    plan_rank,
    selected_indices,
    coverage_matrix,
    demand_weights,
    candidates_reset,
    demand_reset,
    target_var,
    total_target,
    best_covered_pop=None,
    travel_time_matrix=None,
):
    """Create a serializable-ish deployment-plan dictionary for Streamlit state."""
    selected_indices = [int(i) for i in selected_indices]
    covered_pop, covered_mask = coverage_for_selection(
        selected_indices, coverage_matrix, demand_weights
    )

    if selected_indices:
        sel_fac = candidates_reset.iloc[selected_indices].copy()
        sel_fac["_candidate_row"] = selected_indices
    else:
        sel_fac = candidates_reset.iloc[0:0].copy()
        sel_fac["_candidate_row"] = []

    selected_cand_ids = set(sel_fac["cand_idx"].astype(int).tolist()) if len(sel_fac) else set()
    covered_dem_ids = (
        set(demand_reset.loc[covered_mask, "dem_idx"].astype(int).tolist())
        if len(demand_reset) and "dem_idx" in demand_reset.columns
        else set()
    )

    site_metrics_lookup = compute_site_metrics(
        selected_indices=selected_indices,
        coverage_matrix=coverage_matrix,
        demand_weights=demand_weights,
        candidates_reset=candidates_reset,
    )

    avg_travel_time_min = weighted_average_min_travel_time(
        selected_indices=selected_indices,
        coverage_matrix=coverage_matrix,
        demand_weights=demand_weights,
        travel_time_matrix=travel_time_matrix,
    )

    coverage_pct = (covered_pop / total_target) * 100.0 if total_target > 0 else 0.0
    if best_covered_pop is None or best_covered_pop <= 0:
        loss_value = 0.0
        loss_pct_points = 0.0
        quality_pct = 100.0 if covered_pop > 0 else 0.0
    else:
        loss_value = float(best_covered_pop - covered_pop)
        best_pct = (best_covered_pop / total_target) * 100.0 if total_target > 0 else 0.0
        loss_pct_points = float(best_pct - coverage_pct)
        quality_pct = (covered_pop / best_covered_pop) * 100.0

    return {
        "plan_rank": int(plan_rank),
        "selected_indices": selected_indices,
        "selected_facilities": sel_fac,
        "covered_pop": float(covered_pop),
        "covered_mask": covered_mask,
        "selected_cand_ids": selected_cand_ids,
        "covered_dem_ids": covered_dem_ids,
        "site_metrics_lookup": site_metrics_lookup,
        "coverage_pct": float(coverage_pct),
        "loss_value": float(loss_value),
        "loss_pct_points": float(loss_pct_points),
        "quality_vs_best_pct": float(quality_pct),
        "avg_travel_time_min": avg_travel_time_min,
        "target_var": target_var,
    }


def attach_display_travel_times_to_plans(
    plans,
    coverage_matrix,
    demand_weights,
    travel_time_matrix=None,
):
    """Populate avg_travel_time_min for existing plan dictionaries.

    This helper is kept for backward compatibility with already generated plan
    structures. The main optimizer now also receives the travel-time matrix, so
    avg_travel_time_min should match the secondary ranking criterion whenever a
    valid travel-time matrix is available.
    """
    if not plans:
        return plans

    for plan in plans:
        plan["avg_travel_time_min"] = weighted_average_min_travel_time(
            selected_indices=plan.get("selected_indices", []),
            coverage_matrix=coverage_matrix,
            demand_weights=demand_weights,
            travel_time_matrix=travel_time_matrix,
        )

    return plans



def _plan_avg_time_sort_value(plan):
    """Return sortable average-travel-time value for a plan."""
    try:
        avg_time = float(plan.get("avg_travel_time_min", np.nan))
    except Exception:
        return np.inf
    return avg_time if np.isfinite(avg_time) else np.inf


def rerank_plans_by_coverage_then_travel_time(
    plans,
    coverage_matrix,
    demand_weights,
    candidates_reset,
    demand_reset,
    target_var,
    total_target,
    travel_time_matrix=None,
):
    """Sort generated plans by covered demand, then average travel time.

    The MCLP solve already maximizes coverage. This final pass makes the table
    order explicit and stable, especially when several plans cover the same
    demand value. Ranks and loss-vs-best metrics are rebuilt after sorting.
    """
    if not plans:
        return plans

    sortable = []
    for plan in plans:
        selected_indices = [int(i) for i in plan.get("selected_indices", [])]
        covered_value = float(plan.get("covered_pop", 0.0))
        avg_time = _plan_avg_time_sort_value(plan)
        selected_tuple = tuple(sorted(selected_indices))
        sortable.append((plan, covered_value, avg_time, selected_tuple))

    sortable.sort(key=lambda item: (-item[1], item[2], item[3]))
    best_covered_pop = max((item[1] for item in sortable), default=0.0)

    reranked = []
    for rank, (plan, _, _, _) in enumerate(sortable, start=1):
        reranked.append(
            build_deployment_plan(
                plan_rank=rank,
                selected_indices=plan.get("selected_indices", []),
                coverage_matrix=coverage_matrix,
                demand_weights=demand_weights,
                candidates_reset=candidates_reset,
                demand_reset=demand_reset,
                target_var=target_var,
                total_target=total_target,
                best_covered_pop=best_covered_pop,
                travel_time_matrix=travel_time_matrix,
            )
        )

    return reranked


def solve_single_site_rankings(
    coverage_matrix,
    demand_weights,
    num_alternative_plans,
    candidates_reset,
    demand_reset,
    target_var,
    total_target,
    travel_time_matrix=None,
):
    """For one MHC, rank sites by covered demand, then lower average travel time."""
    n_fac, n_dem = coverage_matrix.shape
    demand_weights = np.asarray(demand_weights, dtype=float)

    if n_fac == 0:
        return []

    scores = []
    for i in range(n_fac):
        if n_dem == 0:
            score = 0.0
        else:
            score = float(demand_weights[coverage_matrix[i, :].astype(bool)].sum())
        avg_time = weighted_average_min_travel_time(
            selected_indices=[i],
            coverage_matrix=coverage_matrix,
            demand_weights=demand_weights,
            travel_time_matrix=travel_time_matrix,
        )
        avg_time_sort = avg_time if np.isfinite(avg_time) else np.inf
        name = str(candidates_reset.iloc[i].get("name", ""))
        scores.append((i, score, avg_time_sort, name))

    scores = sorted(scores, key=lambda x: (-x[1], x[2], x[3], x[0]))
    best_score = scores[0][1] if scores else 0.0

    plans = []
    for rank, (row_idx, _, _, _) in enumerate(scores[: int(num_alternative_plans)], start=1):
        plans.append(
            build_deployment_plan(
                plan_rank=rank,
                selected_indices=[row_idx],
                coverage_matrix=coverage_matrix,
                demand_weights=demand_weights,
                candidates_reset=candidates_reset,
                demand_reset=demand_reset,
                target_var=target_var,
                total_target=total_target,
                best_covered_pop=best_score,
                travel_time_matrix=travel_time_matrix,
            )
        )

    return plans

def coverage_mask_too_similar(candidate_mask, accepted_masks, min_distinctness=COVERAGE_DISTINCTNESS_THRESHOLD):
    """
    Return True when a plan covers nearly the same demand points as a previously
    accepted plan. Distinctness is 1 - Jaccard similarity of covered-demand sets.
    """
    if not accepted_masks:
        return False

    cand = np.asarray(candidate_mask, dtype=bool)
    for prev_mask in accepted_masks:
        prev = np.asarray(prev_mask, dtype=bool)
        union = np.logical_or(cand, prev).sum()
        if union == 0:
            distance = 0.0
        else:
            jaccard = np.logical_and(cand, prev).sum() / union
            distance = 1.0 - float(jaccard)
        if distance < float(min_distinctness):
            return True
    return False


def get_overlap_limit(num_facilities, diversity_mode):
    """Return per-prior-plan overlap limit for alternative generation."""
    p = int(num_facilities)
    if diversity_mode == "Prefer distinct alternatives":
        return max(0, p // 2)
    return max(0, p - 1)



def build_pulp_solver_candidates(msg=False):
    """Return available PuLP solver objects, preferring the current CBC interface."""
    solvers = []
    for solver_cls in (COIN_CMD, PULP_CBC_CMD):
        if solver_cls is None:
            continue
        try:
            solvers.append(solver_cls(msg=1 if msg else 0))
        except TypeError:
            try:
                solvers.append(solver_cls(msg=bool(msg)))
            except Exception:
                continue
        except Exception:
            continue
    return solvers


def solve_pulp_model(model, msg=False):
    """Solve a PuLP model with a robust CBC/default-solver fallback."""
    last_error = None
    for solver in build_pulp_solver_candidates(msg=msg):
        try:
            return model.solve(solver)
        except Exception as exc:
            last_error = exc

    try:
        return model.solve()
    except Exception:
        if last_error is not None:
            raise last_error
        raise

def solve_maxcover_once(
    coverage_matrix,
    demand_weights,
    num_facilities,
    previous_solutions=None,
    diversity_mode="Rank by coverage only",
    travel_time_matrix=None,
):
    """
    Solve one MCLP instance with an optional lexicographic travel-time tie-breaker.

    Stage 1 maximizes covered demand. Stage 2 keeps that maximum covered-demand
    value and minimizes weighted travel time among tied coverage solutions when a
    valid travel_time_matrix is supplied. This gives the requested ranking logic:
    highest coverage first, then lowest average nearest travel time.
    """
    previous_solutions = previous_solutions or []

    n_fac, n_dem = coverage_matrix.shape
    demand_weights = np.asarray(demand_weights, dtype=float)
    p = int(num_facilities)

    if n_fac == 0 or p <= 0:
        return []

    p = min(p, n_fac)

    # If every candidate must be selected, there is only one possible plan.
    if p >= n_fac:
        selected_all = tuple(range(n_fac))
        if selected_all in {tuple(sorted(s)) for s in previous_solutions}:
            return []
        return list(selected_all)

    model = LpProblem("Max_Coverage", LpMaximize)
    x = LpVariable.dicts("facility", range(n_fac), cat="Binary")
    y = LpVariable.dicts("covered", range(n_dem), cat="Binary")

    coverage_expr = lpSum(demand_weights[j] * y[j] for j in range(n_dem)) if n_dem > 0 else 0
    model += coverage_expr

    add_plan_selection_constraints(model, x, n_fac, p, previous_solutions, diversity_mode)
    add_coverage_constraints(model, x, y, coverage_matrix)

    try:
        solve_pulp_model(model, msg=False)
    except Exception:
        return []

    status_name = LpStatus.get(model.status, "")
    if status_name not in {"Optimal", "Feasible"}:
        return []

    selected_stage1 = [
        i
        for i in range(n_fac)
        if x[i].varValue is not None and x[i].varValue > 0.5
    ]

    if len(selected_stage1) != p:
        return []

    best_coverage_value = value(coverage_expr) if n_dem > 0 else 0.0
    if best_coverage_value is None:
        best_coverage_value = coverage_for_selection(selected_stage1, coverage_matrix, demand_weights)[0]
    best_coverage_value = float(best_coverage_value)

    # Optional Stage 2: among maximum-coverage plans, minimize weighted travel time.
    if travel_time_matrix is None or n_dem == 0:
        return selected_stage1

    travel_time_matrix = np.asarray(travel_time_matrix, dtype=float)
    if travel_time_matrix.shape != coverage_matrix.shape:
        return selected_stage1

    finite_cover_pairs = [
        (int(i), int(j))
        for i, j in zip(*np.where(coverage_matrix == 1))
        if np.isfinite(travel_time_matrix[int(i), int(j)])
    ]
    if not finite_cover_pairs:
        return selected_stage1

    if len(finite_cover_pairs) > MAX_TIEBREAKER_ASSIGNMENT_PAIRS:
        # Keep the primary MCLP exact and skip only the secondary travel-time
        # tie-breaker when the assignment model would be too large for an
        # interactive Streamlit run.
        return selected_stage1

    tie_model = LpProblem("Max_Coverage_Min_Travel_Tie_Break", LpMinimize)
    x2 = LpVariable.dicts("facility", range(n_fac), cat="Binary")
    y2 = LpVariable.dicts("covered", range(n_dem), cat="Binary")
    z = {
        (i, j): LpVariable(f"assign_{i}_{j}", cat="Binary")
        for i, j in finite_cover_pairs
    }

    coverage_expr2 = lpSum(demand_weights[j] * y2[j] for j in range(n_dem))
    tie_model += coverage_expr2 >= best_coverage_value - 1e-6

    add_plan_selection_constraints(tie_model, x2, n_fac, p, previous_solutions, diversity_mode)
    add_coverage_constraints(tie_model, x2, y2, coverage_matrix)

    pairs_by_demand = {j: [] for j in range(n_dem)}
    for i, j in finite_cover_pairs:
        pairs_by_demand[j].append((i, j))
        tie_model += z[(i, j)] <= x2[i]

    for j in range(n_dem):
        if pairs_by_demand[j]:
            tie_model += lpSum(z[pair] for pair in pairs_by_demand[j]) == y2[j]
        else:
            tie_model += y2[j] == 0

    tie_model += lpSum(
        float(demand_weights[j]) * float(travel_time_matrix[i, j]) * z[(i, j)]
        for i, j in finite_cover_pairs
    )

    try:
        solve_pulp_model(tie_model, msg=False)
    except Exception:
        return selected_stage1

    tie_status_name = LpStatus.get(tie_model.status, "")
    if tie_status_name not in {"Optimal", "Feasible"}:
        return selected_stage1

    selected_stage2 = [
        i
        for i in range(n_fac)
        if x2[i].varValue is not None and x2[i].varValue > 0.5
    ]

    if len(selected_stage2) != p:
        return selected_stage1

    return selected_stage2

def solve_top_k_maxcover(
    coverage_matrix,
    demand_weights,
    num_facilities,
    num_alternative_plans,
    candidates_reset,
    demand_reset,
    target_var,
    total_target,
    diversity_mode="Rank by coverage only",
    travel_time_matrix=None,
):
    """
    Generate ranked deployment plans.

    - For 1 MHC, rank single candidate sites directly by covered demand.
    - For 2+ MHCs, repeatedly solve MCLP and add constraints/filters so the
      returned alternatives are useful backups rather than duplicate plans.

    Internal diversity modes:
      * Rank by coverage only: exact no-good cuts; next-best distinct site set.
      * Prefer distinct alternatives: limits site overlap with prior plans.
      * Prefer coverage-distinct alternatives: skips plans whose covered demand
        set is too similar to already accepted plans.
      * No site reuse until necessary: tries unused sites first, then relaxes.

    Default UI behavior ranks by highest coverage, then lower weighted average
    travel time, while preferring site-distinct backup plans. An advanced option
    can allow near-duplicate coverage-only backup plans when desired.
    """
    n_fac, _ = coverage_matrix.shape
    p = int(num_facilities)
    k = int(num_alternative_plans)

    if n_fac == 0 or p <= 0 or k <= 0:
        return []

    p = min(p, n_fac)

    if p == 1:
        return solve_single_site_rankings(
            coverage_matrix=coverage_matrix,
            demand_weights=demand_weights,
            num_alternative_plans=k,
            candidates_reset=candidates_reset,
            demand_reset=demand_reset,
            target_var=target_var,
            total_target=total_target,
            travel_time_matrix=travel_time_matrix,
        )

    previous_solutions = []
    accepted_masks = []
    seen = set()
    plans = []
    best_covered_pop = None

    solver_mode = diversity_mode
    if diversity_mode == "Prefer coverage-distinct alternatives":
        # Use ordinary no-good enumeration, then filter on covered-demand similarity.
        solver_mode = "Rank by coverage only"

    max_attempts = max(k * 8, k + 5)
    attempts = 0

    while len(plans) < k and attempts < max_attempts:
        attempts += 1
        selected_indices = solve_maxcover_once(
            coverage_matrix=coverage_matrix,
            demand_weights=demand_weights,
            num_facilities=p,
            previous_solutions=previous_solutions,
            diversity_mode=solver_mode,
            travel_time_matrix=travel_time_matrix,
        )

        if not selected_indices and solver_mode != "Rank by coverage only":
            # Strict diversity can become infeasible. Fall back to no-good cuts so
            # users still receive backup plans rather than an empty result.
            selected_indices = solve_maxcover_once(
                coverage_matrix=coverage_matrix,
                demand_weights=demand_weights,
                num_facilities=p,
                previous_solutions=previous_solutions,
                diversity_mode="Rank by coverage only",
                travel_time_matrix=travel_time_matrix,
            )

        if not selected_indices:
            break

        solution_tuple = tuple(sorted(map(int, selected_indices)))
        if solution_tuple in seen:
            break

        # Prevent this exact configuration from being produced again, even if it
        # is later skipped by coverage-similarity filtering.
        previous_solutions.append(solution_tuple)
        seen.add(solution_tuple)

        rank = len(plans) + 1
        plan = build_deployment_plan(
            plan_rank=rank,
            selected_indices=selected_indices,
            coverage_matrix=coverage_matrix,
            demand_weights=demand_weights,
            candidates_reset=candidates_reset,
            demand_reset=demand_reset,
            target_var=target_var,
            total_target=total_target,
            best_covered_pop=best_covered_pop,
            travel_time_matrix=travel_time_matrix,
        )

        if (
            diversity_mode == "Prefer coverage-distinct alternatives"
            and coverage_mask_too_similar(
                plan["covered_mask"],
                accepted_masks,
                min_distinctness=COVERAGE_DISTINCTNESS_THRESHOLD,
            )
        ):
            continue

        if best_covered_pop is None:
            best_covered_pop = plan["covered_pop"]
            plan["loss_value"] = 0.0
            plan["loss_pct_points"] = 0.0
            plan["quality_vs_best_pct"] = 100.0 if best_covered_pop > 0 else 0.0
        else:
            # Rebuild so loss/quality metrics are relative to the true best plan.
            plan = build_deployment_plan(
                plan_rank=rank,
                selected_indices=selected_indices,
                coverage_matrix=coverage_matrix,
                demand_weights=demand_weights,
                candidates_reset=candidates_reset,
                demand_reset=demand_reset,
                target_var=target_var,
                total_target=total_target,
                best_covered_pop=best_covered_pop,
                travel_time_matrix=travel_time_matrix,
            )

        plans.append(plan)
        accepted_masks.append(plan["covered_mask"])

    return rerank_plans_by_coverage_then_travel_time(
        plans=plans,
        coverage_matrix=coverage_matrix,
        demand_weights=demand_weights,
        candidates_reset=candidates_reset,
        demand_reset=demand_reset,
        target_var=target_var,
        total_target=total_target,
        travel_time_matrix=travel_time_matrix,
    )


def run_resource_sweep(
    coverage_matrix,
    demand_weights,
    candidates_reset,
    demand_reset,
    target_var,
    target_label,
    total_target,
    max_mhcs,
    travel_time_matrix=None,
):
    """
    Run exact best-plan MCLP for 1..max_mhcs.

    Each row answers: "what is the best possible coverage with p MHCs?"
    The marginal gain columns show the additional demand covered when moving
    from p-1 to p MHCs.
    """
    max_mhcs = int(max_mhcs)
    rows = []
    previous_exact_covered = 0.0
    covered_col = f"Covered {target_label}"

    for mhcs in range(1, max_mhcs + 1):
        plans = solve_top_k_maxcover(
            coverage_matrix=coverage_matrix,
            demand_weights=demand_weights,
            num_facilities=mhcs,
            num_alternative_plans=1,
            candidates_reset=candidates_reset,
            demand_reset=demand_reset,
            target_var=target_var,
            total_target=total_target,
            diversity_mode=COVERAGE_ONLY_DIVERSITY_MODE,
            # Coverage remains primary; travel time breaks ties among equal-coverage plans.
            travel_time_matrix=travel_time_matrix,
        )

        if not plans:
            continue

        plan = plans[0]
        exact_value = float(plan["covered_pop"])
        exact_pct = float(plan["coverage_pct"])
        exact_marginal = exact_value - previous_exact_covered
        exact_marginal_pct = (
            (exact_marginal / total_target) * 100.0 if total_target > 0 else 0.0
        )
        selected_facilities = plan.get("selected_facilities", pd.DataFrame())
        selected_names = "; ".join(
            str(x)
            for x in selected_facilities.get("name", pd.Series(dtype=str)).fillna("").tolist()
            if str(x).strip()
        )
        selected_types = "; ".join(
            str(x)
            for x in selected_facilities.get("type", pd.Series(dtype=str)).fillna("").tolist()
            if str(x).strip()
        )

        rows.append({
            "MHCs deployed": mhcs,
            covered_col: int(round(exact_value)),
            "Coverage %": round(exact_pct, 2),
            "Exact marginal gain": int(round(exact_marginal)),
            "Marginal gain pct points": round(exact_marginal_pct, 2),
            "Selected site names": selected_names,
            "Selected site types": selected_types,
        })

        previous_exact_covered = exact_value

    return pd.DataFrame(rows)


def format_plan_label(plan, target_label):
    rank = int(plan["plan_rank"])
    covered = int(round(plan["covered_pop"]))
    pct = float(plan["coverage_pct"])
    loss = float(plan["loss_pct_points"])
    if rank == 1:
        return f"Plan {rank} - best model solution ({covered:,}, {pct:.1f}%)"
    if abs(loss) < 0.005:
        return f"Plan {rank} - alternative with same coverage ({covered:,}, {pct:.1f}%)"
    return f"Plan {rank} - alternative ({covered:,}, {pct:.1f}%, -{loss:.1f} pts)"

def build_plan_summary_df(plans, target_label):
    rows = []

    for plan in plans:
        sel_fac = plan["selected_facilities"]
        site_names = "; ".join(
            str(x)
            for x in sel_fac.get("name", pd.Series(dtype=str)).fillna("").tolist()
            if str(x).strip()
        )
        site_types = "; ".join(
            str(x)
            for x in sel_fac.get("type", pd.Series(dtype=str)).fillna("").tolist()
            if str(x).strip()
        )
        avg_time = float(plan.get("avg_travel_time_min", np.nan))
        rows.append({
            "Plan": int(plan["plan_rank"]),
            "Sites in plan": int(len(sel_fac)),
            f"Covered {target_label}": int(round(float(plan["covered_pop"]))),
            "Coverage %": round(float(plan["coverage_pct"]), 2),
            "Avg nearest travel time (min)": round(avg_time, 2) if np.isfinite(avg_time) else np.nan,
            "Loss vs best": int(round(float(plan["loss_value"]))),
            "Site names": site_names,
            "Site types": site_types,
        })

    return pd.DataFrame(rows)


def build_plan_sites_df(plan, target_label):
    sel_fac = plan["selected_facilities"].copy()
    if len(sel_fac) == 0:
        return pd.DataFrame()

    site_metrics_lookup = plan["site_metrics_lookup"]
    gross_vals = []
    marginal_vals = []

    for _, row in sel_fac.iterrows():
        cand_idx = int(row["cand_idx"])
        metrics = site_metrics_lookup.get(cand_idx, {})
        gross_vals.append(int(round(float(metrics.get("gross_covered_value", 0.0)))))
        marginal_vals.append(int(round(float(metrics.get("marginal_value", 0.0)))))

    df_display = sel_fac.copy()
    df_display[f"Gross covered {target_label}"] = gross_vals
    df_display[f"Marginal contribution {target_label}"] = marginal_vals
    df_display = (
        df_display
        .sort_values(f"Marginal contribution {target_label}", ascending=False)
        .reset_index(drop=True)
    )
    df_display.insert(0, "Site Rank", range(1, len(df_display) + 1))
    return df_display




def build_recommended_site_review_df(plans, target_label):
    """
    Build a post-run review table containing only sites that appear in generated plans.

    The user can mark one or more recommended sites as infeasible/unavailable,
    then rerun the optimizer with those sites excluded. Rows are kept in first
    appearance order across the generated plans.
    """
    rows_by_cand = {}
    order = []
    marginal_col = f"Marginal contribution {target_label}"

    for plan in plans or []:
        plan_rank = int(plan.get("plan_rank", 0))
        plan_site_df = build_plan_sites_df(plan, target_label)
        if plan_site_df.empty:
            continue

        for _, row in plan_site_df.iterrows():
            try:
                cand_idx = int(row.get("cand_idx", -1))
            except Exception:
                continue
            if cand_idx < 0:
                continue

            if cand_idx not in rows_by_cand:
                rows_by_cand[cand_idx] = {
                    "Mark infeasible/unavailable": False,
                    "cand_idx": cand_idx,
                    "Site name": str(row.get("name", "")).strip(),
                    "Site type": str(row.get("type", "")).strip(),
                    "Appears in plans": [],
                    "Address": str(row.get("address", "")).strip(),
                    f"Max marginal contribution {target_label}": 0,
                }
                order.append(cand_idx)

            rows_by_cand[cand_idx]["Appears in plans"].append(plan_rank)
            try:
                marginal_value = int(round(float(row.get(marginal_col, 0))))
            except Exception:
                marginal_value = 0
            rows_by_cand[cand_idx][f"Max marginal contribution {target_label}"] = max(
                int(rows_by_cand[cand_idx][f"Max marginal contribution {target_label}"]),
                marginal_value,
            )

    rows = []
    for cand_idx in order:
        item = rows_by_cand[cand_idx]
        plan_numbers = sorted(set(int(p) for p in item["Appears in plans"] if int(p) > 0))
        item = dict(item)
        item["Appears in plans"] = "; ".join(f"Plan {p}" for p in plan_numbers)
        rows.append(item)

    return pd.DataFrame(rows)


def format_review_excluded_site_names(cand_ids, candidates_df, max_names=8):
    """Return a short display string for currently post-run-excluded sites."""
    cand_ids = set(map(int, cand_ids or []))
    if not cand_ids or candidates_df is None or len(candidates_df) == 0:
        return ""

    site_rows = candidates_df[
        candidates_df["cand_idx"].astype(int).isin(cand_ids)
    ].copy()
    if site_rows.empty:
        return ""

    labels = []
    for _, row in site_rows.sort_values(["name", "type", "cand_idx"]).iterrows():
        name = str(row.get("name", "")).strip() or f"Candidate {int(row.get('cand_idx', -1))}"
        ftype = str(row.get("type", "")).strip()
        labels.append(f"{name}" + (f" ({ftype})" if ftype else ""))

    if len(labels) > int(max_names):
        return ", ".join(labels[: int(max_names)]) + f", and {len(labels) - int(max_names)} more"
    return ", ".join(labels)


def build_all_plan_sites_export(plans, target_label):
    rows = []

    for plan in plans:
        plan_sites = build_plan_sites_df(plan, target_label)
        if plan_sites.empty:
            continue

        for _, row in plan_sites.iterrows():
            export_row = {
                "plan_rank": int(plan["plan_rank"]),
                "site_rank": int(row.get("Site Rank", 0)),
                "facility_id": row.get("facility_id", ""),
                "cand_idx": int(row.get("cand_idx", -1)),
                "name": row.get("name", ""),
                "type": row.get("type", ""),
                "address": row.get("address", ""),
                "latitude": row.get("latitude", np.nan),
                "longitude": row.get("longitude", np.nan),
                f"gross_covered_{target_label}": row.get(f"Gross covered {target_label}", 0),
                f"marginal_contribution_{target_label}": row.get(f"Marginal contribution {target_label}", 0),
                "plan_covered_value": int(round(float(plan["covered_pop"]))),
                "plan_coverage_pct": round(float(plan["coverage_pct"]), 2),
                "field_feasibility_status": "",
                "field_notes": "",
            }
            for raw_col, display_col in FEASIBILITY_COLUMNS.items():
                if raw_col in row.index:
                    export_row[display_col] = row.get(raw_col, "")
            rows.append(export_row)

    return pd.DataFrame(rows)


# ===========================
# DATA LOADING
# ===========================
@st.cache_data
def load_data(json_path: Path):
    with open(json_path, "r") as f:
        data = json.load(f)

    county_data = data.get("counties", {})
    county_gdf = None

    if county_data:
        geometries, properties = [], []
        for county_id, county_info in county_data.items():
            if "coords" not in county_info:
                continue
            try:
                coords_lonlat = [[pt[1], pt[0]] for pt in county_info["coords"]]
                geom = Polygon(coords_lonlat)
                geometries.append(geom)
                properties.append({
                    "COUNTY_FIPS": str(county_id),
                    "county_name": county_info.get("name", str(county_id)),
                })
            except Exception:
                continue
        if geometries:
            county_gdf = gpd.GeoDataFrame(properties, geometry=geometries, crs="EPSG:4326")

    zip_data = data.get("zip_boundaries", data.get("zips", {}))
    if not zip_data:
        raise ValueError("No ZIP boundaries found in JSON.")

    geometries, properties = [], []
    for zip_code, zip_info in zip_data.items():
        if "coords" not in zip_info:
            continue
        try:
            coords_lonlat = [[pt[1], pt[0]] for pt in zip_info["coords"]]
            geom = Polygon(coords_lonlat)
            geometries.append(geom)
            properties.append({
                "ZIP_CODE": str(zip_code).zfill(5),
                "po_name": zip_info.get("po_name", str(zip_code)),
            })
        except Exception:
            continue

    if not geometries:
        raise ValueError("No valid ZIP geometries.")

    zip_gdf = gpd.GeoDataFrame(properties, geometry=geometries, crs="EPSG:4326")

    candidates_data = data.get("candidate_facilities", data.get("facilities", []))
    if not candidates_data:
        raise ValueError("No candidate facilities found.")
    candidates_df = pd.DataFrame(candidates_data)

    demand_data = data.get("demand_points", data.get("demand", []))
    if not demand_data:
        raise ValueError("No demand points found.")
    demand_df = pd.DataFrame(demand_data)

    candidates_df["cand_idx"] = np.arange(len(candidates_df), dtype=int)
    demand_df["dem_idx"] = np.arange(len(demand_df), dtype=int)

    for df in (candidates_df, demand_df):
        if "zip_code" in df.columns:
            df["zip_code"] = df["zip_code"].astype(str).str.zfill(5)
        for col in ("latitude", "longitude"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

    for required_col in ("latitude", "longitude"):
        if required_col not in candidates_df.columns:
            raise ValueError(f"Candidate facilities are missing required column: {required_col}")
        if required_col not in demand_df.columns:
            raise ValueError(f"Demand points are missing required column: {required_col}")

    if "type" not in candidates_df.columns:
        candidates_df["type"] = "Candidate site"
    if "name" not in candidates_df.columns:
        candidates_df["name"] = candidates_df["cand_idx"].apply(lambda x: f"Candidate {x}")
    if "address" not in candidates_df.columns:
        candidates_df["address"] = ""

    if "feasibility_status" not in candidates_df.columns:
        candidates_df["feasibility_status"] = "Unknown"
    if "field_notes" not in candidates_df.columns:
        candidates_df["field_notes"] = ""

    for var_col in TARGET_VARIABLE_OPTIONS.values():
        if var_col in demand_df.columns:
            demand_df[var_col] = pd.to_numeric(demand_df[var_col], errors="coerce").fillna(0)

    candidates_gdf = gpd.GeoDataFrame(
        candidates_df,
        geometry=gpd.points_from_xy(candidates_df["longitude"], candidates_df["latitude"]),
        crs="EPSG:4326",
    )
    demand_gdf = gpd.GeoDataFrame(
        demand_df,
        geometry=gpd.points_from_xy(demand_df["longitude"], demand_df["latitude"]),
        crs="EPSG:4326",
    )

    try:
        _zip = zip_gdf[["ZIP_CODE", "geometry"]].copy()
        candidates_gdf = (
            gpd.sjoin(candidates_gdf, _zip, how="left", predicate="intersects")
            .drop(columns=["index_right"])
        )
        demand_gdf = (
            gpd.sjoin(demand_gdf, _zip, how="left", predicate="intersects")
            .drop(columns=["index_right"])
        )
        candidates_gdf = candidates_gdf.rename(columns={"ZIP_CODE": "zip_join"})
        demand_gdf = demand_gdf.rename(columns={"ZIP_CODE": "zip_join"})
    except Exception:
        if "zip_join" not in candidates_gdf.columns:
            candidates_gdf["zip_join"] = np.nan
        if "zip_join" not in demand_gdf.columns:
            demand_gdf["zip_join"] = np.nan

    zip_county_map = pd.DataFrame(columns=["ZIP_CODE", "COUNTY_FIPS", "county_name"])

    if county_gdf is not None:
        try:
            zip_county_join = (
                gpd.sjoin(
                    zip_gdf[["ZIP_CODE", "po_name", "geometry"]],
                    county_gdf[["COUNTY_FIPS", "county_name", "geometry"]],
                    how="left",
                    predicate="intersects",
                )
                .drop(columns=["index_right"])
            )
            zip_county_map = zip_county_join[
                ["ZIP_CODE", "COUNTY_FIPS", "county_name"]
            ].drop_duplicates()

            # Centroids in geographic CRS are sufficient for UI labeling here;
            # exact area/overlap logic is handled elsewhere.
            zip_centroids = zip_gdf.copy()
            zip_centroids["geometry"] = zip_centroids.geometry.centroid
            zip_primary = (
                gpd.sjoin(
                    zip_centroids[["ZIP_CODE", "geometry"]],
                    county_gdf[["COUNTY_FIPS", "county_name", "geometry"]],
                    how="left",
                    predicate="within",
                )
                .drop(columns=["index_right"])
            )
            zip_gdf["COUNTY_FIPS"] = zip_primary["COUNTY_FIPS"].values
            zip_gdf["county_name"] = zip_primary["county_name"].values
        except Exception:
            zip_gdf["COUNTY_FIPS"] = np.nan
            zip_gdf["county_name"] = np.nan

    all_types = candidates_gdf["type"].dropna().unique().tolist()
    global_type_colors = get_type_color_map(all_types)

    return (
        zip_gdf,
        candidates_gdf,
        demand_gdf,
        county_gdf,
        zip_county_map,
        global_type_colors,
    )


# ===========================
# COUNTY OVERVIEW MAP
# ===========================
def create_county_overview_map(
    county_gdf,
    zip_gdf,
    zip_county_map,
    selected_county_fips,
    tiles="CartoDB positron",
    demand_df=None,
    target_var=None,
    target_label=None,
):
    county_rows = county_gdf[
        county_gdf["COUNTY_FIPS"].astype(str) == str(selected_county_fips)
    ]
    if county_rows.empty:
        raise ValueError("Selected county not found.")

    county_row = county_rows.iloc[0]
    bounds = county_row.geometry.bounds
    center_lat = float((bounds[1] + bounds[3]) / 2)
    center_lon = float((bounds[0] + bounds[2]) / 2)

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=10,
        tiles=tiles,
        prefer_canvas=True,
    )

    folium.GeoJson(
        county_gdf.to_json(),
        style_function=lambda _: {
            "fillColor": "transparent",
            "color": "#cccccc",
            "weight": 1,
            "fillOpacity": 0,
            "opacity": 0.4,
        },
    ).add_to(m)

    folium.GeoJson(
        county_row.geometry.__geo_interface__,
        style_function=lambda _: {
            "fillColor": "transparent",
            "color": "#004b98",
            "weight": 4,
            "fillOpacity": 0,
            "opacity": 0.9,
        },
    ).add_to(m)

    county_geom = county_row.geometry
    try:
        pcts = calculate_zip_overlap_pcts(zip_gdf["geometry"], county_geom)
        zips_in_county = zip_gdf[pcts >= ZIP_COUNTY_OVERLAP_THRESHOLD].copy()
    except Exception:
        zips_in_county = zip_gdf.iloc[0:0].copy()

    zip_target_sum = {}
    if (
        demand_df is not None
        and target_var is not None
        and target_var in demand_df.columns
        and "zip_join" in demand_df.columns
        and demand_df["zip_join"].notna().any()
    ):
        demand_tmp = (
            demand_df[["zip_join", target_var]]
            .dropna(subset=["zip_join"])
            .copy()
        )
        demand_tmp["zip_join"] = demand_tmp["zip_join"].astype(str).str.zfill(5)
        demand_tmp[target_var] = pd.to_numeric(
            demand_tmp[target_var], errors="coerce"
        ).fillna(0)
        sums = demand_tmp.groupby("zip_join")[target_var].sum()
        zip_target_sum = {str(k).zfill(5): v for k, v in sums.items()}

    zips_in_county["_target_sum"] = (
        zips_in_county["ZIP_CODE"]
        .astype(str).str.zfill(5)
        .map(zip_target_sum)
        .fillna(0.0)
    )

    zips_in_county = (
        zips_in_county
        .sort_values(["_target_sum", "ZIP_CODE"], ascending=[False, True])
        .reset_index(drop=True)
    )
    zips_in_county["_rank"] = range(1, len(zips_in_county) + 1)
    n_zips = len(zips_in_county)

    def rank_to_color(rank, n):
        if n <= 1:
            return "#1a4f8a"
        t = (rank - 1) / (n - 1)
        r = int(26 + t * (210 - 26))
        g = int(79 + t * (233 - 79))
        b = int(138 + t * (255 - 138))
        return f"#{r:02x}{g:02x}{b:02x}"

    for _, zrow in zips_in_county.iterrows():
        geom = zrow.geometry
        if geom is None or geom.is_empty:
            continue

        rank = int(zrow["_rank"])
        target_val = float(zrow["_target_sum"])
        fill_color = rank_to_color(rank, n_zips)
        label_text = target_label if target_label else target_var

        feature = {
            "type": "Feature",
            "geometry": geom.__geo_interface__,
            "properties": {
                "ZIP_CODE": str(zrow["ZIP_CODE"]),
                "po_name": str(zrow.get("po_name", "")),
                "rank": rank,
                "target_val": int(round(target_val)),
            },
        }

        folium.GeoJson(
            feature,
            style_function=lambda _, fc=fill_color: {
                "fillColor": fc,
                "color": "#1E90FF",
                "weight": 2,
                "fillOpacity": 0.55,
                "opacity": 0.8,
            },
            highlight_function=lambda _: {
                "fillOpacity": 0.85,
                "weight": 4,
                "color": "#004b98",
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["ZIP_CODE", "po_name", "rank", "target_val"],
                aliases=["ZIP:", "Name:", "Rank:", f"{label_text}:"],
                localize=True,
            ),
        ).add_to(m)

        centroid = geom.centroid
        folium.Marker(
            location=[centroid.y, centroid.x],
            icon=folium.DivIcon(
                html=(
                    f'<div style="font-size:11px; font-weight:bold; color:#003366; '
                    f'text-shadow: 1px 1px 2px white, -1px -1px 2px white, '
                    f'1px -1px 2px white, -1px 1px 2px white; white-space:nowrap;">'
                    f'#{rank} {zrow["ZIP_CODE"]}</div>'
                ),
                icon_size=(70, 15),
                icon_anchor=(35, 7),
            ),
        ).add_to(m)

    if n_zips > 0:
        legend_html = f"""
        <div style="
            position: fixed; bottom: 40px; right: 40px; width: 200px;
            background-color: white; z-index:9999; font-size: 13px;
            border:1px solid #c7cfdb; border-radius: 10px; padding: 12px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.12);
        ">
            <p style="margin:0 0 8px 0; font-weight:bold; font-size:14px;">Need Ranking</p>
            <p style="margin:0 0 4px 0; color:#666; font-size:12px;">by {target_label or ""}</p>
            <div style="display:flex; align-items:center; margin-top:8px;">
                <div style="width:100%; height:16px; background: linear-gradient(to right, #1a4f8a, #d2e9ff); border-radius:4px;"></div>
            </div>
            <div style="display:flex; justify-content:space-between; font-size:11px; margin-top:3px;">
                <span>High need</span>
                <span>Low need</span>
            </div>
        </div>
        """
        m.get_root().html.add_child(folium.Element(legend_html))

    minx, miny, maxx, maxy = map(float, bounds)
    pad_x = max((maxx - minx) * 0.08, 0.01)
    pad_y = max((maxy - miny) * 0.08, 0.01)
    m.fit_bounds([[miny - pad_y, minx - pad_x], [maxy + pad_y, maxx + pad_x]])
    return m


# ===========================
# ZIP-LEVEL MAP
# ===========================
def add_candidate_square_marker(
    m,
    fac,
    color,
    opacity=1.0,
    size=12,
    border="1.5px solid rgba(0,0,0,0.3)",
    popup_prefix="Candidate site",
):
    lat, lon = float(fac["latitude"]), float(fac["longitude"])
    name = str(fac.get("name", ""))
    ftype = str(fac.get("type", ""))
    popup_html = f"<b>{name}</b><br>{ftype}<br><span style='color:#666;'>{popup_prefix}</span>"
    folium.Marker(
        location=[lat, lon],
        popup=popup_html,
        icon=folium.DivIcon(
            html=(
                f'<div style="width:{size}px; height:{size}px; '
                f'background-color:{color}; border-radius:2px; opacity:{opacity}; '
                f'border:{border};"></div>'
            ),
            icon_size=(size, size),
            icon_anchor=(size / 2, size / 2),
        ),
    ).add_to(m)


def add_selected_star_marker(
    m,
    fac,
    type_colors,
    target_label,
    site_metrics_lookup=None,
):
    lat, lon = float(fac["latitude"]), float(fac["longitude"])
    name = str(fac.get("name", ""))
    ftype = str(fac.get("type", ""))
    cand_idx = int(fac.get("cand_idx", -1))
    star_color = type_colors.get(ftype, "#808080")
    metrics = (site_metrics_lookup or {}).get(cand_idx, {})

    gross_value = float(metrics.get("gross_covered_value", metrics.get("covered_value", 0.0)))
    marginal_value = float(metrics.get("marginal_value", 0.0))

    popup_html = f"""
    <div style="width: 265px; font-family: Arial, sans-serif; font-size: 13px; line-height: 1.35;">
        <div style="font-weight: 700; color: #1f2d3d; margin-bottom: 4px;">Proposed Site</div>
        <div style="font-weight: 700; color: #004b98; margin-bottom: 2px;">{name}</div>
        <div style="color: #55606e; margin-bottom: 8px;">{ftype}</div>
        <table style="width: 100%; border-collapse: collapse;">
            <tr>
                <td style="padding: 3px 0; color: #55606e; vertical-align: top;">Gross reachable {target_label}</td>
                <td style="padding: 3px 0; text-align: right; font-weight: 700; color: #1f2d3d;">{gross_value:,.0f}</td>
            </tr>
            <tr>
                <td style="padding: 3px 0; color: #55606e; vertical-align: top;">Marginal contribution</td>
                <td style="padding: 3px 0; text-align: right; font-weight: 700; color: #1f2d3d;">{marginal_value:,.0f}</td>
            </tr>
        </table>
    </div>
    """

    folium.Marker(
        location=[lat, lon],
        popup=folium.Popup(popup_html, max_width=310),
        icon=folium.DivIcon(
            html=(
                f'<div style="font-size:38px; line-height:38px; color:{star_color}; '
                f'text-shadow:0 0 5px rgba(255,255,255,0.95), 0 0 2px rgba(0,0,0,0.55);">'
                f'&#9733;</div>'
            ),
            icon_size=(38, 38),
            icon_anchor=(19, 19),
        ),
    ).add_to(m)


def add_previous_deployment_marker(m, loc, size=30):
    """Add a compact marker for an already deployed MHC location."""
    try:
        lat = float(loc.get("latitude"))
        lon = float(loc.get("longitude"))
    except Exception:
        return
    if not (np.isfinite(lat) and np.isfinite(lon)):
        return

    name = html.escape(str(loc.get("name", "Previous deployment location")).strip() or "Previous deployment location")
    ftype = html.escape(str(loc.get("type", "Previous deployment")).strip() or "Previous deployment")
    popup_html = (
        f"<b>Previous deployment</b><br>{name}<br>"
        f"<span style='color:#666;'>{ftype}</span>"
    )
    folium.Marker(
        location=[lat, lon],
        popup=folium.Popup(popup_html, max_width=260),
        tooltip=f"Previous deployment: {name}",
        icon=folium.DivIcon(
            html=(
                f'<div style="width:{size}px; height:{size}px; transform: rotate(45deg); '
                f'background:{PREVIOUS_SITE_COLOR}; color:white; display:flex; '
                f'align-items:center; justify-content:center; font-size:12px; '
                f'font-weight:900; border:3px solid white; '
                f'box-shadow:0 2px 8px rgba(0,0,0,0.45);">'
                f'<span style="transform: rotate(-45deg); display:block;">P</span></div>'
            ),
            icon_size=(size, size),
            icon_anchor=(size / 2, size / 2),
        ),
    ).add_to(m)


def _expand_leaflet_bounds_from_xy(
    minx,
    miny,
    maxx,
    maxy,
    pad_ratio=0.08,
    min_span_degrees=0.01,
):
    """Return Leaflet [[south, west], [north, east]] bounds with padding."""
    try:
        minx, miny, maxx, maxy = map(float, [minx, miny, maxx, maxy])
    except Exception:
        return None

    if maxx < minx:
        minx, maxx = maxx, minx
    if maxy < miny:
        miny, maxy = maxy, miny

    center_x = (minx + maxx) / 2.0
    center_y = (miny + maxy) / 2.0
    span_x = max(maxx - minx, float(min_span_degrees))
    span_y = max(maxy - miny, float(min_span_degrees))

    minx = center_x - span_x / 2.0
    maxx = center_x + span_x / 2.0
    miny = center_y - span_y / 2.0
    maxy = center_y + span_y / 2.0

    pad_x = span_x * float(pad_ratio)
    pad_y = span_y * float(pad_ratio)
    return [[miny - pad_y, minx - pad_x], [maxy + pad_y, maxx + pad_x]]


def build_plan_comparison_focus_bounds(zip_gdf, selected_zip, plans, previous_deployment_locations=None):
    """
    Build one shared, zoomed-in viewport for all compact plan cards.

    The previous thumbnail maps fitted the entire ZIP boundary into a very wide,
    short card. That made the ZIP and the selected sites look tiny. This shared
    viewport focuses on the selected sites across all generated plans, while the
    ZIP boundary is still drawn for context.
    """
    zip_boundary = zip_gdf[zip_gdf["ZIP_CODE"] == selected_zip].iloc[0]
    zip_minx, zip_miny, zip_maxx, zip_maxy = map(float, zip_boundary.geometry.bounds)
    zip_span = max(zip_maxx - zip_minx, zip_maxy - zip_miny, COMPACT_PLAN_MIN_FOCUS_SPAN_DEGREES)
    focus_min_span = min(
        max(zip_span * 0.25, COMPACT_PLAN_MIN_FOCUS_SPAN_DEGREES),
        COMPACT_PLAN_MAX_MIN_FOCUS_SPAN_DEGREES,
    )

    lats = []
    lons = []
    for plan in plans or []:
        selected_facilities = plan.get("selected_facilities", pd.DataFrame())
        if selected_facilities is None or len(selected_facilities) == 0:
            continue
        for _, fac in selected_facilities.iterrows():
            lat = pd.to_numeric(fac.get("latitude", np.nan), errors="coerce")
            lon = pd.to_numeric(fac.get("longitude", np.nan), errors="coerce")
            if np.isfinite(lat) and np.isfinite(lon):
                lats.append(float(lat))
                lons.append(float(lon))

    for loc in previous_deployment_locations or []:
        lat = pd.to_numeric(loc.get("latitude", np.nan), errors="coerce")
        lon = pd.to_numeric(loc.get("longitude", np.nan), errors="coerce")
        if np.isfinite(lat) and np.isfinite(lon):
            lats.append(float(lat))
            lons.append(float(lon))

    if lats and lons:
        return _expand_leaflet_bounds_from_xy(
            min(lons),
            min(lats),
            max(lons),
            max(lats),
            pad_ratio=COMPACT_PLAN_FOCUS_PADDING_RATIO,
            min_span_degrees=focus_min_span,
        )

    return _expand_leaflet_bounds_from_xy(
        zip_minx,
        zip_miny,
        zip_maxx,
        zip_maxy,
        pad_ratio=0.04,
        min_span_degrees=COMPACT_PLAN_MIN_FOCUS_SPAN_DEGREES,
    )


def create_plan_thumbnail_map(
    zip_gdf,
    selected_zip,
    plan,
    type_colors,
    tiles="CartoDB positron",
    comparison_bounds=None,
    previous_deployment_locations=None,
):
    """
    Interactive comparison map for one deployment plan.

    This intentionally stays uncluttered: it shows only the ZIP boundary and the
    selected deployment site(s). The default viewport is now zoomed to the shared
    selected-site area across the compared plans, not the entire ZIP extent, so
    the recommended locations are large enough to compare at a glance.
    """
    zip_boundary = zip_gdf[zip_gdf["ZIP_CODE"] == selected_zip].iloc[0]
    zip_bounds = tuple(map(float, zip_boundary.geometry.bounds))
    minx, miny, maxx, maxy = zip_bounds

    if comparison_bounds:
        center_lat = float((comparison_bounds[0][0] + comparison_bounds[1][0]) / 2.0)
        center_lon = float((comparison_bounds[0][1] + comparison_bounds[1][1]) / 2.0)
        zoom_start = 12
    else:
        center_lat = float((miny + maxy) / 2.0)
        center_lon = float((minx + maxx) / 2.0)
        zoom_start = 10

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=zoom_start,
        tiles=tiles,
        prefer_canvas=True,
        control_scale=True,
        zoom_control=True,
        dragging=True,
        scrollWheelZoom=True,
        doubleClickZoom=True,
        touchZoom=True,
        boxZoom=True,
        keyboard=True,
    )

    try:
        Fullscreen(
            position="topright",
            title="Expand map",
            title_cancel="Exit full screen",
            force_separate_button=True,
        ).add_to(m)
    except Exception:
        pass

    folium.GeoJson(
        zip_boundary.geometry.__geo_interface__,
        style_function=lambda _: {
            "fillColor": "#E6F2FF",
            "color": "#1E90FF",
            "weight": 3,
            "fillOpacity": 0.06,
            "opacity": 0.9,
        },
    ).add_to(m)

    selected_facilities = plan.get("selected_facilities", pd.DataFrame())
    for marker_num, (_, fac) in enumerate(selected_facilities.iterrows(), start=1):
        try:
            lat, lon = float(fac["latitude"]), float(fac["longitude"])
        except Exception:
            continue
        if not (np.isfinite(lat) and np.isfinite(lon)):
            continue

        name = str(fac.get("name", "")).strip() or f"Site {marker_num}"
        ftype = str(fac.get("type", "")).strip()
        marker_color = type_colors.get(ftype, "#004b98")
        popup_html = (
            f"<b>Plan {int(plan.get('plan_rank', 0))}, Site {marker_num}</b><br>"
            f"{html.escape(name)}<br>"
            f"<span style='color:#666;'>{html.escape(ftype)}</span>"
        )
        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup(popup_html, max_width=260),
            tooltip=f"Plan {int(plan.get('plan_rank', 0))} · Site {marker_num}: {html.escape(name)}",
            icon=folium.DivIcon(
                html=(
                    f'<div style="width:32px; height:32px; border-radius:50%; '
                    f'background:{marker_color}; color:white; display:flex; '
                    f'align-items:center; justify-content:center; font-size:13px; '
                    f'font-weight:900; border:3px solid white; '
                    f'box-shadow:0 2px 8px rgba(0,0,0,0.45);">{marker_num}</div>'
                ),
                icon_size=(32, 32),
                icon_anchor=(16, 16),
            ),
        ).add_to(m)

    for loc in previous_deployment_locations or []:
        add_previous_deployment_marker(m, loc, size=28)

    if comparison_bounds:
        m.fit_bounds(
            comparison_bounds,
            padding=(28, 28),
            max_zoom=COMPACT_PLAN_SINGLE_SITE_ZOOM,
        )
    else:
        fallback_bounds = _expand_leaflet_bounds_from_xy(
            minx,
            miny,
            maxx,
            maxy,
            pad_ratio=0.04,
            min_span_degrees=COMPACT_PLAN_MIN_FOCUS_SPAN_DEGREES,
        )
        if fallback_bounds:
            m.fit_bounds(fallback_bounds, padding=(18, 18), max_zoom=12)

    return m

def build_plan_card_header_html(plan, target_label):
    """Small HTML header used above each compact plan map."""
    rank = int(plan["plan_rank"])
    covered = int(round(float(plan["covered_pop"])))
    pct = float(plan["coverage_pct"])
    loss_pts = float(plan.get("loss_pct_points", 0.0))

    badge_class = "plan-alt-badge plan-best-badge" if rank == 1 else "plan-alt-badge"
    if rank == 1:
        badge_text = "Best model solution"
    elif abs(loss_pts) < 0.005:
        badge_text = "Alternative, same coverage"
    else:
        badge_text = f"Alternative, -{loss_pts:.1f} pts"

    return f"""
    <div class="plan-compare-header">
        <div>
            <p class="plan-rank-title">
                <span class="plan-rank-badge">Plan {rank}</span>
                <span class="{badge_class}">{html.escape(badge_text)}</span>
            </p>
            <div class="plan-muted-text">Interactive map zooms to the selected deployment area. Use +/-, scroll, or full-screen to inspect.</div>
        </div>
        <div style="text-align:right; min-width: 180px;">
            <div style="font-weight:800; color:#1f2d3d;">{covered:,}</div>
            <div class="plan-muted-text">Covered {html.escape(target_label)} · {pct:.1f}%</div>
        </div>
    </div>
    """


def get_bordered_container():
    """Use Streamlit's bordered container when available, with a safe fallback."""
    try:
        return st.container(border=True)
    except TypeError:
        return st.container()


def create_map(
    zip_gdf,
    selected_zip,
    candidates_df,
    demand_df,
    type_colors,
    target_var="uninsured_pop",
    target_label="Uninsured Population",
    selected_cand_ids=None,
    covered_dem_ids=None,
    site_metrics_lookup=None,
    show_demand_preview=False,
    selected_types=None,
    eligible_cand_ids=None,
    tiles="CartoDB positron",
    county_gdf=None,
    show_other_candidates_after_analysis=True,
    previous_covered_dem_ids=None,
    previous_deployment_locations=None,
):
    zip_boundary = zip_gdf[zip_gdf["ZIP_CODE"] == selected_zip].iloc[0]
    bounds = zip_boundary.geometry.bounds
    center_lat = float((bounds[1] + bounds[3]) / 2)
    center_lon = float((bounds[0] + bounds[2]) / 2)

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=10,
        tiles=tiles,
        prefer_canvas=True,
    )

    if county_gdf is not None:
        folium.GeoJson(
            county_gdf.to_json(),
            style_function=lambda _: {
                "fillColor": "transparent",
                "color": "#999999",
                "weight": 1.5,
                "fillOpacity": 0,
                "opacity": 0.5,
            },
        ).add_to(m)

    folium.GeoJson(
        zip_boundary.geometry.__geo_interface__,
        style_function=lambda _: {
            "fillColor": "#E6F2FF",
            "color": "#1E90FF",
            "weight": 4,
            "fillOpacity": 0.08,
            "opacity": 0.9,
        },
    ).add_to(m)

    candidates_in_zip = get_candidates_in_zip(
        candidates_df, selected_zip, zip_boundary.geometry
    )
    if selected_types:
        candidates_in_zip = candidates_in_zip[
            candidates_in_zip["type"].isin(selected_types)
        ].copy()

    if eligible_cand_ids is not None:
        eligible_ids = set(map(int, eligible_cand_ids))
        candidates_in_zip = candidates_in_zip[
            candidates_in_zip["cand_idx"].astype(int).isin(eligible_ids)
        ].copy()

    demand_in_zip = get_demand_in_zip(demand_df, selected_zip, zip_boundary.geometry)
    types_in_zip = sorted(candidates_in_zip["type"].dropna().unique())
    analysis_complete = selected_cand_ids is not None and covered_dem_ids is not None
    previous_covered_dem_ids = set(previous_covered_dem_ids or [])
    previous_deployment_locations = previous_deployment_locations or []

    # 1. Candidate layer. After analysis, non-selected candidates are muted and optional.
    selected_rows = []
    if analysis_complete:
        for _, fac in candidates_in_zip.iterrows():
            cand_idx = int(fac.get("cand_idx", -1))
            is_selected = cand_idx in selected_cand_ids
            if is_selected:
                selected_rows.append(fac)
            elif show_other_candidates_after_analysis:
                ftype = str(fac.get("type", ""))
                color = type_colors.get(ftype, MUTED_CANDIDATE_COLOR)
                add_candidate_square_marker(
                    m,
                    fac,
                    color=color,
                    opacity=0.25,
                    size=9,
                    border="1px solid rgba(0,0,0,0.12)",
                    popup_prefix="Not selected in this plan",
                )
    else:
        for _, fac in candidates_in_zip.iterrows():
            ftype = str(fac.get("type", ""))
            color = type_colors.get(ftype, "#808080")
            add_candidate_square_marker(
                m,
                fac,
                color=color,
                opacity=1.0,
                size=12,
                border="1.5px solid rgba(0,0,0,0.3)",
                popup_prefix="Candidate site",
            )

    # 2. Demand layer. These become the main visual story after analysis.
    if analysis_complete or show_demand_preview:
        for _, dem in demand_in_zip.iterrows():
            lat, lon = float(dem["latitude"]), float(dem["longitude"])
            target_val = (
                float(dem[target_var])
                if pd.notna(dem.get(target_var, np.nan))
                else 0.0
            )

            if analysis_complete:
                dem_idx = int(dem.get("dem_idx", -1))
                is_previously_covered = dem_idx in previous_covered_dem_ids
                is_covered = dem_idx in covered_dem_ids
                if is_previously_covered:
                    color, fill_color = PREVIOUS_COVERED_COLOR, PREVIOUS_COVERED_COLOR
                    popup_text = f"<b>Already covered by previous deployment</b><br>{target_label}: {target_val:,.1f}"
                    fill_opacity = 0.58
                elif is_covered:
                    color, fill_color = COVERED_COLOR, COVERED_COLOR
                    popup_text = f"<b>Newly covered within travel time</b><br>{target_label}: {target_val:,.1f}"
                    fill_opacity = 0.78
                else:
                    color, fill_color = UNCOVERED_COLOR, UNCOVERED_COLOR
                    popup_text = f"<b>Outside new deployment travel time</b><br>{target_label}: {target_val:,.1f}"
                    fill_opacity = 0.78
                radius = 4
            else:
                color, fill_color = UNCOVERED_COLOR, UNCOVERED_COLOR
                popup_text = f"<b>Census block centroid</b><br>{target_label}: {target_val:,.1f}"
                radius = 3
                fill_opacity = 0.65

            folium.CircleMarker(
                location=[lat, lon],
                radius=radius,
                popup=popup_text,
                color=color,
                fill=True,
                fillColor=fill_color,
                fillOpacity=fill_opacity,
                weight=1,
            ).add_to(m)

    # 3. Previous deployment and selected sites on top.
    if analysis_complete:
        for loc in previous_deployment_locations:
            add_previous_deployment_marker(m, loc, size=30)

        for fac in selected_rows:
            add_selected_star_marker(
                m,
                fac=fac,
                type_colors=type_colors,
                target_label=target_label,
                site_metrics_lookup=site_metrics_lookup,
            )

    type_rows = build_type_rows_html(types_in_zip, type_colors)

    if analysis_complete:
        candidate_note = (
            '<p style="margin:3px 0; font-size:13px; color:#617184;">'
            'Other candidate sites are muted for context.</p>'
            if show_other_candidates_after_analysis
            else '<p style="margin:3px 0; font-size:13px; color:#617184;">Other candidate sites hidden.</p>'
        )
        previous_legend_html = ""
        if previous_covered_dem_ids or previous_deployment_locations:
            previous_legend_html = f"""
            <p style="margin:3px 0; font-size:14px;">
                <span style="color:{PREVIOUS_COVERED_COLOR}; font-size:16px;">&#9679;</span>
                &nbsp;Census blocks already covered by previous deployment
            </p>
            <p style="margin:3px 0; font-size:14px;">
                <span style="color:{PREVIOUS_SITE_COLOR}; font-size:18px;">◆</span>
                &nbsp;Previous deployment site
            </p>
            """
        legend_html = f"""
        <div style="
            position: fixed; bottom: 40px; right: 40px; width: 315px;
            max-height: 480px; overflow-y: auto; background-color: white; z-index:9999;
            font-size: 14px; border:1px solid #c7cfdb; border-radius: 10px; padding: 14px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.12);
        ">
            <p style="margin:0 0 8px 0; font-weight:bold; font-size:15px;">Map Legend</p>
            <p style="margin:3px 0; font-size:14px;">
                <span style="color:#555; font-size:18px;">&#9733;</span>
                &nbsp;Proposed site (color matches site type)
            </p>
            <p style="margin:3px 0; font-size:14px;">
                <span style="color:{COVERED_COLOR}; font-size:16px;">&#9679;</span>
                &nbsp;Census blocks within travel time
            </p>
            <p style="margin:3px 0; font-size:14px;">
                <span style="color:{UNCOVERED_COLOR}; font-size:16px;">&#9679;</span>
                &nbsp;Census blocks outside travel time
            </p>
            {previous_legend_html}
            {candidate_note}
            <details style="margin-top:10px;">
                <summary style="cursor:pointer; font-size:14px;"><b>Candidate site types ({len(types_in_zip)})</b></summary>
                {type_rows}
            </details>
        </div>
        """
    else:
        if show_demand_preview:
            demand_line = (
                f'<p style="margin:3px 0; font-size:14px;">'
                f'<span style="color:{UNCOVERED_COLOR}; font-size:16px;">&#9679;</span>'
                f'&nbsp;Census block centroids</p>'
            )
        else:
            demand_line = '<p style="margin:3px 0; color:#617184; font-size:14px;">Demand preview hidden</p>'

        legend_html = f"""
        <div style="
            position: fixed; bottom: 40px; right: 40px; width: 300px;
            max-height: 460px; overflow-y: auto; background-color: white; z-index:9999;
            font-size: 14px; border:1px solid #c7cfdb; border-radius: 10px; padding: 14px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.12);
        ">
            <p style="margin:0 0 8px 0; font-weight:bold; font-size:15px;">Map Legend</p>
            {demand_line}
            <details style="margin-top:10px;" open>
                <summary style="cursor:pointer; font-size:14px;"><b>Candidate sites by type ({len(types_in_zip)})</b></summary>
                {type_rows}
            </details>
        </div>
        """

    m.get_root().html.add_child(folium.Element(legend_html))

    minx, miny, maxx, maxy = map(float, bounds)
    pad_x = min(max((maxx - minx) * 0.08, 0.0015), 0.25)
    pad_y = min(max((maxy - miny) * 0.08, 0.0015), 0.25)
    m.fit_bounds([[miny - pad_y, minx - pad_x], [maxy + pad_y, maxx + pad_x]])
    return m


# ===========================
# NETWORK / COVERAGE / SOLVER
# ===========================
def snap_points_to_nodes(G, lons, lats, max_snap_dist_m=MAX_SNAP_DIST_M):
    lons = np.asarray(lons, dtype=float)
    lats = np.asarray(lats, dtype=float)
    out = np.empty(len(lons), dtype=object)

    try:
        nodes, dists = ox.distance.nearest_nodes(G, X=lons, Y=lats, return_dist=True)
        nodes = np.asarray(nodes, dtype=object)
        dists = np.asarray(dists, dtype=float)
        nodes[dists > max_snap_dist_m] = None
        out[:] = nodes
        return out
    except Exception:
        for i, (lo, la) in enumerate(zip(lons, lats)):
            try:
                n, d = ox.distance.nearest_nodes(G, lo, la, return_dist=True)
                out[i] = n if d <= max_snap_dist_m else None
            except Exception:
                out[i] = None
        return out


def estimate_required_graph_dist_m(
    center_lat, center_lon, candidates_df, demand_df, min_dist=15000, buffer_m=5000
):
    pts = []
    if candidates_df is not None and len(candidates_df) > 0:
        pts.append(candidates_df[["latitude", "longitude"]])
    if demand_df is not None and len(demand_df) > 0:
        pts.append(demand_df[["latitude", "longitude"]])
    if not pts:
        return int(min_dist)

    allpts = pd.concat(pts, ignore_index=True).dropna()
    if allpts.empty:
        return int(min_dist)

    try:
        d = ox.distance.great_circle_vec(
            center_lat, center_lon, allpts["latitude"].values, allpts["longitude"].values
        )
        maxd = float(np.nanmax(d))
    except Exception:
        lat1, lon1 = np.radians(center_lat), np.radians(center_lon)
        lat2 = np.radians(allpts["latitude"].values.astype(float))
        lon2 = np.radians(allpts["longitude"].values.astype(float))
        a = (
            np.sin((lat2 - lat1) / 2) ** 2
            + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
        )
        maxd = float(np.nanmax(6371000 * 2 * np.arcsin(np.sqrt(a))))

    return int(max(min_dist, maxd + buffer_m)) if np.isfinite(maxd) else int(min_dist)


def _looks_like_osmnx_cache_error(exc):
    msg = str(exc)
    return (
        "cache" in msg.lower()
        or "WinError 433" in msg
        or "device which does not exist" in msg.lower()
        or "no such device" in msg.lower()
        or "the system cannot find the path" in msg.lower()
    )


def _edge_highway_type(data):
    rt = data.get("highway", "unclassified")
    if isinstance(rt, list):
        rt = rt[0] if rt else "unclassified"
    return str(rt)


def preprocess_network_speeds(G, network_type="drive"):
    """Attach a minutes-based travel-time weight used by the coverage model."""
    if network_type == "walk":
        for _, _, _, data in G.edges(data=True, keys=True):
            length_m = float(data.get("length", 0.0) or 0.0)
            seconds = (length_m / 1000.0 / WALKING_SPEED_KMH) * 3600.0
            data["speed_kph"] = WALKING_SPEED_KMH
            data[NETWORK_TRAVEL_TIME_WEIGHT] = (seconds + TURN_PENALTY_SECONDS) / 60.0
        return G

    try:
        G = ox.routing.add_edge_speeds(
            G,
            hwy_speeds=SC_HIGHWAY_SPEEDS_KMH,
            fallback=SC_FALLBACK_SPEED_KMH,
        )
        G = ox.routing.add_edge_travel_times(G)
    except Exception:
        try:
            G = ox.add_edge_speeds(
                G,
                hwy_speeds=SC_HIGHWAY_SPEEDS_KMH,
                fallback=SC_FALLBACK_SPEED_KMH,
            )
            G = ox.add_edge_travel_times(G)
        except Exception:
            for _, _, _, data in G.edges(data=True, keys=True):
                length_m = float(data.get("length", 0.0) or 0.0)
                speed_kph = float(
                    data.get(
                        "speed_kph",
                        SC_HIGHWAY_SPEEDS_KMH.get(_edge_highway_type(data), SC_FALLBACK_SPEED_KMH),
                    )
                    or SC_FALLBACK_SPEED_KMH
                )
                data["speed_kph"] = speed_kph
                data["travel_time"] = (length_m / 1000.0 / speed_kph) * 3600.0

    for _, _, _, data in G.edges(data=True, keys=True):
        raw_seconds = data.get("travel_time", None)
        if raw_seconds is None or not np.isfinite(float(raw_seconds)):
            length_m = float(data.get("length", 0.0) or 0.0)
            speed_kph = float(data.get("speed_kph", SC_FALLBACK_SPEED_KMH) or SC_FALLBACK_SPEED_KMH)
            raw_seconds = (length_m / 1000.0 / speed_kph) * 3600.0
        data[NETWORK_TRAVEL_TIME_WEIGHT] = (float(raw_seconds) + TURN_PENALTY_SECONDS) / 60.0

    return G


def _download_osm_graph_with_cache_fallback(download_func):
    configure_osmnx_cache()
    try:
        return download_func()
    except Exception as exc:
        if not _looks_like_osmnx_cache_error(exc):
            raise
        old_use_cache = bool(getattr(ox.settings, "use_cache", True))
        ox.settings.use_cache = False
        try:
            return download_func()
        finally:
            ox.settings.use_cache = old_use_cache


@st.cache_resource(show_spinner=False)
def get_osm_graph(center_lat, center_lon, dist_m, network_type):
    """Download/load an OSM network graph around a point with a cache-error fallback."""
    def _download():
        return ox.graph_from_point(
            (center_lat, center_lon),
            dist=int(dist_m),
            network_type=network_type,
            retain_all=True,
            truncate_by_edge=True,
        )

    G = _download_osm_graph_with_cache_fallback(_download)
    return preprocess_network_speeds(G, network_type)


@st.cache_resource(show_spinner=False)
def get_osm_graph_for_polygon(polygon_wkt, network_type):
    """Download/load an OSM network graph for a buffered analysis polygon."""
    polygon = wkt.loads(polygon_wkt)

    def _download():
        return ox.graph_from_polygon(
            polygon,
            network_type=network_type,
            retain_all=True,
            truncate_by_edge=True,
        )

    G = _download_osm_graph_with_cache_fallback(_download)
    return preprocess_network_speeds(G, network_type)


def _valid_point_geometries(df):
    if df is None or len(df) == 0:
        return []
    if "latitude" not in df.columns or "longitude" not in df.columns:
        return []
    tmp = df[["latitude", "longitude"]].copy()
    tmp["latitude"] = pd.to_numeric(tmp["latitude"], errors="coerce")
    tmp["longitude"] = pd.to_numeric(tmp["longitude"], errors="coerce")
    tmp = tmp.dropna()
    if tmp.empty:
        return []
    return list(gpd.points_from_xy(tmp["longitude"], tmp["latitude"]))


def _union_geometries(geo_series):
    try:
        return geo_series.union_all()
    except Exception:
        return geo_series.unary_union


def build_network_query_polygon(zip_geom, candidates_df, demand_df, buffer_m):
    """Create a buffered polygon covering the ZIP and all points used for routing."""
    geoms = []
    if zip_geom is not None and not getattr(zip_geom, "is_empty", True):
        geoms.append(zip_geom)
    geoms.extend(_valid_point_geometries(candidates_df))
    geoms.extend(_valid_point_geometries(demand_df))

    if not geoms:
        return None

    geo_series = gpd.GeoSeries(geoms, crs="EPSG:4326")
    try:
        local_crs = geo_series.estimate_utm_crs()
        if local_crs is None:
            raise ValueError("Could not estimate a local projected CRS.")
        projected = geo_series.to_crs(local_crs)
        query_geom = _union_geometries(projected).convex_hull.buffer(float(buffer_m))
        query_geom = gpd.GeoSeries([query_geom], crs=local_crs).to_crs("EPSG:4326").iloc[0]
        if query_geom is not None and not query_geom.is_empty:
            return query_geom
    except Exception:
        pass

    return zip_geom


def local_projected_xy(lons, lats):
    """Project lon/lat arrays to a local CRS and return x/y in meters."""
    lons = np.asarray(lons, dtype=float)
    lats = np.asarray(lats, dtype=float)
    gdf = gpd.GeoDataFrame(geometry=gpd.points_from_xy(lons, lats), crs="EPSG:4326")
    try:
        local_crs = gdf.estimate_utm_crs()
        if local_crs is None:
            raise ValueError("Could not estimate local CRS.")
        projected = gdf.to_crs(local_crs)
        return projected.geometry.x.to_numpy(dtype=float), projected.geometry.y.to_numpy(dtype=float)
    except Exception:
        lat0 = float(np.nanmean(lats)) if np.isfinite(np.nanmean(lats)) else 0.0
        radius_m = 6371000.0
        x = np.radians(lons) * radius_m * np.cos(np.radians(lat0))
        y = np.radians(lats) * radius_m
        return x, y


def rectilinear_travel_time_matrix(candidates_reset, demand_reset, network_type="drive"):
    """Fast Manhattan-style approximation using a local projected CRS."""
    fac_lons = candidates_reset["longitude"].to_numpy(dtype=float)
    fac_lats = candidates_reset["latitude"].to_numpy(dtype=float)
    dem_lons = demand_reset["longitude"].to_numpy(dtype=float)
    dem_lats = demand_reset["latitude"].to_numpy(dtype=float)

    all_lons = np.concatenate([fac_lons, dem_lons])
    all_lats = np.concatenate([fac_lats, dem_lats])
    x, y = local_projected_xy(all_lons, all_lats)

    n_fac = len(fac_lons)
    fac_x = x[:n_fac][:, None]
    fac_y = y[:n_fac][:, None]
    dem_x = x[n_fac:][None, :]
    dem_y = y[n_fac:][None, :]

    rectilinear_m = (np.abs(dem_x - fac_x) + np.abs(dem_y - fac_y)) * CIRCUITY_FACTOR

    if network_type == "drive":
        return (rectilinear_m / 1609.344 / DEFAULT_DRIVING_SPEED) * 60.0
    return (rectilinear_m / 1000.0 / WALKING_SPEED_KMH) * 60.0


def build_coverage_matrix(
    candidates_subset,
    demand_subset,
    max_time,
    network_type="drive",
    use_network=False,
    G=None,
    return_travel_time=False,
    network_access_direction=NETWORK_ACCESS_DIRECTION,
):
    candidates_reset = candidates_subset.reset_index(drop=True).copy()
    demand_reset = demand_subset.reset_index(drop=True).copy()
    n_fac, n_dem = len(candidates_reset), len(demand_reset)

    if n_fac == 0 or n_dem == 0:
        coverage = np.zeros((n_fac, n_dem), dtype=np.uint8)
        travel_time_matrix = np.full((n_fac, n_dem), np.inf)
        if return_travel_time:
            return coverage, candidates_reset, demand_reset, travel_time_matrix
        return coverage, candidates_reset, demand_reset

    if use_network and G is not None:
        coverage = np.zeros((n_fac, n_dem), dtype=np.uint8)
        travel_time_matrix = np.full((n_fac, n_dem), np.inf, dtype=float)
        dem_nodes = snap_points_to_nodes(
            G,
            demand_reset["longitude"].to_numpy(),
            demand_reset["latitude"].to_numpy(),
        )
        fac_nodes = snap_points_to_nodes(
            G,
            candidates_reset["longitude"].to_numpy(),
            candidates_reset["latitude"].to_numpy(),
        )

        routing_graph = G
        if str(network_access_direction).lower() == "demand_to_site":
            # Running Dijkstra from a facility on the reversed graph gives the
            # same travel time as demand centroid -> facility on the original
            # directed graph, while keeping the fast one-source-per-candidate loop.
            try:
                routing_graph = G.reverse(copy=False)
            except TypeError:
                routing_graph = G.reverse()

        for i, origin in enumerate(fac_nodes):
            if origin is None:
                continue
            try:
                lengths = nx.single_source_dijkstra_path_length(
                    routing_graph,
                    origin,
                    cutoff=float(max_time),
                    weight=NETWORK_TRAVEL_TIME_WEIGHT,
                )
            except Exception:
                continue

            for j, node in enumerate(dem_nodes):
                if node is None:
                    continue
                tt = float(lengths.get(node, np.inf))
                if np.isfinite(tt):
                    travel_time_matrix[i, j] = tt
                    coverage[i, j] = 1 if tt <= float(max_time) else 0

        if return_travel_time:
            return coverage, candidates_reset, demand_reset, travel_time_matrix
        return coverage, candidates_reset, demand_reset

    tt = rectilinear_travel_time_matrix(candidates_reset, demand_reset, network_type)
    coverage = (tt <= float(max_time)).astype(np.uint8)
    if return_travel_time:
        return coverage, candidates_reset, demand_reset, tt.astype(float)
    return coverage, candidates_reset, demand_reset


# ===========================
# MAIN APP
# ===========================
def main():
    st.title("🏥 South Carolina MHC Placement Decision Tool")
    st.caption(f"Version: {APP_VERSION}")
    st.markdown("**Optimizing healthcare accessibility for South Carolina's underserved communities.**")

    st.markdown(
        """<div class="instruction-box">
            <b>How to use:</b>
            Select a <b>County</b> to surface its ZIP codes first, or search any
            <b>ZIP Code</b> in South Carolina directly. Pick a <b>Target Variable</b>,
            choose how many <b>MHCs</b> are available, choose how many <b>alternative
            deployment plans</b> to compare, then click <b>Calculate Optimal Sites</b>.
        </div>""",
        unsafe_allow_html=True,
    )

    with st.expander("📖 Methodology Documentation"):
        st.markdown("""
            **Model Type:** Maximum Coverage Location Problem (MCLP)

            **Current objective:** Maximize the selected demand variable covered
            within the selected travel-time threshold. Plans are ranked
            lexicographically: highest covered demand first, then lowest weighted
            average nearest travel time among equal-coverage solutions. Backup
            plans default to a site-distinct setting so alternatives do not simply
            repeat most of the same locations.

            **Existing deployment option:** If this is not the first deployment, add
            one or more previous deployment locations in Advanced settings. The new
            run focuses on areas not already reached by those locations.

            **Number of MHCs to deploy:** How many mobile clinics are available
            simultaneously. For example, 2 MHCs means the model recommends a
            two-site deployment plan.

            **Alternative deployment plans:** Ranked backup configurations. For
            example, 1 MHC and 3 alternatives shows the top 3 single-site choices;
            2 MHCs and 3 alternatives shows 3 alternative two-site plans.

            **Operational feasibility:** After a run, use the recommended-site review
            panel to mark suggested sites as infeasible/unavailable and rerun. If the
            candidate-site data include fields such as parking, restroom, wifi, ADA,
            permission, or feasibility_status, those fields are shown in the site table
            and exported for field review.

            **Overview analytics:** The fleet size scenario analysis shows exact best-plan coverage
            and marginal gains from 1 through the configured sweep maximum, or fewer if fewer candidate sites are available.

            **Network Analysis (optional):** OSM road network accessibility using
            free-flow travel times. Missing speeds are imputed by road class and
            travel is evaluated from demand block centroids to the selected MHC site.

            **Manhattan-style Distance (default):** Projected rectilinear distance
            x 1.2 circuity factor, converted to travel time. This is a fast
            approximation, not a replacement for road-network routing.
        """)

    st.divider()

    try:
        with st.spinner("Loading geospatial data..."):
            (
                zip_gdf,
                candidates_df,
                demand_df,
                county_gdf,
                zip_county_map,
                global_type_colors,
            ) = load_data(JSON_PATH)
    except Exception as e:
        st.error(f"Error loading data: {e}")
        st.stop()

    if county_gdf is None or len(county_gdf) == 0:
        st.error("County data not found in JSON. Please regenerate with county boundaries.")
        st.stop()

    available_targets = {
        label: col
        for label, col in TARGET_VARIABLE_OPTIONS.items()
        if col in demand_df.columns
    }

    with st.sidebar:
        st.header("Control Panel")

        force_run_analysis = bool(st.session_state.pop("force_run_analysis", False))
        run_analysis = st.button(
            "🚀 Calculate Optimal Sites",
            type="primary",
            use_container_width=True,
        ) or force_run_analysis
        if force_run_analysis:
            st.caption("Rerunning after updating reviewed-site exclusions.")
        else:
            st.caption("Primary action")

        with st.expander("🎯 Target Variable", expanded=True):
            st.caption("Choose a category first, then the specific measure.")
            target_label, target_var, target_category = build_target_selector(available_targets)
            st.caption(f"Selected category: {target_category}")

        with st.expander("📍 Geographic Scope", expanded=True):
            county_names = sorted(county_gdf["county_name"].dropna().unique())
            county_name_to_fips = dict(zip(county_gdf["county_name"], county_gdf["COUNTY_FIPS"]))

            county_options = ["All South Carolina"] + county_names
            default_county_index = 1 if len(county_options) > 1 else 0

            selected_county_name = st.selectbox(
                "County (optional)",
                options=county_options,
                index=default_county_index,
                help="Select a county to filter ZIP codes to only those within that county.",
            )
            selected_county_fips = county_name_to_fips.get(selected_county_name)

            zip_choices = get_ordered_zip_choices(
                zip_gdf,
                selected_county_fips,
                county_gdf,
                demand_df=demand_df,
                target_var=target_var,
            )

            if zip_choices.empty:
                st.error("No ZIP choices are available for this county.")
                st.stop()

            zip_labels = zip_choices["zip_label"].tolist()

            if st.session_state.prev_county != selected_county_fips:
                default_zip = zip_choices.iloc[0]["ZIP_CODE"]
            else:
                default_zip = st.session_state.prev_zip
                if default_zip not in zip_choices["ZIP_CODE"].values:
                    default_zip = zip_choices.iloc[0]["ZIP_CODE"]

            default_zip_label = zip_choices.loc[
                zip_choices["ZIP_CODE"] == default_zip, "zip_label"
            ].iloc[0]

            selected_zip_label = st.selectbox(
                "ZIP Code",
                options=zip_labels,
                index=zip_labels.index(default_zip_label),
                help=(
                    "When a county is selected, only ZIP codes within that county are shown, "
                    "ranked by the selected target variable."
                ),
            )
            selected_zip = zip_choices.loc[
                zip_choices["zip_label"] == selected_zip_label, "ZIP_CODE"
            ].iloc[0]

        selected_zip_row = zip_gdf[zip_gdf["ZIP_CODE"] == selected_zip].iloc[0]
        selected_zip_display = build_zip_display(selected_zip_row)
        selected_zip_county_name = str(selected_zip_row.get("county_name", "")).strip()

        zip_geom = zip_gdf[zip_gdf["ZIP_CODE"] == selected_zip].iloc[0].geometry
        candidates_zip_all = get_candidates_in_zip(candidates_df, selected_zip, zip_geom)
        demand_in_zip = get_demand_in_zip(demand_df, selected_zip, zip_geom)

        with st.expander("⚙️ Model Constraints", expanded=True):
            available_site_types = sorted(candidates_zip_all["type"].dropna().unique())

            use_all_site_types = st.checkbox(
                "Use all eligible site types in this ZIP",
                value=True,
                help="Keeps the panel compact. Turn off only if you want to manually filter site types.",
            )

            if use_all_site_types:
                selected_types = available_site_types
                st.caption(f"{len(selected_types)} site types included")
            else:
                selected_types = st.multiselect(
                    "Choose site types",
                    options=available_site_types,
                    default=available_site_types,
                )
                st.caption(f"{len(selected_types)} of {len(available_site_types)} site types selected")

        candidates_after_type = candidates_zip_all[
            candidates_zip_all["type"].isin(selected_types)
        ].copy()

        is_first_deployment = True
        previous_deployment_mode = "None"
        previous_deployment_cand_idxs = []
        previous_deployment_coordinate_rows = pd.DataFrame(columns=["name", "latitude", "longitude"])
        exclude_previous_deployment_site = True
        excluded_cand_ids = set()

        with st.expander("⚙️ Advanced settings", expanded=False):
            st.markdown("##### Existing deployment")
            deployment_answer = st.radio(
                "Is this your first deployment?",
                options=["Yes", "No"],
                index=0,
                horizontal=True,
                help=(
                    "Choose No when one or more MHC locations already exist and this run should focus "
                    "on demand not already covered by those locations."
                ),
            )
            is_first_deployment = deployment_answer == "Yes"

            if not is_first_deployment:
                previous_deployment_mode = st.radio(
                    "Previous deployment location(s)",
                    options=["Select candidate site(s)", "Enter coordinates"],
                    index=0,
                    help="Add one or more existing MHC locations.",
                )

                if previous_deployment_mode == "Select candidate site(s)":
                    prior_candidate_labels = [
                        build_candidate_choice_label(row)
                        for _, row in candidates_zip_all.sort_values(["type", "name", "cand_idx"]).iterrows()
                    ]
                    prior_label_to_cand_idx = {
                        build_candidate_choice_label(row): int(row["cand_idx"])
                        for _, row in candidates_zip_all.iterrows()
                    }
                    previous_deployment_choices = st.multiselect(
                        "Previous deployment site(s)",
                        options=prior_candidate_labels,
                        default=[],
                        help="Select all sites where an MHC has already been deployed.",
                    )
                    previous_deployment_cand_idxs = [
                        int(prior_label_to_cand_idx[label])
                        for label in previous_deployment_choices
                        if label in prior_label_to_cand_idx
                    ]
                else:
                    previous_deployment_coordinate_rows = st.data_editor(
                        pd.DataFrame(columns=["name", "latitude", "longitude"]),
                        key=f"previous_deployment_coordinates_{selected_zip}",
                        hide_index=True,
                        use_container_width=True,
                        num_rows="dynamic",
                        column_config={
                            "name": st.column_config.TextColumn(
                                "Name",
                                help="Optional label, such as Previous site 1.",
                            ),
                            "latitude": st.column_config.NumberColumn(
                                "Latitude",
                                min_value=-90.0,
                                max_value=90.0,
                                format="%.6f",
                            ),
                            "longitude": st.column_config.NumberColumn(
                                "Longitude",
                                min_value=-180.0,
                                max_value=180.0,
                                format="%.6f",
                            ),
                        },
                    )

                exclude_previous_deployment_site = st.checkbox(
                    "Exclude previous deployment site(s) from new recommendations",
                    value=True,
                    disabled=(previous_deployment_mode != "Select candidate site(s)"),
                    help="Usually keep this on so the extension run does not recommend already-used site(s) again.",
                )

            st.markdown("##### Backup plan options")
            prefer_site_distinct_backups = st.checkbox(
                "Prefer site-distinct backup plans (default)",
                value=True,
                key="prefer_site_distinct_backups_v96",
                help=(
                    "Default on: alternative plans reuse fewer of the same sites. "
                    "For a 3-site plan, each backup plan may share at most one site with an earlier plan unless strict diversity becomes infeasible."
                ),
            )
            diversity_mode = SITE_DISTINCT_DIVERSITY_MODE if prefer_site_distinct_backups else COVERAGE_ONLY_DIVERSITY_MODE
            # st.caption(
            #     "Default: rank by highest coverage, then lowest average travel time, while keeping backup plans site-distinct. "
            #     "Turn this off only when you want near-duplicate backups that may preserve more coverage."
            # )

            st.markdown("##### Pre-run site exclusions")
            show_pre_run_exclusion_tools = st.checkbox(
                "Show pre-run site exclusion tools",
                value=False,
                help=(
                    "Turn this on only when you already know sites are infeasible before running. "
                    "After a run, use the recommended-site review table on the results page."
                ),
            )

            if show_pre_run_exclusion_tools:
                exclude_marked_infeasible = st.checkbox(
                    "Exclude sites already marked infeasible/unavailable",
                    value=False,
                    help=(
                        "Uses a feasibility_status column if present in the dataset. "
                        "Recognized values include infeasible, not feasible, unavailable, rejected, and closed."
                    ),
                )

                if exclude_marked_infeasible and "feasibility_status" in candidates_after_type.columns:
                    candidates_after_type = candidates_after_type[
                        ~candidates_after_type["feasibility_status"].apply(candidate_status_is_infeasible)
                    ].copy()

                candidate_choice_labels = [
                    build_candidate_choice_label(row)
                    for _, row in candidates_after_type.sort_values(["type", "name", "cand_idx"]).iterrows()
                ]
                label_to_cand_idx = {
                    build_candidate_choice_label(row): int(row["cand_idx"])
                    for _, row in candidates_after_type.iterrows()
                }

                excluded_labels = st.multiselect(
                    "Manually exclude known infeasible/unavailable sites before running",
                    options=candidate_choice_labels,
                    default=[],
                    help="Use this only when you already know operational constraints before seeing the recommended plans.",
                )
                excluded_cand_ids = {
                    int(label_to_cand_idx[label])
                    for label in excluded_labels
                    if label in label_to_cand_idx
                }

            auto_previous_excluded_ids = set()
            if (
                not is_first_deployment
                and previous_deployment_mode == "Select candidate site(s)"
                and exclude_previous_deployment_site
                and previous_deployment_cand_idxs
            ):
                auto_previous_excluded_ids.update(map(int, previous_deployment_cand_idxs))

            review_excluded_cand_ids = set(map(int, st.session_state.get("review_excluded_cand_ids", set())))
            review_excluded_cand_ids = set(
                candidates_after_type[
                    candidates_after_type["cand_idx"].astype(int).isin(review_excluded_cand_ids)
                ]["cand_idx"].astype(int).tolist()
            )

            effective_excluded_cand_ids = (
                set(excluded_cand_ids)
                | set(auto_previous_excluded_ids)
                | set(review_excluded_cand_ids)
            )
            if auto_previous_excluded_ids:
                st.caption("Previous deployment site(s) are excluded from new recommendations.")
            if review_excluded_cand_ids:
                st.caption(
                    f"{len(review_excluded_cand_ids):,} reviewed site(s) are excluded from this run."
                )

            candidates_in_zip = candidates_after_type[
                ~candidates_after_type["cand_idx"].astype(int).isin(effective_excluded_cand_ids)
            ].copy()
            max_facilities = len(candidates_in_zip)

            st.markdown("##### Map display")
            map_theme = st.radio("Theme", options=["Light", "Dark"], horizontal=True)
            map_tiles = "CartoDB positron" if map_theme == "Light" else "CartoDB dark_matter"
            show_demand_preview = st.toggle(
                "Show block centroids before analysis",
                value=DEFAULT_SHOW_DEMAND_PREVIEW,
            )
            show_other_candidates_after_analysis = st.toggle(
                "Show muted non-selected candidates after analysis",
                value=DEFAULT_SHOW_MUTED_CANDIDATES_AFTER_ANALYSIS,
            )

            st.markdown("##### Fleet size scenario analysis")
            run_resource_overview = st.toggle(
                "Show fleet size scenario analysis after optimization",
                value=True,
                help=(
                    "Runs best-plan coverage analysis from 1 through the selected scenario maximum. "
                    "The default maximum is 5 MHCs, capped by available candidate sites."
                ),
            )

            sweep_max_mhcs = st.number_input(
                "Fleet size scenario maximum MHCs",
                min_value=1,
                max_value=max(1, max_facilities),
                value=min(DEFAULT_SWEEP_MAX_MHCS, max(1, max_facilities)),
                step=1,
                disabled=(max_facilities == 0 or not run_resource_overview),
                help=(
                    "Default is 5. The scenario analysis will show 1 through this number, "
                    "capped by available candidate sites. This is separate from the main "
                    "Number of MHCs to deploy setting."
                ),
            )

        with st.expander("🚐 Deployment and travel settings", expanded=True):
            c_mhcs, c_alts = st.columns(2)
            with c_mhcs:
                num_mhcs = st.number_input(
                    "Number of MHCs to deploy",
                    min_value=1,
                    max_value=max(1, max_facilities),
                    value=min(DEFAULT_NUM_MHCS, max(1, max_facilities)),
                    disabled=(max_facilities == 0),
                    help="How many mobile clinics are available simultaneously.",
                )
            with c_alts:
                num_alternative_plans = st.number_input(
                    "Alternative plans to show",
                    min_value=1,
                    max_value=MAX_ALTERNATIVE_PLANS,
                    value=DEFAULT_NUM_ALTERNATIVE_PLANS,
                    help="How many ranked backup deployment configurations to compare.",
                )

            use_network = st.toggle("Enable Road-Network Routing", value=DEFAULT_USE_NETWORK)

            c1, c2 = st.columns(2)
            with c1:
                travel_mode = st.radio("Travel Mode", options=["drive", "walk"], horizontal=True)
            with c2:
                default_time = 5 if travel_mode == "drive" else 10
                time_threshold = st.select_slider(
                    "Max Travel Time (min)",
                    options=[5, 10, 15, 20, 30, 45],
                    value=default_time,
                )

            st.caption(f"{max_facilities:,} candidate sites remain after filters/exclusions.")

    if st.session_state.prev_county != selected_county_fips:
        st.session_state.view_mode = "county" if selected_county_fips is not None else "zip"
        st.session_state.review_excluded_cand_ids = set()
        st.session_state.review_exclusion_zip = str(selected_zip)
        reset_analysis_state()
        st.session_state.prev_county = selected_county_fips
        st.session_state.prev_zip = selected_zip
    elif st.session_state.prev_zip != selected_zip:
        st.session_state.view_mode = "zip"
        st.session_state.review_excluded_cand_ids = set()
        st.session_state.review_exclusion_zip = str(selected_zip)
        reset_analysis_state()
        st.session_state.prev_zip = selected_zip
    elif st.session_state.get("review_exclusion_zip") is None:
        st.session_state.review_exclusion_zip = str(selected_zip)

    previous_deployment_df = build_previous_deployment_location_df(
        is_first_deployment=is_first_deployment,
        previous_deployment_mode=previous_deployment_mode,
        previous_deployment_cand_idxs=previous_deployment_cand_idxs,
        previous_deployment_coordinates=previous_deployment_coordinate_rows,
        candidates_zip_all=candidates_zip_all,
    )

    current_analysis_params = make_analysis_params(
        selected_zip=selected_zip,
        target_var=target_var,
        selected_types=selected_types,
        excluded_cand_ids=effective_excluded_cand_ids,
        travel_mode=travel_mode,
        time_threshold=time_threshold,
        use_network=use_network,
        num_mhcs=num_mhcs,
        num_alternative_plans=num_alternative_plans,
        diversity_mode=diversity_mode,
        run_resource_overview=run_resource_overview,
        sweep_max_mhcs=sweep_max_mhcs,
        is_first_deployment=is_first_deployment,
        previous_deployment_mode=previous_deployment_mode,
        previous_deployment_cand_idxs=previous_deployment_cand_idxs,
        previous_deployment_coordinates=previous_deployment_coordinate_rows,
        exclude_previous_deployment_site=exclude_previous_deployment_site,
    )

    if (
        st.session_state.analysis_complete
        and st.session_state.analysis_params is not None
        and st.session_state.analysis_params != current_analysis_params
        and not run_analysis
    ):
        reset_analysis_state()
        st.session_state.view_mode = "zip"
        st.info("Controls changed since the last calculation, so previous results were cleared. Click Calculate Optimal Sites to refresh.")

    if run_analysis:
        st.session_state.view_mode = "analysis"

    total_target = (
        float(demand_in_zip[target_var].sum())
        if len(demand_in_zip) and target_var in demand_in_zip.columns
        else 0.0
    )

    result_title = build_result_title(
        selected_zip_display=selected_zip_display,
        target_label=target_label,
        time_threshold=time_threshold,
        travel_mode=travel_mode,
        num_mhcs=num_mhcs,
        num_alternative_plans=num_alternative_plans,
    )
    if not is_first_deployment:
        result_title = f"{result_title} — new coverage after previous deployment"

    col_map, col_insights = st.columns([8, 2], gap="large")

    primary_plan = None

    with col_insights:
        st.subheader("📊 Summary Statistics")
        if st.session_state.view_mode == "county" and selected_county_fips is not None:
            st.metric("County", selected_county_name)
        else:
            st.metric("ZIP Code", selected_zip_display)
        st.metric(f"Total {target_label}", f"{int(round(total_target)):,}")
        if not is_first_deployment:
            st.metric("Deployment mode", "Existing deployment")
        st.metric("Available Candidate Sites", f"{len(candidates_in_zip):,}")
        st.metric("Demand Points", f"{len(demand_in_zip):,}")
        st.metric("MHCs to deploy", f"{int(num_mhcs):,}")
        st.metric("Alternatives requested", f"{int(num_alternative_plans):,}")

    if run_analysis:
        if max_facilities == 0:
            st.error("No candidate facilities available. Change ZIP, site types, or exclusions.")
            st.session_state.view_mode = "zip"
        elif not is_first_deployment and previous_deployment_df.empty:
            st.error("Select or enter at least one previous deployment location before running this mode.")
            st.session_state.view_mode = "zip"
        else:
            with st.spinner(f"Optimizing ranked deployment plans for: {target_label}..."):
                G = None
                method_used = "Manhattan-style Distance"
                previous_deployment_locations = previous_deployment_records_from_df(previous_deployment_df)

                if use_network:
                    zip_center = zip_geom.centroid
                    net_type = "drive" if travel_mode == "drive" else "walk"
                    try:
                        network_extent_candidates = candidates_in_zip
                        if previous_deployment_df is not None and not previous_deployment_df.empty:
                            network_extent_candidates = pd.concat(
                                [candidates_in_zip, previous_deployment_df],
                                ignore_index=True,
                                sort=False,
                            )

                        buffer_m = (
                            NETWORK_QUERY_BUFFER_M_DRIVE
                            if travel_mode == "drive"
                            else NETWORK_QUERY_BUFFER_M_WALK
                        )
                        query_polygon = build_network_query_polygon(
                            zip_geom=zip_geom,
                            candidates_df=network_extent_candidates,
                            demand_df=demand_in_zip,
                            buffer_m=buffer_m,
                        )

                        with st.spinner("Loading road network..."):
                            try:
                                if query_polygon is None or query_polygon.is_empty:
                                    raise ValueError("Network query polygon could not be created.")
                                G = get_osm_graph_for_polygon(query_polygon.wkt, net_type)
                            except Exception:
                                graph_dist_m = estimate_required_graph_dist_m(
                                    zip_center.y,
                                    zip_center.x,
                                    network_extent_candidates,
                                    demand_in_zip,
                                    min_dist=15000,
                                    buffer_m=buffer_m,
                                )
                                G = get_osm_graph(zip_center.y, zip_center.x, int(graph_dist_m), net_type)

                        method_used = "Road Network (OSM, demand-to-site free-flow travel time)"
                    except Exception as e:
                        st.warning(
                            "Road network failed, so the app is using Manhattan-style distance instead. "
                            f"Details: {e}"
                        )

                raw_coverage_matrix, candidates_reset, demand_reset, travel_time_matrix = build_coverage_matrix(
                    candidates_in_zip,
                    demand_in_zip,
                    time_threshold,
                    network_type=travel_mode,
                    use_network=use_network,
                    G=G,
                    return_travel_time=True,
                    network_access_direction=NETWORK_ACCESS_DIRECTION,
                )

                if target_var not in demand_reset.columns:
                    st.error(f"The selected target variable is not available in the demand data: {target_var}")
                    st.stop()

                if use_network and G is not None and not np.isfinite(travel_time_matrix).any():
                    st.warning(
                        "Road-network routing produced no routable candidate-to-demand pairs. "
                        "Check coordinates, OSM road coverage, and the snap-distance threshold."
                    )

                raw_demand_weights = pd.to_numeric(
                    demand_reset[target_var],
                    errors="coerce",
                ).fillna(0).to_numpy(dtype=float)
                negative_weight_count = int(np.sum(raw_demand_weights < 0))
                if negative_weight_count > 0:
                    st.warning(
                        f"{negative_weight_count:,} demand records had negative values for {target_label}; "
                        "they were clipped to zero before optimization."
                    )
                    raw_demand_weights = np.clip(raw_demand_weights, 0.0, None)

                coverage_matrix, demand_weights, previous_covered_mask, previous_covered_value, optimization_total_target = (
                    apply_previous_deployment_adjustment(
                        coverage_matrix=raw_coverage_matrix,
                        demand_weights=raw_demand_weights,
                        demand_reset=demand_reset,
                        previous_deployment_df=previous_deployment_df,
                        time_threshold=time_threshold,
                        travel_mode=travel_mode,
                        use_network=use_network,
                        G=G,
                        network_access_direction=NETWORK_ACCESS_DIRECTION,
                    )
                )
                previous_covered_dem_ids = (
                    set(demand_reset.loc[previous_covered_mask, "dem_idx"].astype(int).tolist())
                    if "dem_idx" in demand_reset.columns and len(demand_reset)
                    else set()
                )

                if not is_first_deployment and optimization_total_target <= 0:
                    st.warning(
                        "The previous deployment already covers all selected target demand within the current travel-time threshold. "
                        "The generated results may show zero additional coverage."
                    )

                plans = solve_top_k_maxcover(
                    coverage_matrix=coverage_matrix,
                    demand_weights=demand_weights,
                    num_facilities=int(num_mhcs),
                    num_alternative_plans=int(num_alternative_plans),
                    candidates_reset=candidates_reset,
                    demand_reset=demand_reset,
                    target_var=target_var,
                    total_target=optimization_total_target,
                    diversity_mode=diversity_mode,
                    # Coverage remains primary; travel time now breaks ties and orders equal-coverage plans.
                    travel_time_matrix=travel_time_matrix,
                )

                if not plans:
                    st.error("No feasible deployment plan could be generated with the current settings.")
                    st.session_state.view_mode = "zip"
                else:
                    sweep_df = None
                    if run_resource_overview:
                        sweep_max = min(int(sweep_max_mhcs), max_facilities)
                        sweep_df = run_resource_sweep(
                            coverage_matrix=coverage_matrix,
                            demand_weights=demand_weights,
                            candidates_reset=candidates_reset,
                            demand_reset=demand_reset,
                            target_var=target_var,
                            target_label=target_label,
                            total_target=optimization_total_target,
                            max_mhcs=sweep_max,
                            travel_time_matrix=travel_time_matrix,
                        )

                    first_plan = plans[0]
                    st.session_state.update({
                        "analysis_complete": True,
                        "view_mode": "analysis",
                        "alternative_plans": plans,
                        "selected_facilities": first_plan["selected_facilities"],
                        "coverage_matrix": coverage_matrix,
                        "travel_time_matrix": travel_time_matrix,
                        "demand_reset": demand_reset,
                        "candidates_reset": candidates_reset,
                        "covered_pop": first_plan["covered_pop"],
                        "covered_mask": first_plan["covered_mask"],
                        "method_used": method_used,
                        "selected_cand_ids": first_plan["selected_cand_ids"],
                        "covered_dem_ids": first_plan["covered_dem_ids"],
                        "target_variable": target_var,
                        "target_label": target_label,
                        "site_metrics_lookup": first_plan["site_metrics_lookup"],
                        "analysis_title": result_title,
                        "analysis_params": current_analysis_params,
                        "scenario_sweep_df": sweep_df,
                        "prior_deployment_active": not is_first_deployment,
                        "previous_deployment_locations": previous_deployment_locations,
                        "previous_covered_dem_ids": previous_covered_dem_ids,
                        "previous_covered_value": previous_covered_value,
                        "remaining_target": optimization_total_target,
                    })

    if st.session_state.analysis_complete and st.session_state.view_mode == "analysis":
        plans = st.session_state.get("alternative_plans", [])
        if plans:
            # Plan 1 remains the primary/best plan for backward-compatible exports,
            # but the UI no longer asks users to switch between plans to compare them.
            primary_plan = plans[0]
            with col_insights:
                st.subheader("🧭 Plan Set")
                best_plan = plans[0]
                cov_pop = float(best_plan["covered_pop"])
                pct = float(best_plan["coverage_pct"])
                covered_count = int(np.sum(best_plan["covered_mask"]))
                total_pts = (
                    len(st.session_state.demand_reset)
                    if st.session_state.demand_reset is not None else 0
                )

                st.metric("Plans generated", f"{len(plans):,}")
                if st.session_state.get("prior_deployment_active", False):
                    st.metric(
                        f"Previously covered {st.session_state.target_label}",
                        f"{int(round(float(st.session_state.get('previous_covered_value', 0.0)))):,}",
                    )
                    st.metric(
                        f"Remaining {st.session_state.target_label} optimized",
                        f"{int(round(float(st.session_state.get('remaining_target', 0.0)))):,}",
                    )
                    st.metric(f"Best new covered {st.session_state.target_label}", f"{int(round(cov_pop)):,}")
                else:
                    st.metric(f"Best covered {st.session_state.target_label}", f"{int(round(cov_pop)):,}")
                st.metric("Best coverage percentage", f"{pct:.1f}%")
                if st.session_state.get("prior_deployment_active", False):
                    remaining_pts = max(
                        total_pts - len(st.session_state.get("previous_covered_dem_ids", set())),
                        0,
                    )
                    st.metric("New covered demand points", f"{covered_count} / {remaining_pts}")
                else:
                    st.metric("Covered demand points", f"{covered_count} / {total_pts}")
                st.progress(min(max(pct / 100.0, 0.0), 1.0))
                st.caption(f"Method: {st.session_state.method_used}")
                if st.session_state.get("prior_deployment_active", False):
                    st.caption("Existing deployment mode: coverage statistics count newly covered demand only; demand already covered by previous deployment locations is excluded.")
                st.caption("All requested deployment alternatives are shown together on the main screen.")

    with col_map:
        if st.session_state.view_mode == "analysis" and st.session_state.analysis_complete and plans:
            result_target_label = st.session_state.target_label
            st.subheader(f"🗺️ Compare Deployment Plans — {st.session_state.get('analysis_title', result_title)}")
            st.caption(
                "Each card starts with a selected-site comparison map using a shared zoomed-in viewport. "
                "Summary metrics stay visible. Open a plan's full details only when you need the detailed coverage map and site table."
            )

            previous_deployment_locations = st.session_state.get("previous_deployment_locations", [])
            previous_covered_dem_ids = st.session_state.get("previous_covered_dem_ids", set())

            comparison_bounds = build_plan_comparison_focus_bounds(
                zip_gdf=zip_gdf,
                selected_zip=selected_zip,
                plans=plans,
                previous_deployment_locations=previous_deployment_locations,
            )

            for plan in plans:
                rank = int(plan["plan_rank"])
                with get_bordered_container():
                    st.markdown(
                        build_plan_card_header_html(plan, result_target_label),
                        unsafe_allow_html=True,
                    )
                    thumb_map = create_plan_thumbnail_map(
                        zip_gdf=zip_gdf,
                        selected_zip=selected_zip,
                        plan=plan,
                        type_colors=global_type_colors,
                        tiles=map_tiles,
                        comparison_bounds=comparison_bounds,
                        previous_deployment_locations=previous_deployment_locations,
                    )
                    render_folium_map(
                        thumb_map,
                        key=f"map_{selected_zip}_plan_{rank}_thumbnail",
                        height=COMPACT_PLAN_MAP_HEIGHT,
                    )

                    metric_cols = st.columns(5)
                    with metric_cols[0]:
                        st.metric(f"Covered {result_target_label}", f"{int(round(float(plan['covered_pop']))):,}")
                    with metric_cols[1]:
                        st.metric("Coverage", f"{float(plan['coverage_pct']):.1f}%")
                    with metric_cols[2]:
                        avg_time = float(plan.get("avg_travel_time_min", np.nan))
                        avg_time_label = f"{avg_time:.1f}" if np.isfinite(avg_time) else "N/A"
                        st.metric("Avg time (min)", avg_time_label)
                    with metric_cols[3]:
                        st.metric("Loss vs best", f"{int(round(float(plan.get('loss_value', 0.0)))):,}")
                    with metric_cols[4]:
                        st.metric("Sites", f"{len(plan['selected_facilities']):,}")

                    with st.expander(f"Full details for Plan {rank}", expanded=False):
                        st.caption(
                            "Open this section for the detailed coverage map, covered/uncovered demand points, muted candidate sites, full legend, and site-level detail table."
                        )
                        render_detail_map = st.checkbox(
                            f"Render detailed coverage map for Plan {rank}",
                            value=False,
                            key=f"render_detail_map_{selected_zip}_{rank}",
                            help="Leave unchecked to keep the page faster and less cluttered.",
                        )
                        if render_detail_map:
                            detail_map = create_map(
                                zip_gdf=zip_gdf,
                                selected_zip=selected_zip,
                                candidates_df=candidates_df,
                                demand_df=demand_df,
                                type_colors=global_type_colors,
                                target_var=st.session_state.target_variable,
                                target_label=result_target_label,
                                selected_cand_ids=plan["selected_cand_ids"],
                                covered_dem_ids=plan["covered_dem_ids"],
                                site_metrics_lookup=plan["site_metrics_lookup"],
                                show_demand_preview=True,
                                selected_types=selected_types,
                                eligible_cand_ids=set(candidates_in_zip["cand_idx"].astype(int).tolist()),
                                tiles=map_tiles,
                                county_gdf=county_gdf,
                                show_other_candidates_after_analysis=show_other_candidates_after_analysis,
                                previous_covered_dem_ids=previous_covered_dem_ids,
                                previous_deployment_locations=previous_deployment_locations,
                            )
                            render_folium_map(
                                detail_map,
                                key=f"map_{selected_zip}_plan_{rank}_detail",
                                height=620,
                            )

                        detail_site_df = build_plan_sites_df(plan, result_target_label)
                        if detail_site_df.empty:
                            st.warning("This plan has no selected sites.")
                        else:
                            feasibility_cols = pick_feasibility_columns(detail_site_df)
                            show_cols = ["Site Rank"] + [
                                c for c in [
                                    "name",
                                    "type",
                                    "address",
                                    f"Gross covered {result_target_label}",
                                    f"Marginal contribution {result_target_label}",
                                ]
                                if c in detail_site_df.columns
                            ] + list(feasibility_cols.keys())
                            rename_map = {"name": "Name", "type": "Type", "address": "Address", **feasibility_cols}
                            st.dataframe(
                                detail_site_df[show_cols].rename(columns=rename_map),
                                use_container_width=True,
                                hide_index=True,
                            )
        elif st.session_state.view_mode == "zip":
            zip_title = selected_zip_display
            if selected_zip_county_name:
                zip_title = f"{selected_zip_display}, {selected_zip_county_name} County"
            st.subheader(f"🗺️ {zip_title}")
            m = create_map(
                zip_gdf=zip_gdf,
                selected_zip=selected_zip,
                candidates_df=candidates_df,
                demand_df=demand_df,
                type_colors=global_type_colors,
                target_var=target_var,
                target_label=target_label,
                show_demand_preview=show_demand_preview,
                selected_types=selected_types,
                eligible_cand_ids=set(candidates_in_zip["cand_idx"].astype(int).tolist()),
                tiles=map_tiles,
                county_gdf=county_gdf,
            )
            render_folium_map(m, key=f"map_{selected_zip}_pre", height=640)

        else:
            st.info(
                f"📍 **{selected_county_name} County**. Select a ZIP code from the sidebar or click a ZIP on the map."
            )
            m = create_county_overview_map(
                county_gdf,
                zip_gdf,
                zip_county_map,
                selected_county_fips,
                tiles=map_tiles,
                demand_df=demand_df,
                target_var=target_var,
                target_label=target_label,
            )
            map_data = st_folium(
                m,
                key=f"map_county_{selected_county_fips}",
                height=640,
                use_container_width=True,
                returned_objects=["last_active_drawing", "last_object_clicked_tooltip"],
            )

            clicked_tooltip = map_data.get("last_object_clicked_tooltip")
            if clicked_tooltip:
                try:
                    match = re.search(r"\b\d{5}\b", str(clicked_tooltip))
                    if match:
                        clicked_zip = match.group(0).zfill(5)
                        if clicked_zip in zip_choices["ZIP_CODE"].values:
                            st.session_state.prev_zip = clicked_zip
                            st.session_state.view_mode = "zip"
                            reset_analysis_state()
                            st.rerun()
                except Exception:
                    pass

    if st.session_state.analysis_complete and st.session_state.view_mode == "analysis" and plans:
        result_target_label = st.session_state.target_label
        plans = st.session_state.get("alternative_plans", [])
        best_plan = plans[0]
        best_site_df = build_plan_sites_df(best_plan, result_target_label)

        st.divider()
        st.subheader(f"🧾 Plan Comparison Table — {st.session_state.get('analysis_title', result_title)}")
        plan_summary_df = build_plan_summary_df(plans, result_target_label)
        st.dataframe(plan_summary_df, use_container_width=True, hide_index=True)

        # # st.caption(
        # #     "Ranking uses covered demand first and weighted average nearest travel time second. Default backup plans also limit site reuse so alternatives are more distinct."
        # # )
        # if st.session_state.get("prior_deployment_active", False):
        #     st.caption(
        #         "Existing deployment mode: covered values in this table count newly covered demand only; demand already covered by previous deployment locations is excluded."
        #     )
        #
        # if int(num_mhcs) == 1:
        #     st.caption(
        #         "Because one MHC is selected, each alternative plan is one candidate site ranked by covered demand, then lower average nearest travel time."
        #     )
        # else:
        #     st.caption(
        #         "Each row is a full deployment configuration. Alternative plans are generated by repeatedly solving MCLP and excluding or diversifying prior solutions."
        #     )



        st.divider()
        with get_bordered_container():
            st.markdown("#### 🛠️ Review recommended sites and rerun")
            st.caption(
                "Use this after inspecting the maps. The table lists only sites that appear in the generated plans. "
                "Mark one or more sites as infeasible/unavailable, then rerun; the model will optimize again using the remaining sites."
            )

            active_review_excluded_ids = set(map(int, st.session_state.get("review_excluded_cand_ids", set())))
            current_zip_cand_ids = set(candidates_zip_all["cand_idx"].astype(int).tolist())
            active_review_excluded_ids_for_zip = active_review_excluded_ids & current_zip_cand_ids

            if active_review_excluded_ids_for_zip:
                excluded_names = format_review_excluded_site_names(
                    active_review_excluded_ids_for_zip,
                    candidates_zip_all,
                )
                if excluded_names:
                    st.info(f"Currently excluded from post-run review: {excluded_names}")
                else:
                    st.info(
                        f"{len(active_review_excluded_ids_for_zip):,} reviewed site(s) are currently excluded from this ZIP."
                    )

            review_df = build_recommended_site_review_df(plans, result_target_label)
            if review_df.empty:
                st.info("No recommended sites are available to review for the current plans.")
            else:
                editor_key = (
                    f"recommended_site_review_editor_{selected_zip}_"
                    f"{len(plans)}_{int(st.session_state.get('review_edit_version', 0))}"
                )
                disabled_cols = [
                    c for c in review_df.columns
                    if c != "Mark infeasible/unavailable"
                ]
                edited_review_df = st.data_editor(
                    review_df,
                    key=editor_key,
                    hide_index=True,
                    use_container_width=True,
                    num_rows="fixed",
                    disabled=disabled_cols,
                    column_config={
                        "Mark infeasible/unavailable": st.column_config.CheckboxColumn(
                            "Mark infeasible/unavailable",
                            help="Check sites that should be excluded from the next optimization run.",
                            default=False,
                        ),
                        "cand_idx": None,
                    },
                )

                selected_review_exclusions = edited_review_df.loc[
                    edited_review_df["Mark infeasible/unavailable"],
                    "cand_idx",
                ].astype(int).tolist()

                review_button_cols = st.columns([2, 1])
                with review_button_cols[0]:
                    exclude_clicked = st.button(
                        "Exclude marked sites and rerun",
                        type="primary",
                        disabled=(len(selected_review_exclusions) == 0),
                        key=f"exclude_review_sites_and_rerun_{selected_zip}",
                        help="Adds the checked recommended sites to the exclusion list and reruns the optimization.",
                    )
                with review_button_cols[1]:
                    clear_clicked = st.button(
                        "Clear review exclusions and rerun",
                        disabled=(len(active_review_excluded_ids_for_zip) == 0),
                        key=f"clear_review_exclusions_and_rerun_{selected_zip}",
                        help="Removes post-run review exclusions for this ZIP and reruns the optimization.",
                    )

                if exclude_clicked:
                    st.session_state.review_excluded_cand_ids = (
                        active_review_excluded_ids | set(map(int, selected_review_exclusions))
                    )
                    st.session_state.review_exclusion_zip = str(selected_zip)
                    st.session_state.review_edit_version = int(st.session_state.get("review_edit_version", 0)) + 1
                    st.session_state.force_run_analysis = True
                    reset_analysis_state()
                    st.session_state.view_mode = "analysis"
                    st.rerun()

                if clear_clicked:
                    st.session_state.review_excluded_cand_ids = (
                        active_review_excluded_ids - active_review_excluded_ids_for_zip
                    )
                    st.session_state.review_exclusion_zip = str(selected_zip)
                    st.session_state.review_edit_version = int(st.session_state.get("review_edit_version", 0)) + 1
                    st.session_state.force_run_analysis = True
                    reset_analysis_state()
                    st.session_state.view_mode = "analysis"
                    st.rerun()

        sweep_df = st.session_state.get("scenario_sweep_df")
        has_sweep = sweep_df is not None and isinstance(sweep_df, pd.DataFrame) and not sweep_df.empty

        with st.expander("Optional analytics", expanded=False):
            st.markdown("#### Site-level metric definitions")
            st.markdown(f"""
            - **Gross covered {result_target_label}:** demand this site could reach by itself.
            - **Marginal contribution {result_target_label}:** how much plan coverage would be lost if this site were removed. This is the key non-overlap contribution metric for multi-site plans.
            """)

            if has_sweep:
                st.divider()
                st.markdown("#### 📈 Fleet Size Scenario Analysis: Coverage Line Chart")
                sweep_limit = (
                    int(sweep_df["MHCs deployed"].max())
                    if "MHCs deployed" in sweep_df.columns
                    else int(sweep_max_mhcs)
                )
                st.caption(
                    f"Shows exact MCLP coverage for fleet sizes 1 through {sweep_limit}. "
                    "The table keeps the exact marginal gain so diminishing returns are visible without extra charts."
                )
                if "Coverage %" in sweep_df.columns:
                    st.line_chart(sweep_df.set_index("MHCs deployed")[["Coverage %"]])

                important_sweep_cols = [
                    c for c in [
                        "MHCs deployed",
                        f"Covered {result_target_label}",
                        "Coverage %",
                        "Exact marginal gain",
                        "Marginal gain pct points",
                        "Selected site names",
                        "Selected site types",
                    ]
                    if c in sweep_df.columns
                ]
                st.dataframe(
                    sweep_df[important_sweep_cols],
                    use_container_width=True,
                    hide_index=True,
                )

        with st.expander("📥 Export Results", expanded=False):
            c1, c2, c3 = st.columns(3)

            with c1:
                if not best_site_df.empty:
                    best_export_cols = [
                        c for c in [
                            "facility_id",
                            "cand_idx",
                            "name",
                            "type",
                            "address",
                            "latitude",
                            "longitude",
                            f"Gross covered {result_target_label}",
                            f"Marginal contribution {result_target_label}",
                        ] + list(FEASIBILITY_COLUMNS.keys())
                        if c in best_site_df.columns
                    ]
                    st.download_button(
                        "Download Best Plan Sites (CSV)",
                        best_site_df[best_export_cols].to_csv(index=False),
                        f"best_plan_{best_plan['plan_rank']}_sites_{selected_zip}.csv",
                        "text/csv",
                        key="best_plan_csv_dl",
                    )

            with c2:
                st.download_button(
                    "Download All Plan Summary (CSV)",
                    plan_summary_df.to_csv(index=False),
                    f"deployment_plan_summary_{selected_zip}.csv",
                    "text/csv",
                    key="plan_summary_csv_dl",
                )

            with c3:
                all_plan_sites_df = build_all_plan_sites_export(plans, result_target_label)
                st.download_button(
                    "Download Field Verification CSV",
                    all_plan_sites_df.to_csv(index=False),
                    f"field_verification_plans_{selected_zip}.csv",
                    "text/csv",
                    key="field_verification_csv_dl",
                )

            if not best_site_df.empty:
                gdf_sel = gpd.GeoDataFrame(
                    best_site_df,
                    geometry=gpd.points_from_xy(best_site_df["longitude"], best_site_df["latitude"]),
                    crs="EPSG:4326",
                )
                st.download_button(
                    "Download Best Plan Sites (GeoJSON)",
                    gdf_sel.to_json(),
                    f"best_plan_{best_plan['plan_rank']}_sites_{selected_zip}.geojson",
                    "application/geo+json",
                    key="best_plan_geojson_dl",
                )


if __name__ == "__main__":
    main()

