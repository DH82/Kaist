#include <cuda.h>
#include "cuda_runtime.h"
#include "device_launch_parameters.h"
#include <cub/cub.cuh>
#include <cub/device/device_radix_sort.cuh>
#include <glm/glm.hpp>

#include <cooperative_groups.h>
#include <cooperative_groups/reduce.h>
namespace cg = cooperative_groups;

#include "config.h"
#include "auxiliary.h"
#include "data_structure.h"
#include "api.h"


/**
 * Helper function to find the highest bit set in an integer.
 * 
 * @param n Integer.
 * @return Highest bit set.
*/
static uint32_t getHigherMsb(uint32_t n) {
	uint32_t msb = sizeof(n) * 4;
	uint32_t step = msb;
	while (step > 1)
	{
		step /= 2;
		if (n >> msb)
			msb += step;
		else
			msb -= step;
	}
	if (n >> msb)
		msb++;
	return msb;
}


/**
 * Forward pass for converting the input spherical harmonics coefficients of each voxel to a simple RGB color.
 * 
 * @param idx Index of the point in the input array.
 * @param deg Degree of the spherical harmonics coefficients.
 * @param max_coeffs Maximum number of coefficients.
 * @param pos Position of the point.
 * @param campos Camera position.
 * @param shs Array of spherical harmonics coefficients.
 * @param clamped Array of booleans to store if the color was clamped.
 * @return The color of the point.
 */
static __device__ glm::vec3 computeColorFromSH(int idx, int deg, int max_coeffs, const glm::vec3 pos, glm::vec3 campos, const float* shs, bool* clamped) {
	// The implementation is loosely based on code for 
	// "Differentiable Point-Based Radiance Fields for 
	// Efficient View Synthesis" by Zhang et al. (2022)
	glm::vec3 dir = pos - campos;
	dir = dir / glm::length(dir);

	glm::vec3* sh = ((glm::vec3*)shs) + idx * max_coeffs;
	glm::vec3 result = SH_C0 * sh[0];

	if (deg > 0)
	{
		float x = dir.x;
		float y = dir.y;
		float z = dir.z;
		result = result - SH_C1 * y * sh[1] + SH_C1 * z * sh[2] - SH_C1 * x * sh[3];

		if (deg > 1)
		{
			float xx = x * x, yy = y * y, zz = z * z;
			float xy = x * y, yz = y * z, xz = x * z;
			result = result +
				SH_C2[0] * xy * sh[4] +
				SH_C2[1] * yz * sh[5] +
				SH_C2[2] * (2.0f * zz - xx - yy) * sh[6] +
				SH_C2[3] * xz * sh[7] +
				SH_C2[4] * (xx - yy) * sh[8];

			if (deg > 2)
			{
				result = result +
					SH_C3[0] * y * (3.0f * xx - yy) * sh[9] +
					SH_C3[1] * xy * z * sh[10] +
					SH_C3[2] * y * (4.0f * zz - xx - yy) * sh[11] +
					SH_C3[3] * z * (2.0f * zz - 3.0f * xx - 3.0f * yy) * sh[12] +
					SH_C3[4] * x * (4.0f * zz - xx - yy) * sh[13] +
					SH_C3[5] * z * (xx - yy) * sh[14] +
					SH_C3[6] * x * (xx - 3.0f * yy) * sh[15];
			}
		}
	}
	result += 0.5f;

	// RGB colors are clamped to positive values. If values are
	// clamped, we need to keep track of this for the backward pass.
	clamped[3 * idx + 0] = (result.x < 0 | result.x > 1);
	clamped[3 * idx + 1] = (result.y < 0 | result.y > 1);
	clamped[3 * idx + 2] = (result.z < 0 | result.z > 1);
	return glm::min(glm::max(result, 0.0f), 1.0f);
}


/**
 * Compute the morton code for a 3D point based on the camera position and the depth of the voxel.
 * 
 * @param pos Position of the point.
 * @param campos Camera position.
 * @param depth Depth of the voxel.
 */
