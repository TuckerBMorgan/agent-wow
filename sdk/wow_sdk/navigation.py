"""Read-only Detour navigation. Missing data is an error, never a straight-line fallback."""
import ctypes
import math
from pathlib import Path


class Navigation:
    def __init__(self, directory):
        self.directory, self.maps = Path(directory), {}
        self.library = None

    def path(self, map_id, start, end):
        if map_id is None or not start or len(end) != 3 or not all(math.isfinite(v) for v in [*start, *end]):
            raise ValueError("A known map and finite 3D positions are required")
        if self.library is None:
            lib = self.library = ctypes.CDLL("/api/navigation.so")
            lib.nav_open.argtypes, lib.nav_open.restype = [ctypes.c_char_p], ctypes.c_void_p
            lib.nav_add.argtypes, lib.nav_add.restype = [ctypes.c_void_p, ctypes.c_char_p], ctypes.c_int
            lib.nav_path.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float), ctypes.c_int]
            lib.nav_path.restype = ctypes.c_int
        if map_id not in self.maps:
            handle = self.library.nav_open(str(self.directory / f"{map_id:03}.mmap").encode())
            if not handle: raise ValueError("Navigation mesh is unavailable for this map")
            self.maps[map_id] = handle, set()
        handle, loaded = self.maps[map_id]
        tile = lambda value: math.floor(32 - value / (1600 / 3))
        sx, sy, ex, ey = tile(start[0]), tile(start[1]), tile(end[0]), tile(end[1])
        for x in range(max(0, min(sx, ex) - 1), min(63, max(sx, ex) + 1) + 1):
            for y in range(max(0, min(sy, ey) - 1), min(63, max(sy, ey) + 1) + 1):
                if (x, y) in loaded: continue
                path = self.directory / f"{map_id:03}{x:02}{y:02}.mmtile"
                if path.is_file() and self.library.nav_add(handle, str(path).encode()) < 0:
                    raise ValueError(f"Invalid navigation tile: {path.name}")
                loaded.add((x, y))
        a, b, out = (ctypes.c_float * 3)(*start), (ctypes.c_float * 3)(*end), (ctypes.c_float * 12288)()
        count = self.library.nav_path(handle, a, b, out, 4096)
        if count <= 0: raise ValueError("Destination has no complete ground path")
        return [[float(out[i * 3 + j]) for j in range(3)] for i in range(count)]
