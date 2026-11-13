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
#include "cuda/config.h"


static std::function<char*(size_t N)> resizeFunctional(torch::Tensor& t) {
    auto lambda = [&t](size_t N) {
        t.resize_({(long long)N});
		return reinterpret_cast<char*>(t.contiguous().data_ptr());
    };
    return lambda;
}


std::tuple<int, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor>
OctreeDecoupolyRasterizer::rasterizeOctreeDecoupolysCUDA(
	const torch::Tensor& background,
	const torch::Tensor& positions,
	const torch::Tensor& decoupolys_V,
	const torch::Tensor& decoupolys_g,
	const torch::Tensor& densities,
	const float density_shift,
	const torch::Tensor& shs,
	const int degree,
	const torch::Tensor& colors,
	const int used_rank,
	const torch::Tensor& depths,
	const float scale_modifier,
	const torch::Tensor& viewmatrix,
	const torch::Tensor& projmatrix,
	const float tan_fovx,
    const float tan_fovy,
	const int image_height,
	const int image_width,
	const torch::Tensor& campos,
	const torch::Tensor& aabb,
	const torch::Tensor& random_image
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
	torch::Tensor out_color = torch::zeros({CHANNELS, H, W}, float_opts);
	torch::Tensor out_depth = torch::zeros({H, W}, float_opts);
	torch::Tensor out_alpha = torch::zeros({H, W}, float_opts);

	// Allocate temporary tensors
	torch::Tensor geomBuffer = torch::empty({0}, byte_opts);
	torch::Tensor binningBuffer = torch::empty({0}, byte_opts);
	torch::Tensor imgBuffer = torch::empty({0}, byte_opts);
	std::function<char*(size_t)> geomFunc = resizeFunctional(geomBuffer);
	std::function<char*(size_t)> binningFunc = resizeFunctional(binningBuffer);
	std::function<char*(size_t)> imgFunc = resizeFunctional(imgBuffer);

	// DEBUG
	// int dbg_ray_id = 241932;
	// torch::Tensor dbg_position = torch::zeros({16384, 3}, float_opts);
	// torch::Tensor dbg_density = torch::zeros({16384}, float_opts);
	// torch::Tensor dbg_color = torch::zeros({16384, 3}, float_opts);
	// torch::Tensor dbg_weight = torch::zeros({16384}, float_opts);

	// Call Forward
	int rendered = 0;
	if (P > 0) {
		int num_sh_coefs = shs.size(0) == 0 ? 0 : shs.size(2);
		rendered = OctreeDecoupolyRasterizer::CUDA::forward(
			geomFunc, binningFunc, imgFunc,
			P, degree, num_sh_coefs,
			background.contiguous().data_ptr<float>(),
			W, H, aabb.contiguous().data_ptr<float>(),
			positions.contiguous().data_ptr<float>(),
			decoupolys_V.contiguous().data_ptr<float>(),
			decoupolys_g.contiguous().data_ptr<float>(),
			densities.contiguous().data_ptr<float>(),
			density_shift,
			shs.contiguous().data_ptr<float>(),
			colors.contiguous().data_ptr<float>(),
			used_rank,
			depths.contiguous().data_ptr<uint8_t>(),
			scale_modifier,
			viewmatrix.contiguous().data_ptr<float>(),
			projmatrix.contiguous().data_ptr<float>(),
			campos.contiguous().data_ptr<float>(),
			tan_fovx, tan_fovy,
			random_image.contiguous().data_ptr<float>(),
			out_color.contiguous().data_ptr<float>(),
			out_depth.contiguous().data_ptr<float>(),
			out_alpha.contiguous().data_ptr<float>()
			
			// DEBUG
			// ,dbg_ray_id,
			// dbg_position.contiguous().data_ptr<float>(),
			// dbg_density.contiguous().data_ptr<float>(),
			// dbg_color.contiguous().data_ptr<float>(),
			// dbg_weight.contiguous().data_ptr<float>()
		);
	}

	// DEBUG
	// torch::save(dbg_position, "/home/t-jxiang/sparse-voxel-octree/dbg_cuda_position.pt");
	// torch::save(dbg_density, "/home/t-jxiang/sparse-voxel-octree/dbg_cuda_density.pt");
	// torch::save(dbg_color, "/home/t-jxiang/sparse-voxel-octree/dbg_cuda_color.pt");
	// torch::save(dbg_weight, "/home/t-jxiang/sparse-voxel-octree/dbg_cuda_weight.pt");

	return std::make_tuple(
		rendered, out_color, out_depth, out_alpha,
		geomBuffer, binningBuffer, imgBuffer
	);
}


