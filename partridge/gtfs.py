import os
from threading import RLock

import numpy as np
import pandas as pd

from .config import default_config
from .utilities import detect_encoding, empty_df, setwrap


def _read_file(filename):
    return property(lambda feed: feed.get(filename))


class Feed(object):
    def __init__(self, source, view=None, config=None):
        self._config = default_config() if config is None else config
        self._view = {} if view is None else view
        self._cache = {}
        self._pathmap = {}
        self._delete_after_reading = False
        self._shared_lock = RLock()
        self._locks = {}
        if isinstance(source, self.__class__):
            self._read = source.get
        elif os.path.isdir(source):
            self._read = self._read_csv
            self._bootstrap(source)
        else:
            raise ValueError("Invalid source")

    def get(self, filename):
        lock = self._locks.get(filename, self._shared_lock)
        with lock:
            df = self._cache.get(filename)
            if df is None:
                df = self._read(filename)
                df = self._filter(filename, df)
                df = self._prune(filename, df)
                self._convert_types(filename, df)
                self._cache[filename] = df.reset_index(drop=True)
            return self._cache[filename]

    agency = _read_file("agency.txt")
    calendar = _read_file("calendar.txt")
    calendar_dates = _read_file("calendar_dates.txt")
    fare_attributes = _read_file("fare_attributes.txt")
    fare_rules = _read_file("fare_rules.txt")
    feed_info = _read_file("feed_info.txt")
    frequencies = _read_file("frequencies.txt")
    routes = _read_file("routes.txt")
    shapes = _read_file("shapes.txt")
    stops = _read_file("stops.txt")
    stop_times = _read_file("stop_times.txt")
    transfers = _read_file("transfers.txt")
    trips = _read_file("trips.txt")

    def _bootstrap(self, path):
        # Walk recursively through the directory
        for root, _subdirs, files in os.walk(path):
            for fname in files:
                basename = os.path.basename(fname)
                if basename in self._pathmap:
                    # Verify that the folder does not contain multiple files of the same name.
                    raise ValueError("More than one {} in folder".format(basename))
                # Index paths by their basename.
                self._pathmap[basename] = os.path.join(root, fname)
                # Build a lock for each file to synchronize reads.
                self._locks[basename] = RLock()

    def _read_csv(self, filename):
        path = self._pathmap.get(filename)
        columns = self._config.nodes.get(filename, {}).get("required_columns", [])

        if path is None or os.path.getsize(path) == 0:
            # The file is missing or empty. Return an empty
            # DataFrame containing any required columns.
            return empty_df(columns)

        # If the file isn't in the zip, return an empty DataFrame.
        with open(path, "rb") as f:
            encoding = detect_encoding(f)

        df = pd.read_csv(path, dtype=np.unicode, encoding=encoding, index_col=False)

        # Strip leading/trailing whitespace from column names
        df.rename(columns=lambda x: x.strip(), inplace=True)

        if not df.empty:
            # Strip leading/trailing whitespace from column values
            for col in df.columns:
                df[col] = df[col].str.strip()

        return df

    def _filter(self, filename, df):
        """
        Apply view filters
        """
        view = self._view.get(filename)
        if view is None:
            return df

        keep = df.index
        for col, values in view.items():
            # If applicable, filter this dataframe by the given set of values
            if col in df.columns:
                mask = df[col].isin(setwrap(values))
                keep = keep.intersection(df[mask].index)

        drop = df.index.difference(keep)
        return df.drop(drop)

    def _prune(self, filename, df):
        """
        Depth-first search through the dependency graph
        and prune dependent DataFrames along the way.
        """
        dependencies = []
        for _, depf, data in self._config.out_edges(filename, data=True):
            deps = data.get("dependencies")
            if deps is None:
                msg = f"Edge missing `dependencies` attribute: {filename}->{depf}"
                raise ValueError(msg)
            dependencies.append((depf, deps))

        if not dependencies:
            return df

        keep = df.index
        for depfile, column_pairs in dependencies:
            # Read the filtered, cached file dependency
            depdf = self.get(depfile)
            for deps in column_pairs:
                col = deps[filename]
                depcol = deps[depfile]
                # If applicable, prune this dataframe by the other
                if col in df.columns and depcol in depdf.columns:
                    mask = df[col].isin(depdf[depcol])
                    keep = keep.intersection(df[mask].index)

        drop = df.index.difference(keep)
        return df.drop(drop)

    def _convert_types(self, filename, df):
        """
        Apply type conversions
        """
        if df.empty:
            return

        converters = self._config.nodes.get(filename, {}).get("converters", {})
        for col, converter in converters.items():
            if col in df.columns:
                df[col] = converter(df[col])
