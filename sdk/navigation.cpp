// Small client-side adapter for the public AzerothCore/Detour navigation meshes.
#include "DetourNavMesh.h"
#include "DetourNavMeshQuery.h"
#include "DetourAlloc.h"
#include <cmath>
#include <cstdio>
#include <cstring>

struct Navigation { dtNavMesh* mesh; dtNavMeshQuery* query; };

extern "C" {
void* nav_open(const char* path) {
    FILE* file = fopen(path, "rb");
    if (!file) return nullptr;
    dtNavMeshParams params;
    bool read = fread(&params, sizeof(params), 1, file) == 1;
    fclose(file);
    if (!read) return nullptr;
    auto* nav = new Navigation{dtAllocNavMesh(), dtAllocNavMeshQuery()};
    if (dtStatusFailed(nav->mesh->init(&params)) || dtStatusFailed(nav->query->init(nav->mesh, 32768))) {
        dtFreeNavMeshQuery(nav->query); dtFreeNavMesh(nav->mesh); delete nav; return nullptr;
    }
    return nav;
}

int nav_add(void* pointer, const char* path) {
    auto* nav = static_cast<Navigation*>(pointer);
    FILE* file = fopen(path, "rb");
    if (!file) return -1;
    unsigned header[14];
    // Build-12340 client data uses mmap v20: a 56-byte header, then Detour data.
    if (fread(header, sizeof(header), 1, file) != 1 || header[0] != 0x4d4d4150 ||
        header[1] != DT_NAVMESH_VERSION || header[2] != 20 || header[3] < sizeof(dtMeshHeader) || header[3] > (64u << 20)) {
        fclose(file); return -1;
    }
    auto* data = static_cast<unsigned char*>(dtAlloc(header[3], DT_ALLOC_PERM));
    bool read = data && fread(data, header[3], 1, file) == 1;
    fclose(file);
    if (!read || dtStatusFailed(nav->mesh->addTile(data, header[3], DT_TILE_FREE_DATA, 0, nullptr))) {
        dtFree(data); return -1;
    }
    return 1;
}

int nav_path(void* pointer, const float* source, const float* destination, float* output, int capacity) {
    auto* nav = static_cast<Navigation*>(pointer);
    float a[3] = {source[1], source[2], source[0]}, b[3] = {destination[1], destination[2], destination[0]};
    float extents[3] = {3, 4, 3}, start[3], end[3];
    dtPolyRef first = 0, last = 0;
    dtQueryFilter filter; filter.setIncludeFlags(1); // Ground polygons only.
    nav->query->findNearestPoly(a, extents, &filter, &first, start);
    nav->query->findNearestPoly(b, extents, &filter, &last, end);
    if (!first || !last || fabsf(start[1] - a[1]) > 1.5f) return -1;
    dtPolyRef polygons[4096]; int count = 0;
    dtStatus status = nav->query->findPath(first, last, start, end, &filter, polygons, &count, 4096);
    if (dtStatusFailed(status) || !count || polygons[count - 1] != last) return -2;
    float corners[12288]; int cornerCount = 0;
    nav->query->findStraightPath(start, end, polygons, count, corners, nullptr, nullptr, &cornerCount, 4096);
    if (cornerCount < 1) return -3;
    float current[3]; memcpy(current, start, sizeof(current));
    dtPolyRef polygon = first; int written = 0;
    for (int i = 1; i < cornerCount; ++i) {
        const float* target = corners + i * 3;
        float origin[3]; memcpy(origin, current, sizeof(origin));
        int steps = static_cast<int>(ceilf(hypotf(target[0] - origin[0], target[2] - origin[2]) / 1.5f));
        if (steps < 1) steps = 1;
        for (int step = 1; step <= steps; ++step) {
            float goal[3]; for (int j = 0; j < 3; ++j) goal[j] = origin[j] + (target[j] - origin[j]) * step / steps;
            float next[3]; dtPolyRef visited[32]; int visitedCount = 0;
            if (dtStatusFailed(nav->query->moveAlongSurface(polygon, current, goal, &filter, next, visited, &visitedCount, 32)) || !visitedCount) return -4;
            polygon = visited[visitedCount - 1];
            if (dtStatusFailed(nav->query->getPolyHeight(polygon, next, &next[1]))) return -4;
            memcpy(current, next, sizeof(current));
            if (written >= capacity) return -5;
            output[written * 3] = next[2]; output[written * 3 + 1] = next[0]; output[written * 3 + 2] = next[1]; ++written;
        }
    }
    if (!written) {
        output[0] = end[2]; output[1] = end[0]; output[2] = end[1]; written = 1;
    }
    return written;
}
}
