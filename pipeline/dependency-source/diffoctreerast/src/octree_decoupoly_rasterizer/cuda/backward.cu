#include <cuda.h>
#include "cuda_runtime.h"
#include "device_launch_parameters.h"
#include <cub/cub.cuh>
#include <cub/block/block_reduce.cuh>
#include <cooperative_groups.h>
#include <cooperative_groups/memcpy_async.h>
#include <glm/glm.hpp>
namespace cg = cooperative_groups;

#include "config.h"
#include "auxiliary.h"
#include "data_structure.h"
#include "api.h"


/**
 * Backward pass for converting the input spherical harmonics coefficients of each voxel to a simple RGB color.
 * 
 * @param deg Degree of the spherical harmonics coefficients.
 * @param max_coeffs Maximum number of coefficients.
 * @param mean Array of 3D points.
 * @param campos Camera position.
 * @param sh Array of spherical harmonics coefficients.
 * @param dL_dcolor Gradient of the output colors.
 * @param dL_dsh Gradient of the input spherical harmonics coefficients.
 */
static __device__ void computeColorFromSHBackward(int deg, int max_coeffs, const glm::vec3* mean, glm::vec3 campos, const float* sh, const float* dL_dcolor, float* dL_dsh) {
	// Compute intermediate values, as it is done during forward
	glm::vec3 pos = *mean;
	glm::vec3 dir = pos - campos;
	dir = dir / glm::length(dir);

	for (int tc = 0; tc < DECOUPOLY_RANK; tc++)
		for (int ch = 0; ch < CHANNELS; ch++)
			dL_dsh[tc * max_coeffs * CHANNELS + ch] = SH_C0 * dL_dcolor[tc * CHANNELS + ch];

	if (deg > 0)
	{
		float x = dir.x;
		float y = dir.y;
		float z = dir.z;
		float coeff[3] = { -SH_C1 * y, SH_C1 * z, -SH_C1 * x };
		for (int tc = 0; tc < DECOUPOLY_RANK; tc++) {
			for (int ch = 0; ch < CHANNELS; ch++) {
				dL_dsh[tc * max_coeffs * CHANNELS + 1 * CHANNELS + ch] = coeff[0] * dL_dcolor[tc * CHANNELS + ch];
				dL_dsh[tc * max_coeffs * CHANNELS + 2 * CHANNELS + ch] = coeff[1] * dL_dcolor[tc * CHANNELS + ch];
				dL_dsh[tc * max_coeffs * CHANNELS + 3 * CHANNELS + ch] = coeff[2] * dL_dcolor[tc * CHANNELS + ch];
			}
		}

		if (deg > 1)
		{
			float xx = x * x, yy = y * y, zz = z * z;
			float xy = x * y, yz = y * z, xz = x * z;
			float coeff[5] = { SH_C2[0] * xy, SH_C2[1] * yz, SH_C2[2] * (2.0f * zz - xx - yy), SH_C2[3] * xz, SH_C2[4] * (xx - yy) };
			for (int tc = 0; tc < DECOUPOLY_RANK; tc++) {
				for (int ch = 0; ch < CHANNELS; ch++) {
					dL_dsh[tc * max_coeffs * CHANNELS + 4 * CHANNELS + ch] = coeff[0] * dL_dcolor[tc * CHANNELS + ch];
					dL_dsh[tc * max_coeffs * CHANNELS + 5 * CHANNELS + ch] = coeff[1] * dL_dcolor[tc * CHANNELS + ch];
					dL_dsh[tc * max_coeffs * CHANNELS + 6 * CHANNELS + ch] = coeff[2] * dL_dcolor[tc * CHANNELS + ch];
					dL_dsh[tc * max_coeffs * CHANNELS + 7 * CHANNELS + ch] = coeff[3] * dL_dcolor[tc * CHANNELS + ch];
					dL_dsh[tc * max_coeffs * CHANNELS + 8 * CHANNELS + ch] = coeff[4] * dL_dcolor[tc * CHANNELS + ch];
				}
			}

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
				for (int tc = 0; tc < DECOUPOLY_RANK; tc++) {
					for (int ch = 0; ch < CHANNELS; ch++) {
						dL_dsh[tc * max_coeffs * CHANNELS + 9 * CHANNELS + ch] = coeff[0] * dL_dcolor[tc * CHANNELS + ch];
						dL_dsh[tc * max_coeffs * CHANNELS + 10 * CHANNELS + ch] = coeff[1] * dL_dcolor[tc * CHANNELS + ch];
						dL_dsh[tc * max_coeffs * CHANNELS + 11 * CHANNELS + ch] = coeff[2] * dL_dcolor[tc * CHANNELS + ch];
						dL_dsh[tc * max_coeffs * CHANNELS + 12 * CHANNELS + ch] = coeff[3] * dL_dcolor[tc * CHANNELS + ch];
						dL_dsh[tc * max_coeffs * CHANNELS + 13 * CHANNELS + ch] = coeff[4] * dL_dcolor[tc * CHANNELS + ch];
						dL_dsh[tc * max_coeffs * CHANNELS + 14 * CHANNELS + ch] = coeff[5] * dL_dcolor[tc * CHANNELS + ch];
						dL_dsh[tc * max_coeffs * CHANNELS + 15 * CHANNELS + ch] = coeff[6] * dL_dcolor[tc * CHANNELS + ch];
					}
				}
			}
		}
	}
}


