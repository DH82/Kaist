#include <cub/cub.cuh>
#include "data_structure.h"
#include "config.h"


OctreeDecoupolyRasterizer::CUDA::GeometryState OctreeDecoupolyRasterizer::CUDA::GeometryState::fromChunk(char*& chunk, size_t P) {
	GeometryState geom;
	obtain(chunk, geom.morton_codes, P, MEM_ALIGNMENT);
	obtain(chunk, geom.colors, P * DECOUPOLY_RANK * CHANNELS, MEM_ALIGNMENT);
	obtain(chunk, geom.bboxes, P, MEM_ALIGNMENT);
	obtain(chunk, geom.tiles_touched, P, MEM_ALIGNMENT);
	cub::DeviceScan::InclusiveSum(nullptr, geom.scan_size, geom.tiles_touched, geom.tiles_touched, P);
	obtain(chunk, geom.scanning_space, geom.scan_size, MEM_ALIGNMENT);
	obtain(chunk, geom.point_offsets, P, MEM_ALIGNMENT);
	return geom;
}


OctreeDecoupolyRasterizer::CUDA::ImageState OctreeDecoupolyRasterizer::CUDA::ImageState::fromChunk(char*& chunk, size_t N) {
	ImageState img;
	obtain(chunk, img.n_contrib, N, MEM_ALIGNMENT);
	obtain(chunk, img.t_contrib, N, MEM_ALIGNMENT);
	obtain(chunk, img.ranges, N, MEM_ALIGNMENT);
	return img;
}


OctreeDecoupolyRasterizer::CUDA::BinningState OctreeDecoupolyRasterizer::CUDA::BinningState::fromChunk(char*& chunk, size_t P) {
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