static __device__ uint32_t computeMortonCode(float3 pos, float3 campos, uint8_t depth) {
	uint32_t mul = 1 << MAX_TREE_DEPTH;
	uint32_t xcode = (uint32_t)(pos.x * mul);
	uint32_t ycode = (uint32_t)(pos.y * mul);
	uint32_t zcode = (uint32_t)(pos.z * mul);
	uint32_t cxcode = (uint32_t)(campos.x * mul);
	uint32_t cycode = (uint32_t)(campos.y * mul);
	uint32_t czcode = (uint32_t)(campos.z * mul);
	uint32_t xflip = 0, yflip = 0, zflip = 0;
	bool done = false;
	for (int i = 1; i <= MAX_TREE_DEPTH && !done; i++)
	{
		xflip |= ((xcode >> (MAX_TREE_DEPTH - i + 1) << 1) < (cxcode >> (MAX_TREE_DEPTH - i))) ? (1 << (MAX_TREE_DEPTH - i)) : 0;
		yflip |= ((ycode >> (MAX_TREE_DEPTH - i + 1) << 1) < (cycode >> (MAX_TREE_DEPTH - i))) ? (1 << (MAX_TREE_DEPTH - i)) : 0;
		zflip |= ((zcode >> (MAX_TREE_DEPTH - i + 1) << 1) < (czcode >> (MAX_TREE_DEPTH - i))) ? (1 << (MAX_TREE_DEPTH - i)) : 0;
		done = i == depth;
	}
	xcode ^= xflip;
	ycode ^= yflip;
	zcode ^= zflip;
	return expandBits(xcode) | (expandBits(ycode) << 1) | (expandBits(zcode) << 2);
}


/**
 * Preprocess input 3D points
 */
static __global__ void preprocess(
	const int num_nodes,
	const int active_sh_degree,
	const int num_sh_coefs,
	const float* positions,
	const uint8_t* tree_depths,
	const float scale_modifier,
	const float* shs,
	bool* clamped,
	const float* colors_precomp,
	const float* viewmatrix,
	const float* projmatrix,
	const glm::vec3* cam_pos,
	const int width,
	const int height,
	const float tan_fovx,
	const float tan_fovy,
	const float focal_x,
	const float focal_y,
	const float* aabb,
	int4* bboxes,
	float* depths,
	float* rgb,
	const dim3 grid,
	uint32_t* tiles_touched,
	uint32_t* morton_codes
) {
	auto idx = cg::this_grid().thread_rank();
	if (idx >= num_nodes)
		return;

	// Initialize bboxes and touched tiles to 0. If this isn't changed,
	// this voxel will not be processed further.
	bboxes[idx] = { 0, 0, 0, 0 };
	tiles_touched[idx] = 0;

	// Perform near culling, quit if outside.
	float3 p_orig = {
		positions[3 * idx] * aabb[3] + aabb[0],
		positions[3 * idx + 1] * aabb[4] + aabb[1],
		positions[3 * idx + 2] * aabb[5] + aabb[2]
	};
	float3 p_view;
	if (!in_frustum(idx, p_orig, viewmatrix, projmatrix, p_view))
		return;

	// Project 8 vertices of the voxel to screen space to find the
	// bounding box of the projected points.
	float nsize = powf(2.0f, -(float)tree_depths[idx]) * scale_modifier;
	float3 scale = { aabb[3] * nsize, aabb[4] * nsize, aabb[5] * nsize };
	int4 bbox = get_bbox(p_orig, scale, projmatrix, width, height);
	uint2 rect_min, rect_max;
	getRect(bbox, rect_min, rect_max, grid);
	if ((rect_max.x - rect_min.x) * (rect_max.y - rect_min.y) == 0)
		return;

	// If colors have been precomputed, use them, otherwise convert
	// spherical harmonics coefficients to RGB color.
	if (colors_precomp == nullptr)
	{
		glm::vec3 result = computeColorFromSH(idx, active_sh_degree, num_sh_coefs, *(glm::vec3*)&p_orig, *cam_pos, shs, clamped);
		rgb[idx * 3 + 0] = result.x;
		rgb[idx * 3 + 1] = result.y;
		rgb[idx * 3 + 2] = result.z;
	}

	// Calculate view-dependent morton code for sorting.
	float3 pos = { positions[3 * idx], positions[3 * idx + 1], positions[3 * idx + 2] };
	float3 ncampos = {
		max(0.0f, min(1.0f, (cam_pos->x - aabb[0]) / aabb[3])),
		max(0.0f, min(1.0f, (cam_pos->y - aabb[1]) / aabb[4])),
		max(0.0f, min(1.0f, (cam_pos->z - aabb[2]) / aabb[5]))
	};
	uint32_t morton_code = computeMortonCode(pos, ncampos, tree_depths[idx]);

	// Store some useful helper data for the next steps.
	depths[idx] = p_view.z;
	bboxes[idx] = bbox;
	tiles_touched[idx] = (rect_max.y - rect_min.y) * (rect_max.x - rect_min.x);
	morton_codes[idx] = morton_code;
}


