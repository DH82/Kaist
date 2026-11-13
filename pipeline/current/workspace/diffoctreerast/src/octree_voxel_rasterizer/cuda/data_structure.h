#pragma once
#include <cstdint>
#include <cuda_runtime_api.h>
#include "config.h"


namespace OctreeVoxelRasterizer{
namespace CUDA{
    
/**
 * Obtain a pointer to a chunk of memory and update the chunk pointer
 * 
 * @tparam T Type of the pointer
 * @param chunk Pointer to the chunk of memory, updated to the next available memory
 * @param ptr Pointer to the memory to be obtained
 * @param count Number of elements to be obtained
 * @param alignment Alignment of the memory
 */
template <typename T>
static void obtain(char*& chunk, T*& ptr, std::size_t count, std::size_t alignment) {
    std::size_t offset = (reinterpret_cast<std::uintptr_t>(chunk) + alignment - 1) & ~(alignment - 1);
    ptr = reinterpret_cast<T*>(offset);
    chunk = reinterpret_cast<char*>(ptr + count);
}


/**
 * Calculate the required size of memory for a given type and number of elements
 * 
 * @tparam T Type of the pointer
 * @param P Number of elements
 * @return Size of the memory required
 */
template<typename T> 
size_t required(size_t P) {
    char* size = nullptr;
    T::fromChunk(size, P);
    return (reinterpret_cast<std::uintptr_t>(size) + MEM_ALIGNMENT - 1) & ~(MEM_ALIGNMENT - 1);
}


struct GeometryState {
    size_t scan_size;
    float* depths;
    uint32_t* morton_codes;
    char* scanning_space;
    bool* clamped;
    int4* bboxes;
    float* rgb;
    uint32_t* point_offsets;
    uint32_t* tiles_touched;

    static GeometryState fromChunk(char*& chunk, size_t P);
};


struct ImageState {
    uint2* ranges;
    uint32_t* n_contrib;
    float* accum_alpha;
    float* wm_sum;

    static ImageState fromChunk(char*& chunk, size_t N);
};


struct BinningState {
    size_t sorting_size;
    uint64_t* point_list_keys_unsorted;
    uint64_t* point_list_keys;
    uint32_t* point_list_unsorted;
    uint32_t* point_list;
    char* list_sorting_space;

    static BinningState fromChunk(char*& chunk, size_t P);
};


}} // namespace OctreeVoxelRasterizer::CUDA
