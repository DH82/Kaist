#pragma once

// #define DEBUG
#define BLOCK_X 8
#define BLOCK_Y 8
#define CHANNELS 3
#define MEM_ALIGNMENT 256
#define MAX_TREE_DEPTH 10
#define PREFETCH_BUFFER_SIZE 8

// Trivec Shape
#define TRIVEC_SIZE(dim, chs) (3 * dim * chs)
#define TRIVEC_X_CH(dim, n) (n * 3 * dim)
#define TRIVEC_Y_CH(dim, n) (n * 3 * dim + dim)
#define TRIVEC_Z_CH(dim, n) (n * 3 * dim + 2 * dim)

// Optimizations
#define ASYNC_GLOBAL_TO_SHARED

#define GRAD_GLOBAL
// #define GRAD_SHARED_TO_GLOBAL
// #define GRAD_LOCAL_TO_GLOBAL
// #define GRAD_LOCAL_REDUCED_TO_GLOBAL