/**
 * Generates one key/value pair for all voxel / tile overlaps. 
 * Run once per voxel (1:N mapping).
 * 
 * @param P Number of points.
 * @param points_xy 2D points.
 * @param depths Depths of points.
 * @param offsets Offsets for writing keys/values.
 * @param keys_unsorted Unsorted keys.
 * @param values_unsorted Unsorted values.
 * @param radii Radii of points.
 * @param grid Grid size.
 */
static __global__ void duplicateWithKeys(
	int P,
	const uint32_t* morton_codes,
	const uint32_t* offsets,
	uint64_t* keys_unsorted,
	uint32_t* values_unsorted,
	int4* bboxes,
	dim3 grid
) {
	auto idx = cg::this_grid().thread_rank();
	if (idx >= P)
		return;

	// Generate no key/value pair for invisible voxels
	if (bboxes[idx].w > 0)
	{
		// Find this voxel's offset in buffer for writing keys/values.
		uint32_t off = (idx == 0) ? 0 : offsets[idx - 1];
		uint2 rect_min, rect_max;
		getRect(bboxes[idx], rect_min, rect_max, grid);

		// For each tile that the bounding rect overlaps, emit a 
		// key/value pair. The key is |  tile ID  |      depth      |,
		// and the value is the ID of the voxel. Sorting the values 
		// with this key yields voxel IDs in a list, such that they
		// are first sorted by tile and then by depth. 
		for (int y = rect_min.y; y < rect_max.y; y++)
		{
			for (int x = rect_min.x; x < rect_max.x; x++)
			{
				uint64_t key = y * grid.x + x;
				key <<= 32;
				key |= morton_codes[idx];
				keys_unsorted[off] = key;
				values_unsorted[off] = idx;
				off++;
			}
		}
	}
}


/**
 * Check keys to see if it is at the start/end of one tile's range in the full sorted list. If yes, write start/end of this tile.
 * 
 * @param L Number of points.
 * @param point_list_keys List of keys.
 * @param ranges Ranges of tiles.
 */
static __global__ void identifyTileRanges(int L, uint64_t* point_list_keys, uint2* ranges)
{
	auto idx = cg::this_grid().thread_rank();
	if (idx >= L)
		return;

	// Read tile ID from key. Update start/end of tile range if at limit.
	uint64_t key = point_list_keys[idx];
	uint32_t currtile = key >> 32;
	if (idx == 0)
		ranges[currtile].x = 0;
	else
	{
		uint32_t prevtile = point_list_keys[idx - 1] >> 32;
		if (currtile != prevtile)
		{
			ranges[prevtile].y = idx;
			ranges[currtile].x = idx;
		}
	}
	if (idx == L - 1)
		ranges[currtile].y = L;
}


/**
 * Main rasterization method. Collaboratively works on one tile per
 * block, each thread treats one pixel. Alternates between fetching 
 * and rasterizing data.
 * 
 * @tparam CHANNELS Number of channels.
 * @param ranges Ranges of voxel instances for each tile.
 * @param point_list List of voxel instances.
 * @param W Width of the image.
 * @param H Height of the image.
 * @param bg_color Background color.
 * @param cam_pos Camera position.
 * @param tan_fovx Tangent of the horizontal field of view.
 * @param tan_fovy Tangent of the vertical field of view.
 * @param viewmatrix View matrix.
 * @param aabb Axis-aligned bounding box.
 * @param positions Centers of octree nodes.
 * @param features Features of octree nodes.
 * @param depths Depths of octree nodes (in view space).
 * @param tree_depths Depths of octree nodes.
 * @param scale_modifier Scale modifier.
 * @param densities densities of octree nodes.
 * @param final_T Final T.
 * @param final_wm_sum Weighted midpoint sum.
 * @param n_contrib Number of contributors.
 * @param out_color Output color.
 * @param out_depth Output depth.
 * @param out_alpha Output alpha.
 * @param out_distloss Output distance loss.
 */
