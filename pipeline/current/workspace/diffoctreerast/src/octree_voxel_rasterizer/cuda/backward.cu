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
 * Backward pass for converting the input spherical harmonics coefficients of each voxel to a simple RGB color.
 * 
 * @param idx Index of the point in the input array.
 * @param deg Degree of the spherical harmonics coefficients.
 * @param max_coeffs Maximum number of coefficients.
 * @param pos Position of the point.
 * @param campos Camera position.
 * @param shs Array of spherical harmonics coefficients.
 * @param clamped Array of booleans to store if the color was clamped.
 * @param dL_dcolors Gradient of the output colors.
 * @param dL_dshs Gradient of the input spherical harmonics coefficients.
 */
static __device__ void computeColorFromSHBackward(int idx, int deg, int max_coeffs, const glm::vec3 pos, glm::vec3 campos, const float* shs, const bool* clamped, const glm::vec3* dL_dcolors, glm::vec3* dL_dshs) {
	// Compute intermediate values, as it is done during forward
	glm::vec3 dir_orig = pos - campos;
	glm::vec3 dir = dir_orig / glm::length(dir_orig);

	glm::vec3* sh = ((glm::vec3*)shs) + idx * max_coeffs;

	// Use PyTorch rule for clamping: if clamping was applied,
	// gradient becomes 0.
	glm::vec3 dL_dRGB = dL_dcolors[idx];
	// MODIFIED: do not clamp the gradient
	// dL_dRGB.x *= clamped[3 * idx + 0] ? 0 : 1;
	// dL_dRGB.y *= clamped[3 * idx + 1] ? 0 : 1;
	// dL_dRGB.z *= clamped[3 * idx + 2] ? 0 : 1;

	glm::vec3 dRGBdx(0, 0, 0);
	glm::vec3 dRGBdy(0, 0, 0);
	glm::vec3 dRGBdz(0, 0, 0);
	float x = dir.x;
	float y = dir.y;
	float z = dir.z;

	// Target location for this voxel to write SH gradients to
	glm::vec3* dL_dsh = dL_dshs + idx * max_coeffs;

	// No tricks here, just high school-level calculus.
	float dRGBdsh0 = SH_C0;
	dL_dsh[0] = dRGBdsh0 * dL_dRGB;
	if (deg > 0)
	{
		float dRGBdsh1 = -SH_C1 * y;
		float dRGBdsh2 = SH_C1 * z;
		float dRGBdsh3 = -SH_C1 * x;
		dL_dsh[1] = dRGBdsh1 * dL_dRGB;
		dL_dsh[2] = dRGBdsh2 * dL_dRGB;
		dL_dsh[3] = dRGBdsh3 * dL_dRGB;

		dRGBdx = -SH_C1 * sh[3];
		dRGBdy = -SH_C1 * sh[1];
		dRGBdz = SH_C1 * sh[2];

		if (deg > 1)
		{
			float xx = x * x, yy = y * y, zz = z * z;
			float xy = x * y, yz = y * z, xz = x * z;

			float dRGBdsh4 = SH_C2[0] * xy;
			float dRGBdsh5 = SH_C2[1] * yz;
			float dRGBdsh6 = SH_C2[2] * (2.f * zz - xx - yy);
			float dRGBdsh7 = SH_C2[3] * xz;
			float dRGBdsh8 = SH_C2[4] * (xx - yy);
			dL_dsh[4] = dRGBdsh4 * dL_dRGB;
			dL_dsh[5] = dRGBdsh5 * dL_dRGB;
			dL_dsh[6] = dRGBdsh6 * dL_dRGB;
			dL_dsh[7] = dRGBdsh7 * dL_dRGB;
			dL_dsh[8] = dRGBdsh8 * dL_dRGB;

			dRGBdx += SH_C2[0] * y * sh[4] + SH_C2[2] * 2.f * -x * sh[6] + SH_C2[3] * z * sh[7] + SH_C2[4] * 2.f * x * sh[8];
			dRGBdy += SH_C2[0] * x * sh[4] + SH_C2[1] * z * sh[5] + SH_C2[2] * 2.f * -y * sh[6] + SH_C2[4] * 2.f * -y * sh[8];
			dRGBdz += SH_C2[1] * y * sh[5] + SH_C2[2] * 2.f * 2.f * z * sh[6] + SH_C2[3] * x * sh[7];

			if (deg > 2)
			{
				float dRGBdsh9 = SH_C3[0] * y * (3.f * xx - yy);
				float dRGBdsh10 = SH_C3[1] * xy * z;
				float dRGBdsh11 = SH_C3[2] * y * (4.f * zz - xx - yy);
				float dRGBdsh12 = SH_C3[3] * z * (2.f * zz - 3.f * xx - 3.f * yy);
				float dRGBdsh13 = SH_C3[4] * x * (4.f * zz - xx - yy);
				float dRGBdsh14 = SH_C3[5] * z * (xx - yy);
				float dRGBdsh15 = SH_C3[6] * x * (xx - 3.f * yy);
				dL_dsh[9] = dRGBdsh9 * dL_dRGB;
				dL_dsh[10] = dRGBdsh10 * dL_dRGB;
				dL_dsh[11] = dRGBdsh11 * dL_dRGB;
				dL_dsh[12] = dRGBdsh12 * dL_dRGB;
				dL_dsh[13] = dRGBdsh13 * dL_dRGB;
				dL_dsh[14] = dRGBdsh14 * dL_dRGB;
				dL_dsh[15] = dRGBdsh15 * dL_dRGB;

				dRGBdx += (
					SH_C3[0] * sh[9] * 3.f * 2.f * xy +
					SH_C3[1] * sh[10] * yz +
					SH_C3[2] * sh[11] * -2.f * xy +
					SH_C3[3] * sh[12] * -3.f * 2.f * xz +
					SH_C3[4] * sh[13] * (-3.f * xx + 4.f * zz - yy) +
					SH_C3[5] * sh[14] * 2.f * xz +
					SH_C3[6] * sh[15] * 3.f * (xx - yy));

				dRGBdy += (
					SH_C3[0] * sh[9] * 3.f * (xx - yy) +
					SH_C3[1] * sh[10] * xz +
					SH_C3[2] * sh[11] * (-3.f * yy + 4.f * zz - xx) +
					SH_C3[3] * sh[12] * -3.f * 2.f * yz +
					SH_C3[4] * sh[13] * -2.f * xy +
					SH_C3[5] * sh[14] * -2.f * yz +
					SH_C3[6] * sh[15] * -3.f * 2.f * xy);

				dRGBdz += (
					SH_C3[1] * sh[10] * xy +
					SH_C3[2] * sh[11] * 4.f * 2.f * yz +
					SH_C3[3] * sh[12] * 3.f * (2.f * zz - xx - yy) +
					SH_C3[4] * sh[13] * 4.f * 2.f * xz +
					SH_C3[5] * sh[14] * (xx - yy));
			}
		}
	}
}


