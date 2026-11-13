from setuptools import setup
from torch.utils.cpp_extension import CUDAExtension, BuildExtension
import os
os.path.dirname(os.path.abspath(__file__))

setup(
    name="diffoctreerast",
    packages=['diffoctreerast'],
    ext_modules=[
        CUDAExtension(
            name="diffoctreerast._C",
            sources=[
                # Octree Voxel rasterization
                "src/octree_voxel_rasterizer/cuda/data_structure.cu",
                "src/octree_voxel_rasterizer/cuda/forward.cu",
                "src/octree_voxel_rasterizer/cuda/backward.cu",
                "src/octree_voxel_rasterizer/api.cpp",
                # Octree Gaussian rasterization
                "src/octree_gaussian_rasterizer/cuda/data_structure.cu",
                "src/octree_gaussian_rasterizer/cuda/forward.cu",
                "src/octree_gaussian_rasterizer/cuda/backward.cu",
                "src/octree_gaussian_rasterizer/api.cpp",
                # Octree Trivec rasterization
                "src/octree_trivec_rasterizer/cuda/data_structure.cu",
                "src/octree_trivec_rasterizer/cuda/forward.cu",
                "src/octree_trivec_rasterizer/cuda/backward.cu",
                "src/octree_trivec_rasterizer/api.cpp",
                # Octree Decoupled Polynomial rasterization
                "src/octree_decoupoly_rasterizer/cuda/data_structure.cu",
                "src/octree_decoupoly_rasterizer/cuda/forward.cu",
                "src/octree_decoupoly_rasterizer/cuda/backward.cu",
                "src/octree_decoupoly_rasterizer/api.cpp",
                # Main
                "src/ext.cpp"
            ],
            extra_compile_args={"nvcc": ["-I" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib/glm/")]})
        ],
    cmdclass={
        'build_ext': BuildExtension
    }
)
