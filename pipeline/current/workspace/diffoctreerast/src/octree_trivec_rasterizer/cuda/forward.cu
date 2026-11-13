#include <cuda.h>
#include "cuda_runtime.h"
#include "device_launch_parameters.h"
#include <cub/cub.cuh>
#include <cub/device/device_radix_sort.cuh>
#include <glm/glm.hpp>

#include <cooperative_groups.h>
#include <cooperative_groups/memcpy_async.h>
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
 * Forward pass for converting the input spherical harmonics coefficients of each voxel to color.
 * 
 * @param deg Degree of the spherical harmonics coefficients.
 * @param max_coeffs Maximum number of coefficients.
 * @param mean 3D points.
 * @param campos Camera position.
 * @param sh spherical harmonics coefficients.
 * @param color Output color.
 */
static __device__ void computeColorFromSH(int deg, int max_coeffs, int trivec_rank, const glm::vec3* mean, glm::vec3 campos, const float* sh, float* color) {
	// The implementation is loosely based on code for 
	// "Differentiable Point-Based Radiance Fields for 
	// Efficient View Synthesis" by Zhang et al. (2022)
	glm::vec3 pos = *mean;
	glm::vec3 dir = pos - campos;
	dir = dir / glm::length(dir);

	for (int tc = 0; tc < trivec_rank; tc++)
		for (int ch = 0; ch < CHANNELS; ch++)
			color[tc * CHANNELS + ch] = SH_C0 * sh[tc * max_coeffs * CHANNELS + ch];

	if (deg > 0)
	{
		float x = dir.x;
		float y = dir.y;
		float z = dir.z;
		float coeff[3] = { -SH_C1 * y, SH_C1 * z, -SH_C1 * x };
		for (int tc = 0; tc < trivec_rank; tc++)
			for (int ch = 0; ch < CHANNELS; ch++)
				color[tc * CHANNELS + ch] +=
					coeff[0] * sh[tc * max_coeffs * CHANNELS + 1 * CHANNELS + ch] +
					coeff[1] * sh[tc * max_coeffs * CHANNELS + 2 * CHANNELS + ch] +
					coeff[2] * sh[tc * max_coeffs * CHANNELS + 3 * CHANNELS + ch];

		if (deg > 1)
		{
			float xx = x * x, yy = y * y, zz = z * z;
			float xy = x * y, yz = y * z, xz = x * z;
			float coeff[5] = { SH_C2[0] * xy, SH_C2[1] * yz, SH_C2[2] * (2.0f * zz - xx - yy), SH_C2[3] * xz, SH_C2[4] * (xx - yy) };
			for (int tc = 0; tc < trivec_rank; tc++)
				for (int ch = 0; ch < CHANNELS; ch++)
					color[tc * CHANNELS + ch] +=
						coeff[0] * sh[tc * max_coeffs * CHANNELS + 4 * CHANNELS + ch] +
						coeff[1] * sh[tc * max_coeffs * CHANNELS + 5 * CHANNELS + ch] +
						coeff[2] * sh[tc * max_coeffs * CHANNELS + 6 * CHANNELS + ch] +
						coeff[3] * sh[tc * max_coeffs * CHANNELS + 7 * CHANNELS + ch] +
						coeff[4] * sh[tc * max_coeffs * CHANNELS + 8 * CHANNELS + ch];

			if (deg > 2)
			{
				float coeff[7] = {
					SH_C3[0] * y * (3.0f * xx - yy),
					SH_C3[1] * xy * z,
					SH_C3[2] * y * (4.0f * zz - xx - yy),
					SH_C3[3] * z * (2.0f * zz - 3.0f * xx - 3.0f * yy),
					SH_C3[4] * x * (4.0f * zz - xx - yy),
					SH_C3[5] * z * (xx - yy),
					SH_C3[6] * x * (xx - 3.0f * yy)
				};
				for (int tc = 0; tc < trivec_rank; tc++)
					for (int ch = 0; ch < CHANNELS; ch++)
						color[tc * CHANNELS + ch] +=
							coeff[0] * sh[tc * max_coeffs * CHANNELS + 9 * CHANNELS + ch] +
							coeff[1] * sh[tc * max_coeffs * CHANNELS + 10 * CHANNELS + ch] +
							coeff[2] * sh[tc * max_coeffs * CHANNELS + 11 * CHANNELS + ch] +
							coeff[3] * sh[tc * max_coeffs * CHANNELS + 12 * CHANNELS + ch] +
							coeff[4] * sh[tc * max_coeffs * CHANNELS + 13 * CHANNELS + ch] +
							coeff[5] * sh[tc * max_coeffs * CHANNELS + 14 * CHANNELS + ch] +
							coeff[6] * sh[tc * max_coeffs * CHANNELS + 15 * CHANNELS + ch];
			}
		}
	}
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
	const int trivec_rank,
	const float* positions,
	const float* shs,
	const uint8_t* tree_depths,
	const float scale_modifier,
	const float* viewmatrix,
	const float* projmatrix,
	const glm::vec3* cam_pos,
	const int width,
	const int height,
	const float* aabb,
	float* colors,
	int4* bboxes,
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
	if (shs) {
		computeColorFromSH(
			active_sh_degree, num_sh_coefs, trivec_rank,
			(glm::vec3*)&p_orig, *cam_pos,
			shs + idx * trivec_rank * num_sh_coefs * CHANNELS,
			colors + idx * trivec_rank * CHANNELS
		);
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


static __device__ void sample_trivec(
	const float* trivec,
	const int trivec_dim,
	const float* densities,
	const float* colors,
	const float density_shift,
	const int used_rank,
	const float3& p,
	const float3& voxel_min,
	const float3& voxel_max,
	float& out_density,
	float* out_color
) {
	// Compute linear interpolation weights
	float3 _p = {
		(p.x - voxel_min.x) / (voxel_max.x - voxel_min.x) * trivec_dim - 0.5f,
		(p.y - voxel_min.y) / (voxel_max.y - voxel_min.y) * trivec_dim - 0.5f,
		(p.z - voxel_min.z) / (voxel_max.z - voxel_min.z) * trivec_dim - 0.5f
	};
	int3 _ip = { 
		min(trivec_dim - 2, max(0, (int)_p.x)),
		min(trivec_dim - 2, max(0, (int)_p.y)),
		min(trivec_dim - 2, max(0, (int)_p.z))
	};
	float3 w = { _p.x - _ip.x, _p.y - _ip.y, _p.z - _ip.z };

	float _density;
	for (int i = 0; i < used_rank; i++) {
		_density = lerp(trivec[TRIVEC_X_CH(trivec_dim, i) + _ip.x], trivec[TRIVEC_X_CH(trivec_dim, i) + _ip.x + 1], w.x)
				 * lerp(trivec[TRIVEC_Y_CH(trivec_dim, i) + _ip.y], trivec[TRIVEC_Y_CH(trivec_dim, i) + _ip.y + 1], w.y)
				 * lerp(trivec[TRIVEC_Z_CH(trivec_dim, i) + _ip.z], trivec[TRIVEC_Z_CH(trivec_dim, i) + _ip.z + 1], w.z);
		out_density += densities[i] * _density;
		for (int j = 0; j < CHANNELS; j++)
			out_color[j] += colors[CHANNELS * i + j] * _density;
	}
	for (int j = 0; j < CHANNELS; j++)
		out_color[j] = sigmoid(out_color[j]);
	out_density = softplus(out_density - density_shift * 10) * min(1 / (1 - density_shift), 25.0f);
}


/**
 * Main rasterization method. Collaboratively works on one tile per
 * block, each thread treats one pixel. Alternates between fetching 
 * and rasterizing data.
 * 
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
 * @param trivecs Trivec features of octree nodes.
 * @param trivec_rank Trivec rank.
 * @param trivec_dim Trivec dimension.
 * @param densities Densities of octree nodes.
 * @param density_shift Density shift.
 * @param colors Colors of octree nodes.
 * @param tree_depths Depths of octree nodes.
 * @param scale_modifier Scale modifier.
 * @param random_image Random image.
 * @param n_contrib Number of contributors.
 * @param out_color Output color.
 * @param out_depth Output depth.
 * @param out_alpha Output alpha.
 * @param out_percent_depth Output percent depth.
 */
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
	const float* __restrict__ trivecs,
	const int trivec_rank,
	const int trivec_dim,
	const float* __restrict__ densities,
	const float density_shift,
	const float* __restrict__ colors,
	const int used_rank,
	const uint8_t* __restrict__ tree_depths,
	const float scale_modifier,
	const float* __restrict__ random_image,
	const float* __restrict__ colors_overwrite,
	uint32_t* __restrict__ n_contrib,
	uint32_t* __restrict__ t_contrib,
	float* __restrict__ out_color,
	float* __restrict__ out_depth,
	float* __restrict__ out_alpha,
	float* __restrict__ out_percent_depth

	// DEBUG
	// ,int dbg_ray_id,
	// float* __restrict__ dbg_position,
	// float* __restrict__ dbg_density,
	// float* __restrict__ dbg_color,
	// float* __restrict__ dbg_weight
) {
	// Identify current tile and associated min/max pixel range.
	auto block = cg::this_thread_block();
	const uint32_t horizontal_blocks = (W + BLOCK_X - 1) / BLOCK_X;
	const uint2 pix_min = { block.group_index().x * BLOCK_X, block.group_index().y * BLOCK_Y };
	const uint2 pix = { pix_min.x + block.thread_index().x, pix_min.y + block.thread_index().y };
	const uint32_t pix_id = W * pix.y + pix.x;

	// Get ray direction and origin for this pixel.
	const float2 jt_pix = { pix.x + random_image[pix_id * 3 + 0], pix.y + random_image[pix_id * 3 + 1] };
	float3 ray_dir = getRayDir(jt_pix, W, H, tan_fovx, tan_fovy, viewmatrix);
	float depth_div = 1.0f / sqrtf(ray_dir.x * ray_dir.x + ray_dir.y * ray_dir.y + ray_dir.z * ray_dir.z);
	ray_dir.x *= depth_div;
	ray_dir.y *= depth_div;
	ray_dir.z *= depth_div;

	// Check if this thread is associated with a valid pixel or outside.
	const bool inside = pix.x < W&& pix.y < H;
	// Done threads can help with fetching, but don't rasterize
	bool done = !inside;

	// Load start/end range of IDs to process in bit sorted list.
	uint2 range = ranges[block.group_index().y * horizontal_blocks + block.group_index().x];
	const int rounds = ((range.y - range.x + PREFETCH_BUFFER_SIZE - 1) / PREFETCH_BUFFER_SIZE);
	int toDo = range.y - range.x;

	// Allocate storage for shared memory
	const int trivec_size = TRIVEC_SIZE(trivec_dim, trivec_rank);
	extern __shared__ char shared_mem[];
	uint32_t* collected_ids = (uint32_t*)shared_mem;
	float3* collected_scales = (float3*)(collected_ids + PREFETCH_BUFFER_SIZE);
	float3* collected_xyz = (float3*)(collected_scales + PREFETCH_BUFFER_SIZE);
	float* collected_trivecs = (float*)(collected_xyz + PREFETCH_BUFFER_SIZE);
	float* collected_densities = (float*)(collected_trivecs + PREFETCH_BUFFER_SIZE * trivec_size);
	float* collected_colors = (float*)(collected_densities + PREFETCH_BUFFER_SIZE * trivec_rank);

	// Initialize helper variables
	float T = 1.0f;
	uint32_t contributor = 0;
	uint32_t last_contributor = 0;
	uint32_t last_t = 0;
	float C[CHANNELS] = { 0 };
	float D = 0, PD = 0;
	const float jitter = random_image[pix_id * 3 + 2];

	// Iterate over batches until all done or range is complete
	for (int i = 0; i < rounds; i++, toDo -= PREFETCH_BUFFER_SIZE)
	{
		// End if entire block votes that it is done rasterizing
		int num_done = __syncthreads_count(done);
		if (num_done == BLOCK_SIZE)
			break;

		// Collectively fetch per-voxel data from global to shared
		#ifndef ASYNC_GLOBAL_TO_SHARED
		for (int j = 0; j < PREFETCH_BUFFER_SIZE; j++)
		{
			int progress = i * PREFETCH_BUFFER_SIZE + j;
			if (range.x + progress < range.y)
			{
				int coll_id = point_list[range.x + progress];
				float nsize = powf(2.0f, -(float)tree_depths[coll_id]) * scale_modifier;
				if (block.thread_rank() == 0) {
					collected_ids[j] = coll_id;
					collected_scales[j] = { aabb[3] * nsize, aabb[4] * nsize, aabb[5] * nsize };
					collected_xyz[j] = {
						positions[3 * coll_id] * aabb[3] + aabb[0],
						positions[3 * coll_id + 1] * aabb[4] + aabb[1],
						positions[3 * coll_id + 2] * aabb[5] + aabb[2]
					};
				}
				for (int ch = block.thread_rank(); ch < trivec_size; ch += BLOCK_SIZE)
					collected_trivecs[j * trivec_size + ch] = trivecs[coll_id * trivec_size + ch];
				for (int ch = block.thread_rank(); ch < trivec_rank; ch += BLOCK_SIZE)
					collected_densities[j * trivec_rank + ch] = densities[coll_id * trivec_rank + ch];
				for (int ch = block.thread_rank(); ch < trivec_rank * CHANNELS; ch += BLOCK_SIZE)
					collected_colors[j * trivec_rank * CHANNELS + ch] = colors[coll_id * trivec_rank * CHANNELS + ch];
			}
		}
		block.sync();
		#else
		for (int j = 0; j < PREFETCH_BUFFER_SIZE; j++)
		{
			int progress = i * PREFETCH_BUFFER_SIZE + j;
			if (range.x + progress < range.y)
			{
				int coll_id = point_list[range.x + progress];
				float nsize = powf(2.0f, -(float)tree_depths[coll_id]) * scale_modifier;
				if (block.thread_rank() == 0) {
					collected_ids[j] = coll_id;
					collected_scales[j] = { aabb[3] * nsize, aabb[4] * nsize, aabb[5] * nsize };
					collected_xyz[j] = {
						positions[3 * coll_id] * aabb[3] + aabb[0],
						positions[3 * coll_id + 1] * aabb[4] + aabb[1],
						positions[3 * coll_id + 2] * aabb[5] + aabb[2]
					};
				}
				cg::memcpy_async(block, collected_trivecs + j * trivec_size, trivecs + coll_id * trivec_size, trivec_size * sizeof(float));
				cg::memcpy_async(block, collected_densities + j * trivec_rank, densities + coll_id * trivec_rank, trivec_rank * sizeof(float));
				cg::memcpy_async(block, collected_colors + j * trivec_rank * CHANNELS, colors + coll_id * trivec_rank * CHANNELS, trivec_rank * CHANNELS * sizeof(float));
			}
		}
		cg::wait(block);
		block.sync();
		#endif

		// Iterate over current batch
		for (int j = 0; !done && j < min(PREFETCH_BUFFER_SIZE, toDo); j++)
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
			float step = (0.5f / trivec_dim) * scale.x;
			int t_start = (int)ceil(itsc.x / step - jitter);
			int t_end = (int)floor(itsc.y / step - jitter);
			for (int t = t_start; t <= t_end; t++) {	
				float z = (t + jitter) * step;

				// Sample trivec
				float density = 0;
				float color[CHANNELS] = { 0 };
				p = { cam_pos->x + z * ray_dir.x, cam_pos->y + z * ray_dir.y, cam_pos->z + z * ray_dir.z };
				sample_trivec(
					collected_trivecs + j * trivec_size, trivec_dim,
					collected_densities + j * trivec_rank,
					collected_colors + j * trivec_rank * CHANNELS,
					density_shift, used_rank,
					p, voxel_min, voxel_max,
					density, color
				);
				if (colors_overwrite != nullptr)
					for (int k = 0; k < CHANNELS; k++)
						color[k] = colors_overwrite[CHANNELS * collected_ids[j] + k];

				// Accumulate
				float alpha = min(1 - exp(-density * step), 0.999f);
				const float weight = alpha * T;
				for (int k = 0; k < CHANNELS; k++)
					C[k] += color[k] * weight;
				D += z * weight;

				T *= 1 - alpha;
				if (PD == 0 && T < 0.5f)
					PD = z;
				last_t = t;
				if (T < 0.001f)
					break;

				// DEBUG
				// if (pix_id == dbg_ray_id) {
				// 	dbg_position[3 * t + 0] = p.x;
				// 	dbg_position[3 * t + 1] = p.y;
				// 	dbg_position[3 * t + 2] = p.z;
				// 	dbg_density[t] = density;
				// 	for (int k = 0; k < CHANNELS; k++)
				// 		dbg_color[CHANNELS * t + k] = color[k];
				// 	dbg_weight[t] = weight;
				// }
			}

			// Keep track of last range entry to update this pixel.
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
		n_contrib[pix_id] = last_contributor;
		t_contrib[pix_id] = last_t;
		for (int k = 0; k < CHANNELS; k++)
			out_color[k * H * W + pix_id] = C[k] + T * bg_color[k];
		out_depth[pix_id] = D * depth_div;
		out_alpha[pix_id] = 1.0f - T;
		out_percent_depth[pix_id] = PD * depth_div;
	}
}

int OctreeTrivecRasterizer::CUDA::forward(
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
    const float* trivecs,
	const int trivec_rank,
	const int trivec_dim,
	const float* densities,
	const float density_shift,
    const float* shs,
    const float* colors,
    const int used_rank,
    const uint8_t* depths,
	const float scale_modifier,
	const float* viewmatrix,
	const float* projmatrix,
    const float* cam_pos,
	const float tan_fovx,
    const float tan_fovy,
    const float* random_image,
	const float* colors_overwrite,
    float* out_color,
    float* out_depth,
    float* out_alpha,
	float* out_percent_depth

	// DEBUG
	// ,int dbg_ray_id,
	// float* dbg_position,
	// float* dbg_density,
	// float* dbg_color,
	// float* dbg_weight
) {
	DEBUG_PRINT("Starting forward pass\n");
	DEBUG_PRINT("    - Number of nodes: %d\n", num_nodes);
	DEBUG_PRINT("    - Image size: %d x %d\n", width, height);
	DEBUG_PRINT("    - Trivec rank: %d\n", trivec_rank);
	DEBUG_PRINT("    - Trivec dimension: %d\n", trivec_dim);

	// Parrallel config (2D grid of 2D blocks)
	dim3 grid((width + BLOCK_X - 1) / BLOCK_X, (height + BLOCK_Y - 1) / BLOCK_Y, 1);
	dim3 block(BLOCK_X, BLOCK_Y, 1);

	// Allocate buffers for auxiliary info for points and pixels
	DEBUG_PRINT("Allocating buffers\n");
	size_t buffer_size;
	char* buffer_ptr;
	buffer_size = required<GeometryState>(num_nodes, trivec_rank);
	DEBUG_PRINT("    - Geometry buffer size: %zu\n", buffer_size);
	buffer_ptr = geometryBuffer(buffer_size);
	GeometryState geomState = GeometryState::fromChunk(buffer_ptr, num_nodes, trivec_rank);
	buffer_size = required<ImageState>(width * height);
	DEBUG_PRINT("    - Image buffer size: %zu\n", buffer_size);
	buffer_ptr = imageBuffer(buffer_size);
	ImageState imgState = ImageState::fromChunk(buffer_ptr, width * height);

	// Run preprocessing kernel
	DEBUG_PRINT("Calling preprocess kernel\n");
	CHECK_CUDA(preprocess<<<(num_nodes+255)/256, 256>>>(
		num_nodes, active_sh_degree, num_sh_coefs, trivec_rank,
		positions, shs, depths, scale_modifier,
		viewmatrix, projmatrix, (glm::vec3*)cam_pos,
		width, height, aabb, geomState.colors,
		geomState.bboxes, grid, geomState.tiles_touched, geomState.morton_codes
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
	const float* color_ptr = (shs) ? geomState.colors : colors;
	size_t used_memory = PREFETCH_BUFFER_SIZE * (
		sizeof(uint32_t) + 															// collected_ids
		sizeof(float3) + 															// collected_scales
		sizeof(float3) + 															// collected_xyz
		TRIVEC_SIZE(trivec_dim, trivec_rank) * sizeof(float) + 						// collected_trivecs
		trivec_rank * sizeof(float) + 												// collected_densities
		trivec_rank * CHANNELS * sizeof(float) 										// collected_colors
	);
	DEBUG_PRINT("Calling render kernel\n");
	DEBUG_PRINT("    - Used shared memory: %zu\n", used_memory);
	CHECK_CUDA(render<<<grid, block, used_memory>>>(
		imgState.ranges, binningState.point_list,
		width, height, background,
		(float3*)cam_pos, tan_fovx, tan_fovy, viewmatrix, aabb,
		positions, trivecs, trivec_rank, trivec_dim, densities, density_shift, color_ptr, used_rank, depths, scale_modifier, random_image, colors_overwrite,
		imgState.n_contrib, imgState.t_contrib, out_color, out_depth, out_alpha, out_percent_depth
		// DEBUG
		// ,dbg_ray_id, dbg_position, dbg_density, dbg_color, dbg_weight
	));

	return num_rendered;
}