/**
 * Backward pass of the preprocessing steps
 */
static __global__ void preprocessBackward(
	const int num_points,
	const int active_sh_degree,
	const int num_sh_coefs,
	const float* positions,
	const float* shs,
	const glm::vec3* cam_pos,
	const float* aabb,
    float* grad_colors,
    float* grad_shs
) {
	auto idx = cg::this_grid().thread_rank();
	if (idx >= num_points)
		return;

	float3 p_orig = {
		positions[3 * idx] * aabb[3] + aabb[0],
		positions[3 * idx + 1] * aabb[4] + aabb[1],
		positions[3 * idx + 2] * aabb[5] + aabb[2]
	};

	// Compute gradient updates due to computing colors from SHs
	if (shs)
		computeColorFromSHBackward(
            active_sh_degree, num_sh_coefs, (glm::vec3*)&p_orig, *cam_pos,
            shs + idx * DECOUPOLY_RANK * num_sh_coefs * CHANNELS,
			grad_colors + idx * DECOUPOLY_RANK * CHANNELS,
			grad_shs + idx * DECOUPOLY_RANK * num_sh_coefs * CHANNELS
       	);
}


static __device__ void sample_decoupoly(
	const float* decoupoly_V,
	const float* decoupoly_g,
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
	float3 _p = {
		((p.x - voxel_min.x) / (voxel_max.x - voxel_min.x) - 0.5f) * 2.0f,
		((p.y - voxel_min.y) / (voxel_max.y - voxel_min.y) - 0.5f) * 2.0f,
		((p.z - voxel_min.z) / (voxel_max.z - voxel_min.z) - 0.5f) * 2.0f
	};

	float _value;
	float _x, _x_n;
	for (int i = 0; i < used_rank; i++) {
		_x_n = 1.0f;
		_x = decoupoly_V[i * 3 + 0] * _p.x + decoupoly_V[i * 3 + 1] * _p.y + decoupoly_V[i * 3 + 2] * _p.z;
		_value = 1.0f;
		for (int j = 0; j < DECOUPOLY_DEGREE; j++) {
			_value += decoupoly_g[i * DECOUPOLY_DEGREE + j] * _x_n;
			_x_n *= _x;
		}

		out_density += densities[i] * _value;
		for (int j = 0; j < CHANNELS; j++)
			out_color[j] += colors[CHANNELS * i + j] * _value;
	}
	for (int j = 0; j < CHANNELS; j++)
		out_color[j] = sigmoid(out_color[j]);
	out_density = softplus(out_density - density_shift * 10) * min(1 / (1 - density_shift), 25.0f);
}


static __device__ void sample_decoupoly_backward(
	const float* decoupoly_V,
	const float* decoupoly_g,
	const float* densities,
	const float* colors,
	const float density_shift,
	const int used_rank,
	const float& density,
	const float* color,
	const float& grad_density,
	const float* grad_color,
	const float3& p,
	const float3& voxel_min,
	const float3& voxel_max,
	float* decoupoly_V_grad,
	float* decoupoly_g_grad,
	float* density_grad,
	float* color_grad
) {
	float3 _p = {
		((p.x - voxel_min.x) / (voxel_max.x - voxel_min.x) - 0.5f) * 2.0f,
		((p.y - voxel_min.y) / (voxel_max.y - voxel_min.y) - 0.5f) * 2.0f,
		((p.z - voxel_min.z) / (voxel_max.z - voxel_min.z) - 0.5f) * 2.0f
	};

	float ramp = min(1 / (1 - density_shift), 25.0f);
	float _grad_density = grad_density * softplus_prime(density / ramp) * ramp;
	float _grad_color[CHANNELS];
	for (int j = 0; j < CHANNELS; j++)
		_grad_color[j] = grad_color[j] * sigmoid_prime(color[j]);

	float _value;
	float _x, _x_n, _grad_value, _grad_x;
	for (int i = 0; i < used_rank; i++) {
		_x_n = 1.0f;
		_x = decoupoly_V[i * 3 + 0] * _p.x + decoupoly_V[i * 3 + 1] * _p.y + decoupoly_V[i * 3 + 2] * _p.z;
		_value = 1.0f;

		// grad of _value
		_grad_value = _grad_density * densities[i];
		for (int j = 0; j < CHANNELS; j++)
			_grad_value += _grad_color[j] * colors[CHANNELS * i + j];

		_grad_x = 0;
		for (int j = 0; j < DECOUPOLY_DEGREE; j++) {
			_value += decoupoly_g[i * DECOUPOLY_DEGREE + j] * _x_n;

			// grad of _x
			if (j < DECOUPOLY_DEGREE - 1)
				_grad_x += decoupoly_g[i * DECOUPOLY_DEGREE + j + 1] * _grad_value * _x_n * (j + 1);

			// grad of decoupoly_g
			atomicAdd(decoupoly_g_grad + i * DECOUPOLY_DEGREE + j, _grad_value * _x_n);

			_x_n *= _x;
		}

		// grad of decoupoly_V
		atomicAdd(decoupoly_V_grad + i * 3 + 0, _grad_x * _p.x);
		atomicAdd(decoupoly_V_grad + i * 3 + 1, _grad_x * _p.y);
		atomicAdd(decoupoly_V_grad + i * 3 + 2, _grad_x * _p.z);
		
		// grad of density
		atomicAdd(density_grad + i, _grad_density * _value);
		
		// grad of color
		for (int j = 0; j < CHANNELS; j++) {
			atomicAdd(color_grad + i * CHANNELS + j, _grad_color[j] * _value);
		}
	}
}