/**
 * Backward pass of the preprocessing steps
 */
static __global__ void preprocessBackward(
	const int num_nodes,
	const int active_sh_degree,
	const int num_sh_coefs,
	const float* positions,
	const float scale_modifier,
	const float* shs,
	const bool* clamped,
	const float* view,
	const float* proj,
	const glm::vec3* cam_pos,
	const float* aabb,
    float* grad_colors,
    float* grad_shs
) {
	auto idx = cg::this_grid().thread_rank();
	if (idx >= num_nodes)
		return;

	float3 p_orig = {
		positions[3 * idx] * aabb[3] + aabb[0],
		positions[3 * idx + 1] * aabb[4] + aabb[1],
		positions[3 * idx + 2] * aabb[5] + aabb[2]
	};

	// Compute gradient updates due to computing colors from SHs
	if (shs)
		computeColorFromSHBackward(
            idx, active_sh_degree, num_sh_coefs,
            *(glm::vec3*)&p_orig, *cam_pos, shs, clamped,
            (glm::vec3*)grad_colors, (glm::vec3*)grad_shs);
}



/**
 * Backward version of the rendering procedure.
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
 * @param positions Positions of octree nodes.
 * @param colors Colors of octree nodes.
 * @param depths Depths of octree nodes.
 * @param tree_depths Depths of octree nodes.
 * @param scale_modifier Scale modifier.
 * @param densities densities of octree nodes.
 * @param final_T Final T.
 * @param final_wm_sum Final weighted midpoints sum.
 * @param n_contrib Number of contributors.
 * @param grad_out_colors Gradient of output colors.
 * @param grad_out_depths Gradient of output depths.
 * @param grad_out_alphas Gradient of output alphas.
 * @param grad_out_distlosses Gradient of output distance loss.
 * @param grad_colors Gradient of colors.
 * @param grad_densities Gradient of densities.
 * @param aux_grad_colors2 Auxiliary gradient of squared colors.
 * @param aux_contributions Auxiliary contributions.
 */