std::tuple<torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor>
OctreeDecoupolyRasterizer::rasterizeOctreeDecoupolysBackwardCUDA(
	const torch::Tensor& background,
	const torch::Tensor& positions,
	const torch::Tensor& decoupolys_V,
	const torch::Tensor& decoupolys_g,
	const torch::Tensor& densities,
	const float density_shift,
	const torch::Tensor& shs,
	const int degree,
	const torch::Tensor& colors,
	const int used_rank,
	const torch::Tensor& depths,
	const float scale_modifier,
	const torch::Tensor& viewmatrix,
	const torch::Tensor& projmatrix,
	const float tan_fovx,
    const float tan_fovy,
	const torch::Tensor& out_color,
	const torch::Tensor& out_depth,
	const torch::Tensor& out_alpha,
	const torch::Tensor& grad_out_color,
	const torch::Tensor& grad_out_depth,
	const torch::Tensor& grad_out_alpha,
	const torch::Tensor& campos,
	const torch::Tensor& aabb,
	const torch::Tensor& geomBuffer,
	const int num_rendered,
	const torch::Tensor& binningBuffer,
	const torch::Tensor& imageBuffer,
	const torch::Tensor& random_image,
	const bool with_auxiliary
) {
	// Sizes
	const int P = positions.size(0);
	const int H = grad_out_color.size(1);
	const int W = grad_out_color.size(2);

	// Allocate output tensors
	torch::Tensor grad_decoupolys_V = torch::zeros({P, DECOUPOLY_RANK, 3}, positions.options());
	torch::Tensor grad_decoupolys_g = torch::zeros({P, DECOUPOLY_RANK, DECOUPOLY_DEGREE}, positions.options());
	torch::Tensor grad_densities = torch::zeros({P, DECOUPOLY_RANK}, positions.options());
	torch::Tensor grad_shs = shs.size(0) == 0 ? torch::empty({0}, positions.options()) : torch::zeros({P, DECOUPOLY_RANK, shs.size(2), CHANNELS}, positions.options());
	torch::Tensor grad_colors = torch::zeros({P, DECOUPOLY_RANK, CHANNELS}, positions.options());
	torch::Tensor aux_grad_colors2 = with_auxiliary ? torch::zeros({P, CHANNELS}, positions.options()) : torch::empty({0}, positions.options());
	torch::Tensor aux_contributions = with_auxiliary ? torch::zeros({P, 1}, positions.options()) : torch::empty({0}, positions.options());

	// Call Backward
	if (P > 0) {
		int num_sh_coefs = shs.size(0) == 0 ? 0 : shs.size(2);
		OctreeDecoupolyRasterizer::CUDA::backward(
			P, degree, num_sh_coefs, num_rendered,
			background.contiguous().data_ptr<float>(),
			W, H, aabb.contiguous().data_ptr<float>(),
			positions.contiguous().data_ptr<float>(),
			decoupolys_V.contiguous().data_ptr<float>(),
			decoupolys_g.contiguous().data_ptr<float>(),
			densities.contiguous().data_ptr<float>(),
			density_shift,
			shs.contiguous().data_ptr<float>(),
			colors.contiguous().data_ptr<float>(),
			used_rank,
			depths.contiguous().data_ptr<uint8_t>(),
			scale_modifier,
			viewmatrix.contiguous().data_ptr<float>(),
			projmatrix.contiguous().data_ptr<float>(),
			campos.contiguous().data_ptr<float>(),
			tan_fovx, tan_fovy,
			random_image.contiguous().data_ptr<float>(),
			reinterpret_cast<char*>(geomBuffer.contiguous().data_ptr()),
			reinterpret_cast<char*>(binningBuffer.contiguous().data_ptr()),
			reinterpret_cast<char*>(imageBuffer.contiguous().data_ptr()),
			out_color.contiguous().data_ptr<float>(),
			out_depth.contiguous().data_ptr<float>(),
			out_alpha.contiguous().data_ptr<float>(),
			grad_out_color.contiguous().data_ptr<float>(),
			grad_out_depth.contiguous().data_ptr<float>(),
			grad_out_alpha.contiguous().data_ptr<float>(),
			grad_decoupolys_V.contiguous().data_ptr<float>(),
			grad_decoupolys_g.contiguous().data_ptr<float>(),
			grad_densities.contiguous().data_ptr<float>(),
			grad_shs.contiguous().data_ptr<float>(),
			grad_colors.contiguous().data_ptr<float>(),
			aux_grad_colors2.contiguous().data_ptr<float>(),
			aux_contributions.contiguous().data_ptr<float>()
		);
	}

	return std::make_tuple(
		grad_decoupolys_V, grad_decoupolys_g, grad_densities, grad_shs, grad_colors, aux_grad_colors2, aux_contributions
	);
}