template <uint32_t CHANNELS>
static __global__ void __launch_bounds__(BLOCK_X * BLOCK_Y)
render(
	const uint2* __restrict__ ranges,
	const uint32_t* __restrict__ point_list,
	const int W,
	const int H,
	const float* __restrict__ bg_color,
	const float3* cam_pos,
	const float tan_fovx,
	const float tan_fovy,
	const float* __restrict__ viewmatrix,
	const float* __restrict__ aabb,
	const float* __restrict__ positions,
	const float* __restrict__ features,
	const float* __restrict__ depths,
	const uint8_t* __restrict__ tree_depths,
	const float scale_modifier,
	const float* __restrict__ densities,
	float* __restrict__ final_T,
	float* __restrict__ final_wm_sum,
	uint32_t* __restrict__ n_contrib,
	float* __restrict__ out_color,
	float* __restrict__ out_depth,
	float* __restrict__ out_alpha,
	float* __restrict__ out_distloss
) {
	// Identify current tile and associated min/max pixel range.
	auto block = cg::this_thread_block();
	uint32_t horizontal_blocks = (W + BLOCK_X - 1) / BLOCK_X;
	uint2 pix_min = { block.group_index().x * BLOCK_X, block.group_index().y * BLOCK_Y };
	uint2 pix_max = { min(pix_min.x + BLOCK_X, W), min(pix_min.y + BLOCK_Y , H) };
	uint2 pix = { pix_min.x + block.thread_index().x, pix_min.y + block.thread_index().y };
	uint32_t pix_id = W * pix.y + pix.x;

	// Get ray direction and origin for this pixel.
	float3 ray_dir = getRayDir(pix, W, H, tan_fovx, tan_fovy, viewmatrix);

	// Check if this thread is associated with a valid pixel or outside.
	bool inside = pix.x < W&& pix.y < H;
	// Done threads can help with fetching, but don't rasterize
	bool done = !inside;

	// Load start/end range of IDs to process in bit sorted list.
	uint2 range = ranges[block.group_index().y * horizontal_blocks + block.group_index().x];
	const int rounds = ((range.y - range.x + BLOCK_SIZE - 1) / BLOCK_SIZE);
	int toDo = range.y - range.x;

	// Allocate storage for batches of collectively fetched data.
	__shared__ int collected_id[BLOCK_SIZE];
	__shared__ float3 collected_xyz[BLOCK_SIZE];
	__shared__ float3 collected_scales[BLOCK_SIZE];
	__shared__ float collected_densities[BLOCK_SIZE];

	// Initialize helper variables
	float T = 1.0f;
	uint32_t contributor = 0;
	uint32_t last_contributor = 0;
	float C[CHANNELS] = { 0 };
	float D = 0;
	float wm_prefix = 0;
	float distloss = 0;

	// Iterate over batches until all done or range is complete
	for (int i = 0; i < rounds; i++, toDo -= BLOCK_SIZE)
	{
		// End if entire block votes that it is done rasterizing
		int num_done = __syncthreads_count(done);
		if (num_done == BLOCK_SIZE)
			break;

		// Collectively fetch per-voxel data from global to shared
		int progress = i * BLOCK_SIZE + block.thread_rank();
		if (range.x + progress < range.y)
		{
			int coll_id = point_list[range.x + progress];
			collected_id[block.thread_rank()] = coll_id;
			collected_xyz[block.thread_rank()] = {
				positions[3 * coll_id] * aabb[3] + aabb[0],
				positions[3 * coll_id + 1] * aabb[4] + aabb[1],
				positions[3 * coll_id + 2] * aabb[5] + aabb[2]
			};
			float nsize = powf(2.0f, -(float)tree_depths[coll_id]) * scale_modifier;
			collected_scales[block.thread_rank()] = {aabb[3] * nsize, aabb[4] * nsize, aabb[5] * nsize};
			collected_densities[block.thread_rank()] = densities[coll_id];
		}
		block.sync();

		// Iterate over current batch
		for (int j = 0; !done && j < min(BLOCK_SIZE, toDo); j++)
		{
			// Keep track of current position in range
			contributor++;

			// Get ray-voxel intersection
			float3 p = collected_xyz[j];
			float3 scale = collected_scales[j];
			float3 voxel_min = { p.x - 0.5f * scale.x, p.y - 0.5f * scale.y, p.z - 0.5f * scale.z };
			float3 voxel_max = { p.x + 0.5f * scale.x, p.y + 0.5f * scale.y, p.z + 0.5f * scale.z };
			float2 itsc = get_ray_voxel_intersection(*cam_pos, ray_dir, voxel_min, voxel_max);
			float itsc_dist = (itsc.y >= itsc.x) ? itsc.y - itsc.x : -1.0f;
			if (itsc_dist <= 0.0f)
				continue;

			// Volume rendering
			float alpha = min(1 - exp(-collected_densities[j] * itsc_dist), 0.999f);
			const float weight = alpha * T;

			// Accumulate color and depth
			for (int ch = 0; ch < CHANNELS; ch++)
				C[ch] += features[collected_id[j] * CHANNELS + ch] * weight;
			D += depths[collected_id[j]] * weight;

			// Distortion loss
			// loss_bi := 2 * (wm * w_prefix - w * wm_prefix); loss_uni := 1.0f / 3.0f * (itsc_dist * w^2);
			if (out_distloss != nullptr)
			{
				float midpoint = 0.5f * (itsc.x + itsc.y);
				float wm = weight * midpoint;
				distloss += 2.0f * (wm * (1.0f - T) - weight * wm_prefix) + (1.0f / 3.0f) * itsc_dist * weight * weight;
				wm_prefix += wm;
			}

			T *= 1 - alpha;

			// Keep track of last range entry to update this
			// pixel.
			last_contributor = contributor;

			// If we have accumulated enough, we can stop
			if (T < 0.001f)
				done = true;
		}
	}

	// All threads that treat valid pixel write out their final
	// rendering data to the frame and auxiliary buffers.
	if (inside)
	{
		final_T[pix_id] = T;
		n_contrib[pix_id] = last_contributor;
		for (int ch = 0; ch < CHANNELS; ch++)
			out_color[ch * H * W + pix_id] = C[ch] + T * bg_color[ch];
		out_depth[pix_id] = D;
		out_alpha[pix_id] = 1.0f - T;
		if (out_distloss != nullptr) {
			out_distloss[pix_id] = distloss;
			final_wm_sum[pix_id] = wm_prefix;
		}
	}
}