template <uint32_t CHANNELS>
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
	const float* __restrict__ colors,
	const float* __restrict__ depths,
	const uint8_t* __restrict__ tree_depths,
	const float scale_modifier,
	const float* __restrict__ densities,
	const float* __restrict__ final_Ts,
	const float* __restrict__ final_wm_sum,
	const uint32_t* __restrict__ n_contrib,
	const float* __restrict__ grad_out_colors,
	const float* __restrict__ grad_out_depths,
	const float* __restrict__ grad_out_alphas,
	const float* __restrict__ grad_out_distlosses,
    float* __restrict__ grad_colors,
    float* __restrict__ grad_densities,
    float* __restrict__ aux_grad_colors2,
    float* __restrict__ aux_contributions
) {
    // We rasterize again. Compute necessary block info.
	auto block = cg::this_thread_block();
	const uint32_t horizontal_blocks = (W + BLOCK_X - 1) / BLOCK_X;
	const uint32_t block_idx = block.group_index().y * horizontal_blocks + block.group_index().x;
	const uint2 pix_min = { block.group_index().x * BLOCK_X, block.group_index().y * BLOCK_Y };
	const uint2 pix_max = { min(pix_min.x + BLOCK_X, W), min(pix_min.y + BLOCK_Y , H) };
	const uint2 pix = { pix_min.x + block.thread_index().x, pix_min.y + block.thread_index().y };
	const uint32_t pix_id = W * pix.y + pix.x;
	// Get ray direction and origin for this pixel.
	float3 ray_dir = getRayDir(pix, W, H, tan_fovx, tan_fovy, viewmatrix);

    const bool inside = pix.x < W&& pix.y < H;
	bool done = !inside;

	// Allocate storage for batches of collectively fetched data.
	__shared__ int collected_id[BLOCK_SIZE];
	__shared__ float3 collected_xyz[BLOCK_SIZE];
	__shared__ float3 collected_scales[BLOCK_SIZE];
	__shared__ float collected_densities[BLOCK_SIZE];
	__shared__ float collected_colors[CHANNELS * BLOCK_SIZE];
	__shared__ float collected_depths[BLOCK_SIZE];

    // In the forward, we stored the final value for T, the
	// product of all (1 - alpha) factors. 
	const float T_final = inside ? final_Ts[pix_id] : 0;
	float T = T_final;
	const float wm_sum = inside ? final_wm_sum[pix_id] : 0;
	float wm_prefix = wm_sum;

	// We start from the back. The ID of the last contributing
	// voxel is known from each pixel from the forward.
	const int last_contributor = inside ? n_contrib[pix_id] : 0;

    // Optimization: todo can be reduced to the maximum number of
    // voxels that can contribute to pixels in this block.
	#ifndef BACKWARD_BLOCK_TODO
	const uint2 range = ranges[block_idx];
	int toDo = range.y - range.x;
	#else
	typedef cub::BlockReduce<int, BLOCK_SIZE> BlockReduce;
    __shared__ typename BlockReduce::TempStorage temp_storage;
	__shared__ int aggregated_todo;
    int toDo = BlockReduce(temp_storage).Reduce(last_contributor, cub::Max());
	if (block.thread_rank() == 0)
		aggregated_todo = toDo;
	block.sync();
	toDo = aggregated_todo;
	const uint2 range = { ranges[block_idx].x, ranges[block_idx].x + toDo };
    #endif
	const int rounds = ((range.y - range.x + BLOCK_SIZE - 1) / BLOCK_SIZE);
	uint32_t contributor = toDo;

	float accum_color[CHANNELS] = { 0 };
	float accum_depth = 0;
	float accum_dout_distloss_dw = 0;
	float dL_dout_color[CHANNELS];
	float dL_dout_depth;
	float dL_dout_alpha;
	float dL_dout_distloss;
	if (inside) {
		for (int i = 0; i < CHANNELS; i++)
			dL_dout_color[i] = grad_out_colors[i * H * W + pix_id];
		if (grad_out_depths != nullptr)
			dL_dout_depth = grad_out_depths[pix_id];
		if (grad_out_alphas != nullptr)
			dL_dout_alpha = grad_out_alphas[pix_id];
		if (grad_out_distlosses != nullptr)
			dL_dout_distloss = grad_out_distlosses[pix_id];
	}

	// Traverse all voxels
	for (int i = 0; i < rounds; i++, toDo -= BLOCK_SIZE)
	{
		// Load auxiliary data into shared memory, start in the BACK
		// and load them in revers order.
		block.sync();
		const int progress = i * BLOCK_SIZE + block.thread_rank();
		if (range.x + progress < range.y)
		{
			const int coll_id = point_list[range.y - progress - 1];
			collected_id[block.thread_rank()] = coll_id;
			collected_xyz[block.thread_rank()] = {
				positions[3 * coll_id] * aabb[3] + aabb[0],
				positions[3 * coll_id + 1] * aabb[4] + aabb[1],
				positions[3 * coll_id + 2] * aabb[5] + aabb[2]
			};
			float nsize = powf(2.0f, -(float)tree_depths[coll_id]) * scale_modifier;
			collected_scales[block.thread_rank()] = {aabb[3] * nsize, aabb[4] * nsize, aabb[5] * nsize};
			collected_densities[block.thread_rank()] = densities[coll_id];
			for (int i = 0; i < CHANNELS; i++)
				collected_colors[i * BLOCK_SIZE + block.thread_rank()] = colors[coll_id * CHANNELS + i];
			if (grad_out_depths != nullptr)
				collected_depths[block.thread_rank()] = depths[coll_id];
		}
		block.sync();

		// Iterate over voxels
		for (int j = 0; !done && j < min(BLOCK_SIZE, toDo); j++)
		{
			// Keep track of current voxel ID. Skip, if this one
			// is behind the last contributor for this pixel.
			contributor--;
			if (contributor >= last_contributor)
				continue;

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

			// Propagate gradients to per-voxel colors and keep
			// gradients w.r.t. alpha (blending factor for a voxel/pixel
			// pair).
			float dL_dalpha = 0.0f;
			const int global_id = collected_id[j];
			if (grad_out_alphas != nullptr)
				dL_dalpha += T_final / T * dL_dout_alpha;
			T = T / (1.f - alpha);
			const float weight = alpha * T;
			if (aux_contributions != nullptr)
				atomicAdd(&(aux_contributions[global_id]), weight);
			for (int ch = 0; ch < CHANNELS; ch++)
			{
				const float c = collected_colors[ch * BLOCK_SIZE + j];
				const float dL_dchannel = dL_dout_color[ch];
				dL_dalpha += (c - accum_color[ch]) * dL_dchannel;
				accum_color[ch] = alpha * c + (1.f - alpha) * accum_color[ch];
				// Update the gradients w.r.t. color of the voxel. 
				// Atomic, since this pixel is just one of potentially
				// many that were affected by this voxel.
				atomicAdd(&(grad_colors[global_id * CHANNELS + ch]), weight * dL_dchannel);
				if (aux_grad_colors2 != nullptr)
					atomicAdd(&(aux_grad_colors2[global_id * CHANNELS + ch]), weight * dL_dchannel * dL_dchannel);
			}
			if (grad_out_depths != nullptr) {
				const float c_d = collected_depths[j];
				dL_dalpha += (c_d - accum_depth) * dL_dout_depth;
				accum_depth = alpha * c_d + (1.f - alpha) * accum_depth;
			}
			// Distortion loss
			// loss_bi := 2 * (wm * w_prefix - w * wm_prefix); loss_uni := 1.0f / 3.0f * (itsc_dist * w^2);
			if (grad_out_distlosses != nullptr)
			{
				float midpoint = 0.5f * (itsc.x + itsc.y);
				float wm = weight * midpoint;
				wm_prefix -= wm;
				float dout_distloss_dw = 2.0f * midpoint * ((1.0f - T) - (T - T_final - weight));	// bi term wm
				dout_distloss_dw += 2.0f * ((wm_sum - wm_prefix - wm) - wm_prefix);					// bi term w
				dout_distloss_dw += (2.0f / 3.0f) * itsc_dist * weight;								// uni term w
				dL_dalpha += (dout_distloss_dw - accum_dout_distloss_dw) * dL_dout_distloss;
				accum_dout_distloss_dw = alpha * dout_distloss_dw + (1.f - alpha) * accum_dout_distloss_dw;
			}
			dL_dalpha *= T;

			// Account for fact that alpha also influences how much of
			// the background color is added if nothing left to blend
			float bg_dot_dpixel = 0;
			for (int i = 0; i < CHANNELS; i++)
				bg_dot_dpixel += bg_color[i] * dL_dout_color[i];
			dL_dalpha += (-T_final / (1.f - alpha)) * bg_dot_dpixel;

			// Update gradients w.r.t. density of the voxel
			atomicAdd(&(grad_densities[global_id]), dL_dalpha * itsc_dist * (1 - alpha));
		}
	}
}


