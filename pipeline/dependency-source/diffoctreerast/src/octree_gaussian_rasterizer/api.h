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

#pragma once
#include <torch/extension.h>


namespace OctreeGaussianRasterizer {


/**
 * Rasterize a sparse gaussian octree with CUDA backend
 * 
 * @param background 		Tensor of shape (3,) containing the background color
 * @param positions 		Tensor of shape (N, 3) containing the positions of the octree nodes in [0, 1]^3
 * @param colors 			Tensor of shape (N, 3) containing the colors, leave empty if using spherical harmonics
 * @param opacities 		Tensor of shape (N, 1) containing the opacities
 * @param depths 			Tensor of shape (N, 1) containing the depths of the octree nodes
 * @param scale_modifier 	Float containing the scale modifier
 * @param viewmatrix 		Tensor of shape (4, 4) containing the view matrix
 * @param projmatrix 		Tensor of shape (4, 4) containing the projection matrix
 * @param tan_fovx 			Float containing the tangent of the horizontal field of view
 * @param tan_fovy 			Float containing the tangent of the vertical field of view
 * @param image_height 		Integer containing the image height
 * @param image_width 		Integer containing the image width
 * @param sh 				Tensor of shape (N, 3*(degree+1)^2) containing the spherical harmonics coefficients
 * @param degree 			Integer containing the degree of the spherical harmonics
 * @param campos 			Tensor of shape (3,) containing the camera position
 * @param aabb 				Tensor of shape (6,) containing the axis-aligned bounding box
 * 
 * @return A tuple containing:
 * 	- Integer containing the number of points being rasterized
 * 	- Tensor of shape (3, H, W) containing the output color
 * 	- Tensor of shape (H, W) containing the output depth
 * 	- Tensor of shape (H, W) containing the output alpha
 * 	- Tensor used for geometry buffer
 * 	- Tensor used for binning buffer
 * 	- Tensor used for image buffer
 */
std::tuple<int, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor>
rasterizeOctreeGaussiansCUDA(
	const torch::Tensor& background,
	const torch::Tensor& positions,
	const torch::Tensor& colors,
	const torch::Tensor& opacities,
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
	const torch::Tensor& aabb
);


/**
 * Backward pass of the rasterization with CUDA backend
 * 
 * @param background 		Tensor of shape (3,) containing the background color
 * @param positions 		Tensor of shape (N, 3) containing the positions of the octree nodes in [0, 1]^3
 * @param colors 			Tensor of shape (N, 3) containing the colors, leave empty if using spherical harmonics
 * @param opacities 			Tensor of shape (N, 1) containing the opacities
 * @param depths 			Tensor of shape (N, 1) containing the depths of the octree nodes
 * @param scale_modifier 	Float containing the scale modifier
 * @param viewmatrix 		Tensor of shape (4, 4) containing the view matrix
 * @param projmatrix 		Tensor of shape (4, 4) containing the projection matrix
 * @param tan_fovx 			Float containing the tangent of the horizontal field of view
 * @param tan_fovy 			Float containing the tangent of the vertical field of view
 * @param grad_out_color 	Tensor of shape (3, H, W) containing the gradient of the output color
 * @param grad_out_depth 	Tensor of shape (H, W) containing the gradient of the output depth
 * @param grad_out_alpha 	Tensor of shape (H, W) containing the gradient of the output alpha
 * @param shs 				Tensor of shape (N, 3*(degree+1)^2) containing the spherical harmonics coefficients
 * @param degree 			Integer containing the degree of the spherical harmonics
 * @param campos 			Tensor of shape (3,) containing the camera position
 * @param aabb 				Tensor of shape (6,) containing the axis-aligned bounding box
 * @param geomBuffer 		Tensor used for geometry buffer
 * @param num_rendered 		Integer containing the number of rendered points
 * @param binningBuffer 	Tensor used for binning buffer
 * @param imageBuffer 		Tensor used for image buffer
 * @param with_auxiliary 	Boolean containing whether to return the auxiliary tensors
 * 
 * @return A tuple containing:
 * 	- Tensor of shape (N, 3*(degree+1)^2) containing the gradient of the spherical harmonics coefficients
 * 	- Tensor of shape (N, 3) containing the gradient of the colors
 * 	- Tensor of shape (N, 1) containing the gradient of the opacities
 * 	- Tensor of shape (N, 3) containing the squared gradient of the colors
 * 	- Tensor of shape (N, 1) containing the contribution of the voxel to the final image
 */
std::tuple<torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor>
rasterizeOctreeGaussiansBackwardCUDA(
	const torch::Tensor& background,
	const torch::Tensor& positions,
	const torch::Tensor& colors,
	const torch::Tensor& opacities,
	const torch::Tensor& depths,
	const float scale_modifier,
	const torch::Tensor& viewmatrix,
	const torch::Tensor& projmatrix,
	const float tan_fovx,
    const float tan_fovy,
	const torch::Tensor& grad_out_color,
	const torch::Tensor& grad_out_depth,
	const torch::Tensor& grad_out_alpha,
	const torch::Tensor& shs,
	const int degree,
	const torch::Tensor& campos,
	const torch::Tensor& aabb,
	const torch::Tensor& geomBuffer,
	const int num_rendered,
	const torch::Tensor& binningBuffer,
	const torch::Tensor& imageBuffer,
	const bool with_auxiliary
);


}