static __device__ void sample_decoupoly_backward_local(
	const float* decoupoly_V,
	const float* decoupoly_g,
	const float* densities,
	const float* colors,
	const float density_shift,
	const int used_rank,
	const float& density,
	const float* color,
	const float& grad_density,
	const float* grad_color,
	const float3& p,
	const float3& voxel_min,
	const float3& voxel_max,
	float* decoupoly_V_grad,
	float* decoupoly_g_grad,
	float* density_grad,
	float* color_grad
) {
	float3 _p = {
		((p.x - voxel_min.x) / (voxel_max.x - voxel_min.x) - 0.5f) * 2.0f,
		((p.y - voxel_min.y) / (voxel_max.y - voxel_min.y) - 0.5f) * 2.0f,
		((p.z - voxel_min.z) / (voxel_max.z - voxel_min.z) - 0.5f) * 2.0f
	};

	float ramp = min(1 / (1 - density_shift), 25.0f);
	float _grad_density = grad_density * softplus_prime(density / ramp) * ramp;
	float _grad_color[CHANNELS];
	for (int j = 0; j < CHANNELS; j++)
		_grad_color[j] = grad_color[j] * sigmoid_prime(color[j]);

	float _value;
	float _x, _x_n, _grad_value, _grad_x;
	for (int i = 0; i < used_rank; i++) {
		_x_n = 1.0f;
		_x = decoupoly_V[i * 3 + 0] * _p.x + decoupoly_V[i * 3 + 1] * _p.y + decoupoly_V[i * 3 + 2] * _p.z;
		_value = 1.0f;

		// grad of _value
		_grad_value = _grad_density * densities[i];
		for (int j = 0; j < CHANNELS; j++)
			_grad_value += _grad_color[j] * colors[CHANNELS * i + j];

		_grad_x = 0;
		for (int j = 0; j < DECOUPOLY_DEGREE; j++) {
			_value += decoupoly_g[i * DECOUPOLY_DEGREE + j] * _x_n;

			// grad of _x
			if (j < DECOUPOLY_DEGREE - 1)
				_grad_x += decoupoly_g[i * DECOUPOLY_DEGREE + j + 1] * _grad_value * _x_n * (j + 1);

			// grad of decoupoly_g
			decoupoly_g_grad[i * DECOUPOLY_DEGREE + j] += _grad_value * _x_n;

			_x_n *= _x;
		}

		// grad of decoupoly_V
		decoupoly_V_grad[i * 3 + 0] += _grad_x * _p.x;
		decoupoly_V_grad[i * 3 + 1] += _grad_x * _p.y;
		decoupoly_V_grad[i * 3 + 2] += _grad_x * _p.z;
		
		// grad of density
		density_grad[i] += _grad_density * _value;
		
		// grad of color
		for (int j = 0; j < CHANNELS; j++) {
			color_grad[i * CHANNELS + j] += _grad_color[j] * _value;
		}
	}
}


/**
 * Backward version of the rendering procedure.
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
 * @param positions Positions of octree nodes.
 * @param decoupolys Decoupoly features of octree nodes.
 * @param densities Densities of octree nodes.
 * @param density_shift Shift of densities.
 * @param colors Colors of octree nodes.
 * @param used_rank Rank of the used decoupoly channel.
 * @param tree_depths Depths of octree nodes.
 * @param scale_modifier Scale modifier.
 * @param random_image Random image.
 * @param densities densities of octree nodes.
 * @param n_contrib Number of contributors.
 * @param out_color Output color.
 * @param out_depth Output depth.
 * @param out_alpha Output alpha.
 * @param grad_out_colors Gradient of output colors.
 * @param grad_out_depths Gradient of output depths.
 * @param grad_out_alphas Gradient of output alphas.
 * @param grad_decoupolys_V Gradient of decoupolys.
 * @param grad_decoupolys_g Gradient of decoupolys.
 * @param grad_densities Gradient of densities.
 * @param grad_colors Gradient of colors.
 * @param aux_grad_colors2 Auxiliary gradient of squared colors.
 * @param aux_contributions Auxiliary contributions.
 */