void OctreeVoxelRasterizer::CUDA::backward(
    const int num_nodes,
    const int active_sh_degree,
    const int num_sh_coefs,
    const int num_rendered,
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
    char* geom_buffer,
	char* binning_buffer,
	char* img_buffer,
    const float* grad_out_color,
    const float* grad_out_depth,
    const float* grad_out_alpha,
	const float* grad_out_distloss,
    float* grad_shs,
    float* grad_colors,
    float* grad_densities,
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

    const float focal_x = height / (2.f * tan_fovy);
	const float focal_y = width / (2.f * tan_fovx);

    const float* color_ptr = (colors_precomp != nullptr) ? colors_precomp : geomState.rgb;
    CHECK_CUDA(renderBackward<NUM_CHANNELS><<<grid, block>>>(
        imgState.ranges, binningState.point_list,
		width, height, background,
		(float3*)cam_pos, tan_fovx, tan_fovy, viewmatrix, aabb,
		positions, color_ptr, geomState.depths, depths, scale_modifier, densities,
		imgState.accum_alpha, imgState.wm_sum, imgState.n_contrib,
		grad_out_color, grad_out_depth, grad_out_alpha, grad_out_distloss,
        grad_colors, grad_densities, aux_grad_colors2, aux_contributions
    ));

    CHECK_CUDA(preprocessBackward<<<(num_nodes+255)/256, 256>>>(
        num_nodes, active_sh_degree, num_sh_coefs,
        positions, scale_modifier, shs, geomState.clamped,
        viewmatrix, projmatrix, (glm::vec3*)cam_pos, aabb,
        grad_colors, grad_shs
    ));
}