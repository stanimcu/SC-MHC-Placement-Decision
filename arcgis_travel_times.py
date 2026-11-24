"""Read the tiled ArcGIS travel times written by split_statewide_matrix.py.

The folder holds index.json and one .npz tile per grid cell. Only the tiles
holding the requested sites are read, and recently used tiles stay in memory.

    store = PrecomputedTravelTimes("data/arcgis_travel_times/drive")
    demand_ids, coverage, minutes = store.coverage(cand_ids, max_time=10)

Candidate and demand IDs are positions in the candidate_facilities and
demand_points lists of sc_app_data.json, the same as cand_idx and dem_idx in the
app. Travel times run from the demand point to the site.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from pathlib import Path

import numpy as np

FORMAT = 1


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


class PrecomputedTravelTimes:
    def __init__(self, folder, max_cached_tiles=8):
        self.folder = Path(folder)
        with open(self.folder / 'index.json', encoding='utf-8') as stream:
            index = json.load(stream)
        if index.get('kind') != 'arcgis_travel_times' or index.get('format') != FORMAT:
            raise ValueError(f'{self.folder} is not a supported travel-time folder')
        self.index = index
        self.mode = str(index['mode'])
        self.max_time = float(index['max_time'])
        self.candidate_count = int(index['candidate_count'])
        self.demand_count = int(index['demand_count'])
        self.source_sha256 = str(index['source_sha256'])
        self.routing = index.get('routing') or {}
        self.tiles = index['tiles']
        self.site_tile = np.asarray(index['site_tile'], dtype=np.int64)
        if len(self.site_tile) != self.candidate_count:
            raise ValueError('index.json is inconsistent')
        self._cache = OrderedDict()
        self._max_cached = int(max_cached_tiles)

    @property
    def created(self):
        return str(self.index.get('created_utc', ''))[:10]

    def matches(self, candidate_count, demand_count, source_sha256=None):
        """True when these travel times were built for the given app data."""
        if int(candidate_count) != self.candidate_count or int(demand_count) != self.demand_count:
            return False
        return source_sha256 is None or str(source_sha256) == self.source_sha256

    def _tile(self, t):
        if t in self._cache:
            self._cache.move_to_end(t)
            return self._cache[t]
        with np.load(self.folder / self.tiles[t]['file'], allow_pickle=False) as z:
            tile = {key: z[key] for key in ('site_ids', 'indptr', 'demand_delta', 'minutes_x100')}
        self._cache[t] = tile
        while len(self._cache) > self._max_cached:
            self._cache.popitem(last=False)
        return tile

    def site_rows(self, cand_ids):
        """Per site: (demand IDs as int64, minutes x 100 as uint16), sorted by demand ID.

        Sites outside the file (for example custom coordinates with negative IDs)
        and sites without valid coordinates get empty rows.
        """
        empty = (np.empty(0, dtype=np.int64), np.empty(0, dtype=np.uint16))
        rows = []
        for cid in np.asarray(cand_ids, dtype=np.int64).tolist():
            if not 0 <= cid < self.candidate_count or self.site_tile[cid] < 0:
                rows.append(empty)
                continue
            tile = self._tile(int(self.site_tile[cid]))
            pos = int(np.searchsorted(tile['site_ids'], cid))
            if pos >= len(tile['site_ids']) or int(tile['site_ids'][pos]) != cid:
                raise ValueError(f'Site {cid} is missing from its tile')
            start, end = int(tile['indptr'][pos]), int(tile['indptr'][pos + 1])
            rows.append((np.cumsum(tile['demand_delta'][start:end], dtype=np.int64),
                         tile['minutes_x100'][start:end]))
        return rows

    def coverage(self, cand_ids, max_time, demand_ids=None):
        """Coverage and travel-time matrices for the given sites.

        demand_ids: the demand columns to use, in any order. With None, the
        columns are every demand point that at least one of the sites reaches
        within max_time, in ascending order.

        Returns (demand_ids, coverage as uint8 sites x demand, minutes as float
        with inf where a site is not reached within max_time).
        """
        if float(max_time) > self.max_time + 1e-9:
            raise ValueError(f'Travel times were precomputed up to {self.max_time:g} minutes only')
        limit = int(round(float(max_time) * 100))
        kept = []
        for ids, q in self.site_rows(cand_ids):
            ok = q <= limit
            kept.append((ids[ok], q[ok]))
        if demand_ids is None:
            demand_ids = np.unique(np.concatenate([ids for ids, _ in kept])) if kept else np.empty(0, np.int64)
        demand_ids = np.asarray(demand_ids, dtype=np.int64)
        n_dem = len(demand_ids)
        coverage = np.zeros((len(kept), n_dem), dtype=np.uint8)
        minutes = np.full((len(kept), n_dem), np.inf, dtype=float)
        if n_dem:
            order = np.argsort(demand_ids, kind='stable')
            sorted_ids = demand_ids[order]
            for i, (ids, q) in enumerate(kept):
                if not len(ids):
                    continue
                pos = np.searchsorted(sorted_ids, ids)
                hit = pos < n_dem
                hit[hit] = sorted_ids[pos[hit]] == ids[hit]
                cols = order[pos[hit]]
                coverage[i, cols] = 1
                minutes[i, cols] = q[hit] / 100.0
        return demand_ids, coverage, minutes


def build_precomputed_coverage(store, candidates_subset, demand_df, max_time):
    """Same return values as the app's build_coverage_matrix(..., return_travel_time=True).

    Demand columns are the demand points reachable from at least one site, taken
    from the full demand table, so no search radius is needed.
    """
    candidates_reset = candidates_subset.reset_index(drop=True).copy()
    cand_ids = candidates_reset['cand_idx'].astype(int).to_numpy()
    demand_ids, coverage, minutes = store.coverage(cand_ids, max_time)
    demand = demand_df.drop_duplicates(subset='dem_idx')
    demand = demand.set_index(demand['dem_idx'].astype(int).to_numpy())
    demand_reset = demand.loc[demand_ids].reset_index(drop=True).copy()
    return coverage, candidates_reset, demand_reset, minutes


def precomputed_coverage_for_demand(store, sites_df, demand_reset, max_time):
    """Coverage of the given sites over existing demand columns (for previous deployments)."""
    _, coverage, minutes = store.coverage(
        sites_df['cand_idx'].astype(int).to_numpy(), max_time,
        demand_ids=demand_reset['dem_idx'].astype(int).to_numpy(),
    )
    return coverage, minutes