static __global__ void __launch_bounds__(BLOCK_X * BLOCK_Y)
renderBackward(
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
	const float* __restrict__ decoupolys_V,
	const float* __restrict__ decoupolys_g,
	const float* __restrict__ densities,
	const float density_shift,
	const float* __restrict__ colors,
	const int used_rank,
	const uint8_t* __restrict__ tree_depths,
	const float scale_modifier,
	const float* __restrict__ random_image,
	const uint32_t* __restrict__ n_contrib,
	const uint32_t* __restrict__ t_contrib,
	const float* __restrict__ out_color,
	const float* __restrict__ out_depth,
	const float* __restrict__ out_alpha,
	const float* __restrict__ grad_out_colors,
	const float* __restrict__ grad_out_depths,
	const float* __restrict__ grad_out_alphas,
    float* __restrict__ grad_decoupolys_V,
	float* __restrict__ grad_decoupolys_g,
	float* __restrict__ grad_densities,
	float* __restrict__ grad_colors,
    float* __restrict__ aux_grad_colors2,
    float* __restrict__ aux_contributions
) {
    // We rasterize again. Compute necessary block info.
	auto block = cg::this_thread_block();
	const uint32_t horizontal_blocks = (W + BLOCK_X - 1) / BLOCK_X;
	const uint2 pix_min = { block.group_index().x * BLOCK_X, block.group_index().y * BLOCK_Y };
	const uint2 pix = { pix_min.x + block.thread_index().x, pix_min.y + block.thread_index().y };
	const uint32_t pix_id = W * pix.y + pix.x;
	
	// Get ray direction and origin for this pixel.
	float3 ray_dir = getRayDir(pix, W, H, tan_fovx, tan_fovy, viewmatrix);

    const bool inside = pix.x < W&& pix.y < H;
	bool done = !inside;

	// Load start/end range of IDs to process in bit sorted list.
	uint2 range = ranges[block.group_index().y * horizontal_blocks + block.group_index().x];
	const int rounds = ((range.y - range.x + PREFETCH_BUFFER_SIZE - 1) / PREFETCH_BUFFER_SIZE);
	int toDo = range.y - range.x;

	// Allocate storage for shared memory
	__shared__ uint32_t collected_ids[PREFETCH_BUFFER_SIZE];
	__shared__ float3 collected_scales[PREFETCH_BUFFER_SIZE];
	__shared__ float3 collected_xyz[PREFETCH_BUFFER_SIZE];
	__shared__ float collected_decoupolys_V[PREFETCH_BUFFER_SIZE * DECOUPOLY_V_SIZE];
	__shared__ float collected_decoupolys_g[PREFETCH_BUFFER_SIZE * DECOUPOLY_G_SIZE];
	__shared__ float collected_densities[PREFETCH_BUFFER_SIZE * DECOUPOLY_RANK];
	__shared__ float collected_colors[PREFETCH_BUFFER_SIZE * DECOUPOLY_RANK * CHANNELS];
	#ifdef GRAD_SHARED_TO_GLOBAL
	__shared__ float grad_collected_decoupolys_V[PREFETCH_BUFFER_SIZE * DECOUPOLY_V_SIZE];
	for (int i = block.thread_rank(); i < PREFETCH_BUFFER_SIZE * DECOUPOLY_V_SIZE; i += BLOCK_SIZE)
		grad_collected_decoupolys_V[i] = 0;
	__shared__ float grad_collected_decoupolys_g[PREFETCH_BUFFER_SIZE * DECOUPOLY_G_SIZE];
	for (int i = block.thread_rank(); i < PREFETCH_BUFFER_SIZE * DECOUPOLY_G_SIZE; i += BLOCK_SIZE)
		grad_collected_decoupolys_g[i] = 0;
	__shared__ float grad_collected_densities[PREFETCH_BUFFER_SIZE * DECOUPOLY_RANK];
	for (int i = block.thread_rank(); i < PREFETCH_BUFFER_SIZE * DECOUPOLY_RANK; i += BLOCK_SIZE)
		grad_collected_densities[i] = 0;
	__shared__ float grad_collected_colors[PREFETCH_BUFFER_SIZE * DECOUPOLY_RANK * CHANNELS];
	for (int i = block.thread_rank(); i < PREFETCH_BUFFER_SIZE * DECOUPOLY_RANK * CHANNELS; i += BLOCK_SIZE)
		grad_collected_colors[i] = 0;
	#elif defined(GRAD_LOCAL_REDUCED_TO_GLOBAL)
		typedef cub::BlockReduce<float, BLOCK_SIZE> BlockReduce;
		__shared__ typename BlockReduce::TempStorage temp_storage;
		__shared__ float shared_grad_decoupolys_V[DECOUPOLY_V_SIZE];
		__shared__ float shared_grad_decoupolys_g[DECOUPOLY_G_SIZE];
		__shared__ float shared_grad_densities[DECOUPOLY_RANK];
		__shared__ float shared_grad_colors[DECOUPOLY_RANK * CHANNELS];
	#endif

    // Initialize helper variables
	float T = 1.0f;
	uint32_t contributor = 0;
	uint32_t last_contributor = inside ? n_contrib[pix_id] : 0;
	uint32_t last_t = inside ? t_contrib[pix_id] : 0;
	float C[CHANNELS] = { 0 };
	float D = 0;
	float final_T = 1.0f - out_alpha[pix_id];
	float final_C[CHANNELS];
	for (int i = 0; i < CHANNELS; i++)
		final_C[i] = out_color[i * H * W + pix_id];
	float final_D = out_depth[pix_id];
	float jitter = random_image[pix_id];
	float dL_dout_color[CHANNELS];
	float dL_dout_depth;
	float dL_dout_alpha;
	if (inside) {
		if (grad_out_colors != nullptr) {
			for (int i = 0; i < CHANNELS; i++)
				dL_dout_color[i] = grad_out_colors[i * H * W + pix_id];
		}
		if (grad_out_depths != nullptr)
			dL_dout_depth = grad_out_depths[pix_id];
		if (grad_out_alphas != nullptr)
			dL_dout_alpha = grad_out_alphas[pix_id];
	}

	// Traverse all voxels
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
					collected_scales[j] = { aabb[3] * nsize, aabb[4] * nsize, aabb[5] * nsize };
					collected_xyz[j] = {
						positions[3 * coll_id] * aabb[3] + aabb[0],
						positions[3 * coll_id + 1] * aabb[4] + aabb[1],
						positions[3 * coll_id + 2] * aabb[5] + aabb[2]
					};
				}
				for (int ch = block.thread_rank(); ch < DECOUPOLY_V_SIZE; ch += BLOCK_SIZE)
					collected_decoupolys_V[j * DECOUPOLY_V_SIZE + ch] = decoupolys_V[coll_id * DECOUPOLY_V_SIZE + ch];
				for (int ch = block.thread_rank(); ch < DECOUPOLY_G_SIZE; ch += BLOCK_SIZE)
					collected_decoupolys_g[j * DECOUPOLY_G_SIZE + ch] = decoupolys_g[coll_id * DECOUPOLY_G_SIZE + ch];
				for (int ch = block.thread_rank(); ch < DECOUPOLY_RANK; ch += BLOCK_SIZE)
					collected_densities[j * DECOUPOLY_RANK + ch] = densities[coll_id * DECOUPOLY_RANK + ch];
				for (int ch = block.thread_rank(); ch < DECOUPOLY_RANK * CHANNELS; ch += BLOCK_SIZE)
					collected_colors[j * DECOUPOLY_RANK * CHANNELS + ch] = colors[coll_id * DECOUPOLY_RANK * CHANNELS + ch];
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
				cg::memcpy_async(block, collected_decoupolys_V + j * DECOUPOLY_V_SIZE, decoupolys_V + coll_id * DECOUPOLY_V_SIZE, DECOUPOLY_V_SIZE * sizeof(float));
				cg::memcpy_async(block, collected_decoupolys_g + j * DECOUPOLY_G_SIZE, decoupolys_g + coll_id * DECOUPOLY_G_SIZE, DECOUPOLY_G_SIZE * sizeof(float));
				cg::memcpy_async(block, collected_densities + j * DECOUPOLY_RANK, densities + coll_id * DECOUPOLY_RANK, DECOUPOLY_RANK * sizeof(float));
				cg::memcpy_async(block, collected_colors + j * DECOUPOLY_RANK * CHANNELS, colors + coll_id * DECOUPOLY_RANK * CHANNELS, DECOUPOLY_RANK * CHANNELS * sizeof(float));
			}
		}
		cg::wait(block);
		block.sync();
		#endif	

		// Iterate over current batch
		for (int j = 0; j < min(PREFETCH_BUFFER_SIZE, toDo); j++)
		{
			contributor++;
			if (contributor > last_contributor)
				done = true;

			#ifdef GRAD_LOCAL_REDUCED_TO_GLOBAL
			// End if entire block votes that it is done rasterizing
			int num_done = __syncthreads_count(done);
			if (num_done == BLOCK_SIZE)
				break;
			#else
			// End if each thread is done
			if (done)
				break;
			#endif

			// Get ray-voxel intersection
			float3 p = collected_xyz[j];
			float3 scale = collected_scales[j];
			float3 voxel_min = { p.x - 0.5f * scale.x, p.y - 0.5f * scale.y, p.z - 0.5f * scale.z };
			float3 voxel_max = { p.x + 0.5f * scale.x, p.y + 0.5f * scale.y, p.z + 0.5f * scale.z };
			float2 itsc = get_ray_voxel_intersection(*cam_pos, ray_dir, voxel_min, voxel_max);

			// Ray marching
			#if defined(GRAD_LOCAL_TO_GLOBAL) || defined(GRAD_LOCAL_REDUCED_TO_GLOBAL)
			float local_grad_decoupolys_V[DECOUPOLY_V_SIZE] = { 0 };
			float local_grad_decoupolys_g[DECOUPOLY_G_SIZE] = { 0 };
			float local_grad_densities[DECOUPOLY_RANK] = { 0 };
			float local_grad_colors[DECOUPOLY_RANK * CHANNELS] = { 0 };
			#endif
			float step = (0.5f / SAMPLE_DIM) * scale.x;
			int t_start = (int)ceil(itsc.x / step - jitter);
			int t_end = (int)floor(itsc.y / step - jitter);
			float w_sum = 0.0f;
			for (int t = t_start; t <= t_end; t++) {
				if (t > last_t)
					break;

				float z = (t + jitter) * step;

				// Sample decoupoly
				float density = 0;
				float color[CHANNELS] = { 0 };
				p = { cam_pos->x + z * ray_dir.x, cam_pos->y + z * ray_dir.y, cam_pos->z + z * ray_dir.z };
				sample_decoupoly(
					collected_decoupolys_V + j * DECOUPOLY_V_SIZE,
					collected_decoupolys_g + j * DECOUPOLY_G_SIZE,
					collected_densities + j * DECOUPOLY_RANK,
					collected_colors + j * DECOUPOLY_RANK * CHANNELS,
					density_shift,
					used_rank,
					p, voxel_min, voxel_max,
					density, color
				);

				// Accumulate
				float alpha = min(1 - exp(-density * step), 0.999f);
				const float weight = alpha * T;
				w_sum += weight;
				for (int k = 0; k < CHANNELS; k++)
					C[k] += color[k] * weight;
				D += z * weight;

				T *= 1 - alpha;

				// Residual
				float residual_T = final_T / T;
				float residual_C[CHANNELS];
				for (int i = 0; i < CHANNELS; i++)
					residual_C[i] = (final_C[i] - C[i]) / T;
				float residual_D = (final_D - D) / T;

				// Propagate gradients
				float dL_dalpha = 0.0f;
				float dL_dcolor[CHANNELS] = { 0 };
				// color
				if (grad_out_colors != nullptr) {
					for (int i = 0; i < CHANNELS; i++) {
						dL_dalpha += (color[i] - residual_C[i]) * dL_dout_color[i];
						dL_dcolor[i] = weight * dL_dout_color[i];
					}
				}
				// depth
				if (grad_out_depths != nullptr)
					dL_dalpha += (z - residual_D) * dL_dout_depth;
				// alpha
				if (grad_out_alphas != nullptr)
					dL_dalpha += residual_T * dL_dout_alpha;
				dL_dalpha *= T / (1.0f - alpha);
				float dL_ddensity = dL_dalpha * step * (1 - alpha);

				// Propagate gradients to trivec
				#if defined(GRAD_GLOBAL)
				sample_decoupoly_backward(
					collected_decoupolys_V + j * DECOUPOLY_V_SIZE,
					collected_decoupolys_g + j * DECOUPOLY_G_SIZE,
					collected_densities + j * DECOUPOLY_RANK,
					collected_colors + j * DECOUPOLY_RANK * CHANNELS,
					density_shift,
					used_rank,
					density, color,
					dL_ddensity, dL_dcolor,
					p, voxel_min, voxel_max,
					grad_decoupolys_V + collected_ids[j] * DECOUPOLY_V_SIZE,
					grad_decoupolys_g + collected_ids[j] * DECOUPOLY_G_SIZE,
					grad_densities + collected_ids[j] * DECOUPOLY_RANK,
					grad_colors + collected_ids[j] * DECOUPOLY_RANK * CHANNELS
				);
				#elif defined(GRAD_SHARED_TO_GLOBAL)
				sample_decoupoly_backward(
					collected_decoupolys_V + j * DECOUPOLY_V_SIZE,
					collected_decoupolys_g + j * DECOUPOLY_G_SIZE,
					collected_densities + j * DECOUPOLY_RANK,
					collected_colors + j * DECOUPOLY_RANK * CHANNELS,
					density_shift,
					used_rank,
					density, color,
					dL_ddensity, dL_dcolor,
					p, voxel_min, voxel_max,
					grad_collected_decoupolys_V + j * DECOUPOLY_V_SIZE,
					grad_collected_decoupolys_g + j * DECOUPOLY_G_SIZE,
					grad_collected_densities + j * DECOUPOLY_RANK,
					grad_collected_colors + j * DECOUPOLY_RANK * CHANNELS
				);
				#elif defined(GRAD_LOCAL_TO_GLOBAL) || defined(GRAD_LOCAL_REDUCED_TO_GLOBAL)
				sample_decoupoly_backward_local(
					collected_decoupolys_V + j * DECOUPOLY_V_SIZE,
					collected_decoupolys_g + j * DECOUPOLY_G_SIZE,
					collected_densities + j * DECOUPOLY_RANK,
					collected_colors + j * DECOUPOLY_RANK * CHANNELS,
					density_shift,
					used_rank,
					density, color,
					dL_ddensity, dL_dcolor,
					p, voxel_min, voxel_max,
					local_grad_decoupolys_V, local_grad_decoupolys_g, local_grad_densities, local_grad_colors
				);
				#endif

				if (T < 0.001f)
					break;
			}

			// Copy gradients to global memory
			#if defined(GRAD_LOCAL_TO_GLOBAL)
			for (int ch = 0; ch < DECOUPOLY_V_SIZE; ch++) {
				// shuffle the atomic adds to avoid conflicts
				int _ch = (ch + block.thread_rank()) % DECOUPOLY_V_SIZE;
				atomicAdd(grad_decoupolys_V + collected_ids[j] * DECOUPOLY_V_SIZE + _ch, local_grad_decoupolys_V[_ch]);
			}
			for (int ch = 0; ch < DECOUPOLY_G_SIZE; ch++) {
				// shuffle the atomic adds to avoid conflicts
				int _ch = (ch + block.thread_rank()) % DECOUPOLY_G_SIZE;
				atomicAdd(grad_decoupolys_g + collected_ids[j] * DECOUPOLY_G_SIZE + _ch, local_grad_decoupolys_g[_ch]);
			}
			for (int ch = 0; ch < DECOUPOLY_RANK; ch++) {
				// shuffle the atomic adds to avoid conflicts
				int _ch = (ch + block.thread_rank()) % DECOUPOLY_RANK;
				atomicAdd(grad_densities + collected_ids[j] * DECOUPOLY_RANK + _ch, local_grad_densities[_ch]);
			}
			for (int ch = 0; ch < DECOUPOLY_RANK * CHANNELS; ch++) {
				// shuffle the atomic adds to avoid conflicts
				int _ch = (ch + block.thread_rank()) % (DECOUPOLY_RANK * CHANNELS);
				atomicAdd(grad_colors + collected_ids[j] * DECOUPOLY_RANK * CHANNELS + _ch, local_grad_colors[_ch]);
			}
			#elif defined(GRAD_LOCAL_REDUCED_TO_GLOBAL)
			for (int ch = 0; ch < DECOUPOLY_V_SIZE; ch++) {
				// reduce
				float aggregated_grad = BlockReduce(temp_storage).Sum(local_grad_decoupolys_V[ch]);
				block.sync();
				if (block.thread_rank() == 0)
					shared_grad_decoupolys_V[ch] = aggregated_grad;
			}
			block.sync();
			for (int ch = block.thread_rank(); ch < DECOUPOLY_V_SIZE; ch += BLOCK_SIZE)
				atomicAdd(grad_decoupolys_V + collected_ids[j] * DECOUPOLY_V_SIZE + ch, shared_grad_decoupolys_V[ch]);
			for (int ch = 0; ch < DECOUPOLY_G_SIZE; ch++) {
				// reduce
				float aggregated_grad = BlockReduce(temp_storage).Sum(local_grad_decoupolys_g[ch]);
				block.sync();
				if (block.thread_rank() == 0)
					shared_grad_decoupolys_g[ch] = aggregated_grad;
			}
			block.sync();
			for (int ch = block.thread_rank(); ch < DECOUPOLY_G_SIZE; ch += BLOCK_SIZE)
				atomicAdd(grad_decoupolys_g + collected_ids[j] * DECOUPOLY_G_SIZE + ch, shared_grad_decoupolys_g[ch]);
			for (int ch = 0; ch < DECOUPOLY_RANK; ch++) {
				// reduce
				float aggregated_grad = BlockReduce(temp_storage).Sum(local_grad_densities[ch]);
				block.sync();
				if (block.thread_rank() == 0)
					shared_grad_densities[ch] = aggregated_grad;
			}
			block.sync();
			for (int ch = block.thread_rank(); ch < DECOUPOLY_RANK; ch += BLOCK_SIZE)
				atomicAdd(grad_densities + collected_ids[j] * DECOUPOLY_RANK + ch, shared_grad_densities[ch]);
			for (int ch = 0; ch < DECOUPOLY_RANK * CHANNELS; ch++) {
				// reduce
				float aggregated_grad = BlockReduce(temp_storage).Sum(local_grad_colors[ch]);
				block.sync();
				if (block.thread_rank() == 0)
					shared_grad_colors[ch] = aggregated_grad;
			}
			block.sync();
			for (int ch = block.thread_rank(); ch < DECOUPOLY_RANK * CHANNELS; ch += BLOCK_SIZE)
				atomicAdd(grad_colors + collected_ids[j] * DECOUPOLY_RANK * CHANNELS + ch, shared_grad_colors[ch]);
			#endif

			if (aux_grad_colors2 != nullptr) {
				for (int ch = 0; ch < CHANNELS; ch++)
					atomicMax((int*)aux_grad_colors2 + collected_ids[j] * CHANNELS + ch, __float_as_int(w_sum * dL_dout_color[ch] * dL_dout_color[ch]));
			}
			if (aux_contributions != nullptr)
				atomicMax((int*)aux_contributions + collected_ids[j], __float_as_int(w_sum));

			// If we have accumulated enough, we can stop
			if (T < 0.001f)
				done = true;
		}

		// Copy gradients to global memory
		#if defined(GRAD_SHARED_TO_GLOBAL)
		for (int j = 0; j < min(PREFETCH_BUFFER_SIZE, toDo); j++)
			for (int ch = block.thread_rank(); ch < DECOUPOLY_V_SIZE; ch += BLOCK_SIZE)
				atomicAdd(grad_decoupolys_V + collected_ids[j] * DECOUPOLY_V_SIZE + ch, grad_collected_decoupolys_V[j * DECOUPOLY_V_SIZE + ch]);
		for (int j = 0; j < min(PREFETCH_BUFFER_SIZE, toDo); j++)
			for (int ch = block.thread_rank(); ch < DECOUPOLY_G_SIZE; ch += BLOCK_SIZE)
				atomicAdd(grad_decoupolys_g + collected_ids[j] * DECOUPOLY_G_SIZE + ch, grad_collected_decoupolys_g[j * DECOUPOLY_G_SIZE + ch]);
		for (int j = 0; j < min(PREFETCH_BUFFER_SIZE, toDo); j++)
			for (int ch = block.thread_rank(); ch < DECOUPOLY_RANK; ch += BLOCK_SIZE)
				atomicAdd(grad_densities + collected_ids[j] * DECOUPOLY_RANK + ch, grad_collected_densities[j * DECOUPOLY_RANK + ch]);
		for (int j = 0; j < min(PREFETCH_BUFFER_SIZE, toDo); j++)
			for (int ch = block.thread_rank(); ch < DECOUPOLY_RANK * CHANNELS; ch += BLOCK_SIZE)
				atomicAdd(grad_colors + collected_ids[j] * DECOUPOLY_RANK * CHANNELS + ch, grad_collected_colors[j * DECOUPOLY_RANK * CHANNELS + ch]);
		#endif
	}
}


