/*
 * Sparse Voxel Octree (SVO) Rasterizer
 *
 * Copyright (C) 2024, Jianfeng XIANG <belljig@outlook.com>
 * All rights reserved.
 *
 * Licensed under The MIT License [see LICENSE for details]
 *
 * Written by Jianfeng XIANG
 */

#include <torch/extension.h>
#include <tuple>
#include <functional>
#include "api.h"
#include "cuda/api.h"


static std::function<char*(size_t N)> resizeFunctional(torch::Tensor& t) {
    auto lambda = [&t](size_t N) {
        t.resize_({(long long)N});
		return reinterpret_cast<char*>(t.contiguous().data_ptr());
    };
    return lambda;
}


std::tuple<int, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor>
OctreeVoxelRasterizer::rasterizeOctreeVoxelsCUDA(
	const torch::Tensor& background,
	const torch::Tensor& positions,
	const torch::Tensor& colors,
	const torch::Tensor& densities,
	const torch::Tensor& depths,
	const float scale_modifier,
	const torch::Tensor& viewmatrix,
	const torch::Tensor& projmatrix,
	const float tan_fovx,
    const float tan_fovy,
	const int image_height,
	const int image_width,
	const torch::Tensor& sh,
	const int degree,
	const torch::Tensor& campos,
	const torch::Tensor& aabb,
	const bool with_distloss
) {
	// Sizes
	const int P = positions.size(0);
	const int H = image_height;
	const int W = image_width;

	// Types
	torch::TensorOptions float_opts = positions.options().dtype(torch::kFloat32);
	torch::TensorOptions byte_opts(torch::kByte);
	byte_opts = byte_opts.device(positions.device());

	// Allocate output tensors
	torch::Tensor out_color = torch::zeros({NUM_CHANNELS, H, W}, float_opts);
	torch::Tensor out_depth = torch::zeros({H, W}, float_opts);
	torch::Tensor out_alpha = torch::zeros({H, W}, float_opts);
	torch::Tensor out_distloss = with_distloss ? torch::zeros({H, W}, float_opts) : torch::empty({0}, float_opts);

	// Allocate temporary tensors
	torch::Tensor geomBuffer = torch::empty({0}, byte_opts);
	torch::Tensor binningBuffer = torch::empty({0}, byte_opts);
	torch::Tensor imgBuffer = torch::empty({0}, byte_opts);
	std::function<char*(size_t)> geomFunc = resizeFunctional(geomBuffer);
	std::function<char*(size_t)> binningFunc = resizeFunctional(binningBuffer);
	std::function<char*(size_t)> imgFunc = resizeFunctional(imgBuffer);

	// Call Forward
	int rendered = 0;
	if (P > 0) {
		int num_sh_coefs = sh.size(0) == 0 ? 0 : sh.size(1);
		rendered = OctreeVoxelRasterizer::CUDA::forward(
			geomFunc, binningFunc, imgFunc,
			P, degree, num_sh_coefs,
			background.contiguous().data_ptr<float>(),
			W, H, aabb.contiguous().data_ptr<float>(),
			positions.contiguous().data_ptr<float>(),
			sh.contiguous().data_ptr<float>(),
			colors.contiguous().data_ptr<float>(),
			densities.contiguous().data_ptr<float>(),
			depths.contiguous().data_ptr<uint8_t>(),
			scale_modifier,
			viewmatrix.contiguous().data_ptr<float>(),
			projmatrix.contiguous().data_ptr<float>(),
			campos.contiguous().data_ptr<float>(),
			tan_fovx, tan_fovy,
			out_color.contiguous().data_ptr<float>(),
			out_depth.contiguous().data_ptr<float>(),
			out_alpha.contiguous().data_ptr<float>(),
			out_distloss.contiguous().data_ptr<float>()
		);
	}

	return std::make_tuple(
		rendered, out_color, out_depth, out_alpha, out_distloss,
		geomBuffer, binningBuffer, imgBuffer
	);
}


std::tuple<torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor>
OctreeVoxelRasterizer::rasterizeOctreeVoxelsBackwardCUDA(
	const torch::Tensor& background,
	const torch::Tensor& positions,
	const torch::Tensor& colors,
	const torch::Tensor& densities,
	const torch::Tensor& depths,
	const float scale_modifier,
	const torch::Tensor& viewmatrix,
	const torch::Tensor& projmatrix,
	const float tan_fovx,
    const float tan_fovy,
	const torch::Tensor& grad_out_color,
	const torch::Tensor& grad_out_depth,
	const torch::Tensor& grad_out_alpha,
	const torch::Tensor& grad_out_distloss,
	const torch::Tensor& shs,
	const int degree,
	const torch::Tensor& campos,
	const torch::Tensor& aabb,
	const torch::Tensor& geomBuffer,
	const int num_rendered,
	const torch::Tensor& binningBuffer,
	const torch::Tensor& imageBuffer,
	const bool with_auxiliary
) {
	// Sizes
	const int P = positions.size(0);
	const int H = grad_out_color.size(1);
	const int W = grad_out_color.size(2);
	const int M = shs.size(0) == 0 ? 0 : shs.size(1);

	// Allocate output tensors
	torch::Tensor grad_shs = torch::zeros({P, M, 3}, positions.options());
	torch::Tensor grad_colors = torch::zeros({P, NUM_CHANNELS}, positions.options());
	torch::Tensor grad_densities = torch::zeros({P, 1}, positions.options());
	torch::Tensor aux_grad_colors2 = with_auxiliary ? torch::zeros({P, NUM_CHANNELS}, positions.options()) : torch::empty({0}, positions.options());
	torch::Tensor aux_contributions = with_auxiliary ? torch::zeros({P, 1}, positions.options()) : torch::empty({0}, positions.options());

	// Call Backward
	if (P > 0) {
		OctreeVoxelRasterizer::CUDA::backward(
			P, degree, M, num_rendered,
			background.contiguous().data_ptr<float>(),
			W, H, aabb.contiguous().data_ptr<float>(),
			positions.contiguous().data_ptr<float>(),
			shs.contiguous().data_ptr<float>(),
			colors.contiguous().data_ptr<float>(),
			densities.contiguous().data_ptr<float>(),
			depths.contiguous().data_ptr<uint8_t>(),
			scale_modifier,
			viewmatrix.contiguous().data_ptr<float>(),
			projmatrix.contiguous().data_ptr<float>(),
			campos.contiguous().data_ptr<float>(),
			tan_fovx, tan_fovy,
			reinterpret_cast<char*>(geomBuffer.contiguous().data_ptr()),
			reinterpret_cast<char*>(binningBuffer.contiguous().data_ptr()),
			reinterpret_cast<char*>(imageBuffer.contiguous().data_ptr()),
			grad_out_color.contiguous().data_ptr<float>(),
			grad_out_depth.contiguous().data_ptr<float>(),
			grad_out_alpha.contiguous().data_ptr<float>(),
			grad_out_distloss.contiguous().data_ptr<float>(),
			grad_shs.contiguous().data_ptr<float>(),
			grad_colors.contiguous().data_ptr<float>(),
			grad_densities.contiguous().data_ptr<float>(),
			aux_grad_colors2.contiguous().data_ptr<float>(),
			aux_contributions.contiguous().data_ptr<float>()
		);
	}

	return std::make_tuple(
		grad_shs, grad_colors, grad_densities, aux_grad_colors2, aux_contributions
	);
}
