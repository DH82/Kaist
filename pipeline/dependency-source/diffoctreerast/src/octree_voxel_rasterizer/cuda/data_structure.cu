#include <cub/cub.cuh>
#include "data_structure.h"
#include "config.h"


OctreeVoxelRasterizer::CUDA::GeometryState OctreeVoxelRasterizer::CUDA::GeometryState::fromChunk(char*& chunk, size_t P) {
	GeometryState geom;
	obtain(chunk, geom.depths, P, MEM_ALIGNMENT);
	obtain(chunk, geom.morton_codes, P, MEM_ALIGNMENT);
	obtain(chunk, geom.clamped, P * 3, MEM_ALIGNMENT);
	obtain(chunk, geom.bboxes, P, MEM_ALIGNMENT);
	obtain(chunk, geom.rgb, P * 3, MEM_ALIGNMENT);
	obtain(chunk, geom.tiles_touched, P, MEM_ALIGNMENT);
	cub::DeviceScan::InclusiveSum(nullptr, geom.scan_size, geom.tiles_touched, geom.tiles_touched, P);
	obtain(chunk, geom.scanning_space, geom.scan_size, MEM_ALIGNMENT);
	obtain(chunk, geom.point_offsets, P, MEM_ALIGNMENT);
	return geom;
}


OctreeVoxelRasterizer::CUDA::ImageState OctreeVoxelRasterizer::CUDA::ImageState::fromChunk(char*& chunk, size_t N) {
	ImageState img;
	obtain(chunk, img.accum_alpha, N, MEM_ALIGNMENT);
	obtain(chunk, img.wm_sum, N, MEM_ALIGNMENT);
	obtain(chunk, img.n_contrib, N, MEM_ALIGNMENT);
	obtain(chunk, img.ranges, N, MEM_ALIGNMENT);
	return img;
}


OctreeVoxelRasterizer::CUDA::BinningState OctreeVoxelRasterizer::CUDA::BinningState::fromChunk(char*& chunk, size_t P) {
	BinningState binning;
	obtain(chunk, binning.point_list, P, MEM_ALIGNMENT);
	obtain(chunk, binning.point_list_unsorted, P, MEM_ALIGNMENT);
	obtain(chunk, binning.point_list_keys, P, MEM_ALIGNMENT);
	obtain(chunk, binning.point_list_keys_unsorted, P, MEM_ALIGNMENT);
	cub::DeviceRadixSort::SortPairs(
		nullptr, binning.sorting_size,
		binning.point_list_keys_unsorted, binning.point_list_keys,
		binning.point_list_unsorted, binning.point_list, P);
	obtain(chunk, binning.list_sorting_space, binning.sorting_size, MEM_ALIGNMENT);
	return binning;
}
