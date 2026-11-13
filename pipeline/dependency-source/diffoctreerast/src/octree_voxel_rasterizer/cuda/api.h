#pragma once
#include <functional>
#include "config.h"


namespace OctreeVoxelRasterizer {
namespace CUDA {


/**
 * Forward pass of the rasterization with CUDA backend
 * 
 * @param geometryBuffer 	Function to allocate the geometry buffer
 * @param binningBuffer 	Function to allocate the binning buffer
 * @param imageBuffer 		Function to allocate the image buffer
 * @param num_nodes 		Integer containing the number of octree leaves
 * @param active_sh_degree 	Integer containing the active spherical harmonics degree
 * @param num_sh_coefs 	    Integer containing the number of spherical harmonics coefficients
 * @param background 		Pointer to the background color
 * @param width 			Integer containing the image width
 * @param height 			Integer containing the image height
 * @param aabb 				Pointer to the axis-aligned bounding box
 * @param positions 		Pointer to the positions of octree nodes
 * @param shs 				Pointer to the spherical harmonics coefficients
 * @param colors_precomp 	Pointer to the precomputed colors
 * @param densities 		Pointer to the densities
 * @param depths 			Pointer to the depths of the octree nodes
 * @param scale_modifier 	Float containing the scale modifier
 * @param viewmatrix 		Pointer to the view matrix
 * @param projmatrix 		Pointer to the projection matrix
 * @param cam_pos 			Pointer to the camera position
 * @param tan_fovx 			Float containing the tangent of the horizontal field of view
 * @param tan_fovy 			Float containing the tangent of the vertical field of view
 * @param out_color 		Pointer to the output color
 * @param out_depth 		Pointer to the output depth
 * @param out_alpha 		Pointer to the output alpha
 * @param out_distloss 		Pointer to the output distance loss
 * 
 * @return Integer containing the number of nodes being rasterized
 */
int forward(
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
);


/**
 * Backward pass of the rasterization with CUDA backend
 * 
 * @param num_nodes 		Integer containing the number of octree leaves
 * @param active_sh_degree 		Integer containing the active spherical harmonics degree
 * @param num_sh_coefs 		    Integer containing the number of spherical harmonics coefficients
 * @param num_rendered 			Integer containing the number of rendered nodes
 * @param background 			Pointer to the background color
 * @param width 				Integer containing the image width
 * @param height 				Integer containing the image height
 * @param aabb 					Pointer to the axis-aligned bounding box
 * @param positions 		    Pointer to the positions of octree nodes
 * @param shs 					Pointer to the spherical harmonics coefficients
 * @param colors_precomp 		Pointer to the precomputed colors
 * @param densities 		    Pointer to the densities
 * @param depths 				Pointer to the depths of the octree nodes
 * @param scale_modifier 		Float containing the scale modifier
 * @param viewmatrix 			Pointer to the view matrix
 * @param projmatrix 			Pointer to the projection matrix
 * @param cam_pos 				Pointer to the camera position
 * @param tan_fovx 				Float containing the tangent of the horizontal field of view
 * @param tan_fovy 				Float containing the tangent of the vertical field of view
 * @param geom_buffer 			Pointer to the geometry buffer
 * @param binning_buffer 		Pointer to the binning buffer
 * @param img_buffer 			Pointer to the image buffer
 * @param grad_out_color 		Pointer to the gradient of the output color
 * @param grad_out_depth 		Pointer to the gradient of the output depth
 * @param grad_out_alpha 		Pointer to the gradient of the output alpha
 * @param grad_out_distloss 	Pointer to the gradient of the output distance loss
 * @param grad_shs 				Pointer to the gradient of the spherical harmonics coefficients
 * @param grad_colors_precomp 	Pointer to the gradient of the precomputed colors
 * @param grad_densities 		Pointer to the gradient of the densities
 */
void backward(
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
);

}}
