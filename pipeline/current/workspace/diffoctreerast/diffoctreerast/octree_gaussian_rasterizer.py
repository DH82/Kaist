from typing import NamedTuple
import torch.nn as nn
import torch
from . import _C

def cpu_deep_copy_tuple(input_tuple):
    copied_tensors = [item.cpu().clone() if isinstance(item, torch.Tensor) else item for item in input_tuple]
    return tuple(copied_tensors)

def rasterize_octree_gaussian(
    positions,
    sh,
    colors_precomp,
    opacities,
    depths,
    aabb,
    raster_settings,
    aux_grad_color2 = None,
    aux_contributions = None,
):
    return _RasterizeOctreeGaussian.apply(
        positions,
        sh,
        colors_precomp,
        opacities,
        depths,
        aabb,
        raster_settings,
        aux_grad_color2,
        aux_contributions,
    )

class _RasterizeOctreeGaussian(torch.autograd.Function):
    @staticmethod
    def forward(
        ctx,
        positions,
        sh,
        colors_precomp,
        opacities,
        depths,
        aabb,
        raster_settings,
        aux_grad_color2,
        aux_contributions,
    ):

        # Restructure arguments the way that the C++ lib expects them
        args = (
            raster_settings.bg, 
            positions,
            colors_precomp,
            opacities,
            depths,
            raster_settings.scale_modifier,
            raster_settings.viewmatrix,
            raster_settings.projmatrix,
            raster_settings.tanfovx,
            raster_settings.tanfovy,
            raster_settings.image_height,
            raster_settings.image_width,
            sh,
            raster_settings.sh_degree,
            raster_settings.campos,
            aabb,
        )

        # Invoke C++/CUDA rasterizer
        if raster_settings.debug:
            cpu_args = cpu_deep_copy_tuple(args) # Copy them before they can be corrupted
            try:
                num_rendered, color, depth, alpha, geomBuffer, binningBuffer, imgBuffer = _C.rasterize_octree_gaussians(*args)
            except Exception as ex:
                torch.save(cpu_args, "snapshot_fw.dump")
                print("\nAn error occured in forward. Please forward snapshot_fw.dump for debugging.")
                raise ex
        else:
            num_rendered, color, depth, alpha, geomBuffer, binningBuffer, imgBuffer = _C.rasterize_octree_gaussians(*args)

        # Keep relevant tensors for backward
        ctx.raster_settings = raster_settings
        ctx.num_rendered = num_rendered
        ctx.with_aux = aux_grad_color2 is not None or aux_contributions is not None
        ctx.save_for_backward(colors_precomp, positions, opacities, depths, sh, aabb, geomBuffer, binningBuffer, imgBuffer)
        return color, depth, alpha

    @staticmethod
    def backward(ctx, grad_color, grad_depth, grad_alpha):

        # Restore necessary values from context
        num_rendered = ctx.num_rendered
        raster_settings = ctx.raster_settings
        colors_precomp, positions, opacities, depths, sh, aabb, geomBuffer, binningBuffer, imgBuffer = ctx.saved_tensors

        if grad_color is None:
            grad_color = torch.Tensor([])
        if grad_depth is None:
            grad_depth = torch.Tensor([])
        if grad_alpha is None:
            grad_alpha = torch.Tensor([])

        # Restructure args as C++ method expects them
        args = (
            raster_settings.bg,
            positions, 
            colors_precomp, 
            opacities,
            depths, 
            raster_settings.scale_modifier,
            raster_settings.viewmatrix, 
            raster_settings.projmatrix, 
            raster_settings.tanfovx, 
            raster_settings.tanfovy, 
            grad_color,
            grad_depth,
            grad_alpha,
            sh, 
            raster_settings.sh_degree, 
            raster_settings.campos,
            aabb,
            geomBuffer,
            num_rendered,
            binningBuffer,
            imgBuffer,
            ctx.with_aux,
        )

        # Compute gradients for relevant tensors by invoking backward method
        if raster_settings.debug:
            cpu_args = cpu_deep_copy_tuple(args) # Copy them before they can be corrupted
            try:
                grad_sh, grad_colors_precomp, grad_opacities, aux_grad_color2, aux_contributions = _C.rasterize_octree_gaussians_backward(*args)
            except Exception as ex:
                torch.save(cpu_args, "snapshot_bw.dump")
                print("\nAn error occured in backward. Writing snapshot_bw.dump for debugging.\n")
                raise ex
        else:
            grad_sh, grad_colors_precomp, grad_opacities, aux_grad_color2, aux_contributions = _C.rasterize_octree_gaussians_backward(*args)

        grads = (
            None,
            grad_sh,
            grad_colors_precomp,
            grad_opacities,
            None,
            None,
            None,
            aux_grad_color2 if ctx.with_aux else None,
            aux_contributions if ctx.with_aux else None,
        )

        return grads


class OctreeGaussianRasterizationSettings(NamedTuple):
    image_height: int
    image_width: int 
    tanfovx : float
    tanfovy : float
    bg : torch.Tensor
    scale_modifier : float
    viewmatrix : torch.Tensor
    projmatrix : torch.Tensor
    sh_degree : int
    campos : torch.Tensor
    debug : bool


class OctreeGaussianRasterizer(nn.Module):
    def __init__(self, raster_settings):
        super().__init__()
        self.raster_settings = raster_settings

    def forward(self, positions, opacities, shs = None, colors_precomp = None, depths = None, aabb = None, aux = None):
        
        raster_settings = self.raster_settings

        if (shs is None and colors_precomp is None) or (shs is not None and colors_precomp is not None):
            raise Exception('Please provide excatly one of either SHs or precomputed colors!')
                
        if shs is None:
            shs = torch.Tensor([])
        if colors_precomp is None:
            colors_precomp = torch.Tensor([])

        # Invoke C++/CUDA rasterization routine
        return rasterize_octree_gaussian(
            positions,
            shs,
            colors_precomp,
            opacities,
            depths,
            aabb, 
            raster_settings, 
            aux['grad_color2'] if aux is not None else None,
            aux['contributions'] if aux is not None else None,
        )