int OctreeVoxelRasterizer::CUDA::forward(
	std::function<char*(size_t)> geometryBuffer,
	std::function<char*(size_t)> binningBuffer,
	std::function<char*(size_t)> imageBuffer,
	const int num_nodes,
	const int active_sh_degree,
	const int num_sh_coefs,
	const float* background,
    const int width,
    const int height,
    const float* aabb,
    const float* positions,
    const float* shs,
    const float* colors_precomp,
    const float* densities,
    const uint8_t* depths,
	const float scale_modifier,
	const float* viewmatrix,
	const float* projmatrix,
    const float* cam_pos,
	const float tan_fovx,
    const float tan_fovy,
    float* out_color,
    float* out_depth,
    float* out_alpha,
	float* out_distloss
) {
	DEBUG_PRINT("Starting forward pass\n");
	DEBUG_PRINT("    - Number of nodes: %d\n", num_nodes);
	DEBUG_PRINT("    - Active SH degree: %d\n", active_sh_degree);
	DEBUG_PRINT("    - Number of SH coefficients: %d\n", num_sh_coefs);
	DEBUG_PRINT("    - Image size: %d x %d\n", width, height);

	// Parrallel config (2D grid of 2D blocks)
	dim3 grid((width + BLOCK_X - 1) / BLOCK_X, (height + BLOCK_Y - 1) / BLOCK_Y, 1);
	dim3 block(BLOCK_X, BLOCK_Y, 1);

	// Allocate buffers for auxiliary info for points and pixels
	DEBUG_PRINT("Allocating buffers\n");
	size_t buffer_size;
	char* buffer_ptr;
	buffer_size = required<GeometryState>(num_nodes);
	DEBUG_PRINT("    - Geometry buffer size: %zu\n", buffer_size);
	buffer_ptr = geometryBuffer(buffer_size);
	GeometryState geomState = GeometryState::fromChunk(buffer_ptr, num_nodes);
	buffer_size = required<ImageState>(width * height);
	DEBUG_PRINT("    - Image buffer size: %zu\n", buffer_size);
	buffer_ptr = imageBuffer(buffer_size);
	ImageState imgState = ImageState::fromChunk(buffer_ptr, width * height);

	const float focal_x = height / (2.f * tan_fovy);
	const float focal_y = width / (2.f * tan_fovx);

	// Run preprocessing kernel
	DEBUG_PRINT("Calling preprocess kernel\n");
	CHECK_CUDA(preprocess<<<(num_nodes+255)/256, 256>>>(
		num_nodes, active_sh_degree, num_sh_coefs,
		positions, depths, scale_modifier,
		shs, geomState.clamped, colors_precomp,
		viewmatrix, projmatrix, (glm::vec3*)cam_pos,
		width, height, tan_fovx, tan_fovy, focal_x, focal_y, aabb,
		geomState.bboxes, geomState.depths, geomState.rgb,
		grid, geomState.tiles_touched, geomState.morton_codes
	));

	// Compute prefix sum over full list of touched tile counts by voxels
	// E.g., [2, 3, 0, 2, 1] -> [2, 5, 5, 7, 8]
	CHECK_CUDA(cub::DeviceScan::InclusiveSum(
		geomState.scanning_space, geomState.scan_size,
		geomState.tiles_touched, geomState.point_offsets, num_nodes
	));

	// Retrieve total number of voxel instances to launch
	int num_rendered;
	CHECK_CUDA(cudaMemcpy(&num_rendered, geomState.point_offsets + num_nodes - 1, sizeof(int), cudaMemcpyDeviceToHost));
	if (num_rendered == 0)
		return 0;

	// Allocate buffer for binning state
	DEBUG_PRINT("Allocating binning buffer\n");
	DEBUG_PRINT("    - Number of rendered nodes: %d\n", num_rendered);
	buffer_size = required<BinningState>(num_rendered);
	DEBUG_PRINT("    - Binning buffer size: %zu\n", buffer_size);
	buffer_ptr = binningBuffer(buffer_size);
	BinningState binningState = BinningState::fromChunk(buffer_ptr, num_rendered);

	// For each instance to be rendered, produce adequate [ tile | depth ] key 
	// and corresponding dublicated voxel indices to be sorted
	DEBUG_PRINT("Calling duplicateWithKeys kernel\n");
	CHECK_CUDA(duplicateWithKeys<<<(num_nodes+255)/256, 256>>>(
		num_nodes, geomState.morton_codes, geomState.point_offsets,
		binningState.point_list_keys_unsorted, binningState.point_list_unsorted,
		geomState.bboxes, grid
	));

	// Sort complete list of (duplicated) voxel indices by keys
	int bit = getHigherMsb(grid.x * grid.y);
	CHECK_CUDA(cub::DeviceRadixSort::SortPairs(
		binningState.list_sorting_space, binningState.sorting_size,
		binningState.point_list_keys_unsorted, binningState.point_list_keys,
		binningState.point_list_unsorted, binningState.point_list,
		num_rendered, 0, 32 + bit
	));

	// Identify start and end of per-tile workloads in sorted list
	CHECK_CUDA(cudaMemset(imgState.ranges, 0, grid.x * grid.y * sizeof(uint2)));
	CHECK_CUDA(identifyTileRanges<<<(num_rendered+255)/256, 256>>>(
		num_rendered, binningState.point_list_keys, imgState.ranges
	));

	// Let each tile blend its range of voxels independently in parallel
	const float* color_ptr = colors_precomp != nullptr ? colors_precomp : geomState.rgb;
	DEBUG_PRINT("Calling render kernel\n");
	CHECK_CUDA(render<NUM_CHANNELS><<<grid, block>>>(
		imgState.ranges, binningState.point_list,
		width, height, background,
		(float3*)cam_pos, tan_fovx, tan_fovy, viewmatrix, aabb,
		positions, color_ptr, geomState.depths, depths, scale_modifier, densities,
		imgState.accum_alpha, imgState.wm_sum, imgState.n_contrib,
		out_color, out_depth, out_alpha, out_distloss
	));

	return num_rendered;
}