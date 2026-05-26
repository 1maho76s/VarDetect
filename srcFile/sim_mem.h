#ifndef SIM_MEM_H
#define SIM MEM_H
#include <stdint.h>
#define CACHE_LINE_SIZE 64

static inline void* cacheline_addr(void* p) {
    return (void*)((uintptr_t)p & ~(CACHE_LINE_SIZE - 1));
}

static inline void sim_flush(void* addr) {
    addr = cacheline_addr(addr);
    __asm__ volatile("dc civac, %0" :: "r"(addr));
    __asm__ volatile("dsb ish");    //必须
}


#define SIM_FLUSH(addr) sim_flush((void*)(addr))

#define SIM_FLUSH_TEMP(addr) sim_flush((void*)(addr))

#endif