void OctreeDecoupolyRasterizer::CUDA::backward(
    const int num_nodes,
    const int active_sh_degree,
    const int num_sh_coefs,
    const int num_rendered,
    const float* background,
    const int width,
    const int height,
    const float* aabb,
    const float* positions,
    const float* decoupolys_V,
	const float* decoupolys_g,
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
    char* geom_buffer,
	char* binning_buffer,
	char* img_buffer,
    const float* out_color,
    const float* out_depth,
    const float* out_alpha,
    const float* grad_out_color,
    const float* grad_out_depth,
    const float* grad_out_alpha,
    float* grad_decoupolys_V,
	float* grad_decoupolys_g,
	float* grad_densities,
    float* grad_shs,
    float* grad_colors,
    float* aux_grad_colors2,
    float* aux_contributions
) {
	// Parrallel config (2D grid of 2D blocks)
    dim3 grid((width + BLOCK_X - 1) / BLOCK_X, (height + BLOCK_Y - 1) / BLOCK_Y);
    dim3 block(BLOCK_X, BLOCK_Y);

    // Recover buffers
    GeometryState geomState = GeometryState::fromChunk(geom_buffer, num_nodes);
	BinningState binningState = BinningState::fromChunk(binning_buffer, num_rendered);
	ImageState imgState = ImageState::fromChunk(img_buffer, width * height);

	const float* color_ptr = (shs) ? geomState.colors : colors;
    CHECK_CUDA(renderBackward<<<grid, block>>>(
        imgState.ranges, binningState.point_list,
		width, height, background,
		(float3*)cam_pos, tan_fovx, tan_fovy, viewmatrix, aabb,
		positions, decoupolys_V, decoupolys_g, densities, density_shift, color_ptr, used_rank, depths, scale_modifier, random_image,
		imgState.n_contrib, imgState.t_contrib, out_color, out_depth, out_alpha,
		grad_out_color, grad_out_depth, grad_out_alpha,
        grad_decoupolys_V, grad_decoupolys_g, grad_densities, grad_colors, aux_grad_colors2, aux_contributions
    ));

	CHECK_CUDA(preprocessBackward<<<(num_nodes+255)/256, 256>>>(
		num_nodes, active_sh_degree, num_sh_coefs,
        positions, shs,
        (glm::vec3*)cam_pos, aabb,
        grad_colors, grad_shs
	));
}