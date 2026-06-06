

void ase_engine_set_cloud_auth_mode(bool enable)
{
    if (g_engine_mode_cfg) {
        g_engine_mode_cfg->cloud_auth_enable = enable;
    }
}

bool ase_engine_is_cloud_auth_enable_imp()
{
    if (g_engine_mode_cfg) {
        return g_engine_mode_cfg->cloud_auth_enable;
    }
    return false;
}

int ase_engine_mode_cfg_open()
{
    if (g_engine_mode_cfg) {
        return 0;
    }

    uint32_t first_use = 0;
    g_engine_mode_cfg = (typeof(g_engine_mode_cfg))ase_shm_block_register_ext(
        ENGINE_MODE_CFG_SHM_NAME, sizeof(*g_engine_mode_cfg), ASE_SAME | ASE_RDWR, &first_use);
    if (!g_engine_mode_cfg) {
        return -1;
    }

    if (first_use == 1) {
        memset_s(g_engine_mode_cfg, sizeof(*g_engine_mode_cfg), 0, sizeof(*g_engine_mode_cfg));
    }

    return 0;
}

void ase_engine_mode_cfg_close()
{
    if (g_engine_mode_cfg) {
        ase_shm_block_unregister(ENGINE_MODE_CFG_SHM_NAME, g_engine_mode_cfg);
        g_engine_mode_cfg = NULL;
    }
}

bool ase_engine_mode_warning()
{
    return g_engine_mode_cfg ? g_engine_mode_cfg->engine_mode_warning : false;
}

bool g_flow_mode_is_buffered = false;

bool ase_is_flow_mode_buffer_enable()
{
    return g_flow_mode_is_buffered;
}

void ase_set_flow_mode_buffer_enable(bool enable)
{
    g_flow_mode_is_buffered = enable;
}

// #ifdef __cplusplus
// #if __cplusplus
// }
// #endif
// #endif