#include <torch/extension.h>
#include "octree_voxel_rasterizer/api.h"
#include "octree_gaussian_rasterizer/api.h"
#include "octree_trivec_rasterizer/api.h"
#include "octree_decoupoly_rasterizer/api.h"


PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
	m.def("rasterize_octree_voxels", &OctreeVoxelRasterizer::rasterizeOctreeVoxelsCUDA);
	m.def("rasterize_octree_voxels_backward", &OctreeVoxelRasterizer::rasterizeOctreeVoxelsBackwardCUDA);
	m.def("rasterize_octree_gaussians", &OctreeGaussianRasterizer::rasterizeOctreeGaussiansCUDA);
	m.def("rasterize_octree_gaussians_backward", &OctreeGaussianRasterizer::rasterizeOctreeGaussiansBackwardCUDA);
	m.def("rasterize_octree_trivecs", &OctreeTrivecRasterizer::rasterizeOctreeTrivecsCUDA);
	m.def("rasterize_octree_trivecs_backward", &OctreeTrivecRasterizer::rasterizeOctreeTrivecsBackwardCUDA);
	m.def("rasterize_octree_decoupolys", &OctreeDecoupolyRasterizer::rasterizeOctreeDecoupolysCUDA);
	m.def("rasterize_octree_decoupolys_backward", &OctreeDecoupolyRasterizer::rasterizeOctreeDecoupolysBackwardCUDA);
}