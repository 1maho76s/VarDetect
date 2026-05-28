#include "sim_mem.h"
#include "ase_tcp_port.h"
#include "ase_debug.h"
#include "ase_mem.h"
#include "ase_pmem.h"
#include "ase_mmem.h"
#include "sched_task.h"
#include "sched_list.h"
#include "ase_str.h"
#include "ase_io.h"
#include "ase_dlt_factory.h"
#include "ase_buf.h"
#include "ase_sock.h"
#include "ase_port_factory.h"
#include "ase_aging.h"
#include "ase_net_port.h"
#include "ase_stat.h"
#include "ase_sess_status.h"
#include "ase_tcp_port_sock_ops.h"
#include "ase_framework_builder.h"
#include "ase_client.h"
#include "ase_dbg_trace.h"
#include "ase_connection_id.h"
#include "ase_sys_dfx_frame.h"
#include "ase_sys_dfx_traffic.h"
#include "ase_timer_list.h"
#include "ase_perf_analyse.h"
#include "ase_forward_cfg.h"
#include "ase_mbuf_monitor.h"
#include "ase_pfd.h"
#include "ase_rtcm.h"
#include "ase_perf_session.h"
#include "ase_tproxy_str.h"
#include "ase_frame_kpi.h"
#include "bitfield_struct.h"

struct ase_tcp_port_mem_ctx
{
    struct ase_pmem pmem;
    struct ase_mmem mmem;
};

struct ase_tcp_port_mem_ctx g_tcp_port_mem_ctx;

static inline bool no_need_check_sa_bypass(struct ase_tcp_port *tp)
{
    return (!g_sa_on_ase ||                               /* sa 未部署在ase内，比如 ar 产品 */
            tp->sess_ctx->func != ase_sess_content_sec || /* 非转发类业务，如显式代理业务 */
            tp->sess_ctx->d_fwd);                         /* 配置了直转，肯定没有 sa 结果 */
}

int ase_listener_set_hps_special_opt(int fd, struct ase_listener_material *m,
                                     const struct ase_sock_ops *sk_ops)
{
#ifndef __STDIN__
    if (sk_ops->type == ASE_SOCK_TYPE_LINUX)
    {
        SIM_FLUSH_TEMP(&(sk_ops->type));
        return 0; // 支持设备上用 linux socket
    }
    SIM_FLUSH_TEMP(&(sk_ops->type));
    /* set ase mode */
    int opt = 3; /* 3 explicit proxy mode */
    if (sk_ops->setsockopt(fd, SOL_SOCKET, HPS_SO_PROXY_MODE,
                           (void *)&opt, sizeof(int)) < 0)
    {
        SIM_FLUSH_TEMP(&(sk_ops->setsockopt));
        ASE_ERR(ASE_CATE_LISTENER, "ase listener set socket opt proxy failed!"
                                   " errno:%d",
                errno);
        return -1;
    }
    SIM_FLUSH_TEMP(&(sk_ops->setsockopt));
    if (m->vrf != 0){
        SIM_FLUSH_TEMP(&(m->vrf));
        HpsPktInfo info = {
            .ull3Info = 1,
            .ullVrfIndex = m->vrf
        };
    SIM_FLUSH_TEMP(&(m->vrf));
        if (sk_ops->setsockopt(fd, SOL_SOCKET, HPS_SO_RCVVPNID,
                               (char *)&info, sizeof(info)) < 0)
        {
            SIM_FLUSH_TEMP(&(sk_ops->setsockopt));
            ASE_ERR(ASE_CATE_LISTENER, "ase listener set socket opt vrf failed!"
                                       " errno:%d",
                    errno);
            return -1;
        }
        SIM_FLUSH_TEMP(&(sk_ops->setsockopt));
    }
    int no_syn_cookie = 0;
    if (m->no_syn_cookie)
    {
        SIM_FLUSH_TEMP(&(m->no_syn_cookie));
        no_syn_cookie = 1;
    }
    SIM_FLUSH_TEMP(&(m->no_syn_cookie));
    ASE_INFO(ASE_CATE_LISTENER, "set socket fd %d opt no_syn_cookie %d.", fd, no_syn_cookie);
    if (no_syn_cookie == 1 &&
        sk_ops->setsockopt(fd, SOL_SOCKET, HPS_SO_NO_SYNCOOKIE, (void *)&no_syn_cookie, sizeof(int)) < 0)
    {
        SIM_FLUSH_TEMP(&(sk_ops->setsockopt));
        ASE_ERR(ASE_CATE_LISTENER, "failed to set socket opt no_syn_cookie! errno: %d\n!", errno);
    }
    SIM_FLUSH_TEMP(&(sk_ops->setsockopt));
#endif
    return 0;
}

void ase_tcp_port_check_bypass(struct ase_tcp_port *tp)
{
    struct ase_tcp_port *peer = tp->peer.psp;
    SIM_FLUSH(&(tp->peer.psp));
    if (peer == NULL)
    {
        /* peer is in connecting, peer tcp port is not ready */
        ase_tcp_bypass_self_sess(tp);
        if (ase_connection_forced_detect(tp->sess_ctx->conn))
        {
            SIM_FLUSH(&(tp->sess_ctx));
            SIM_FLUSH_TEMP(&(tp->sess_ctx->conn));
            ASE_TRACE_INFO(tp->sess_ctx, ASE_CATE_TCP_PORT, "fd %d ase forced but bypass", tp->se->fd);
            SIM_FLUSH_TEMP(&(tp->sess_ctx));
            SIM_FLUSH_INTER_PTR(&(tp->se));
            SIM_FLUSH_TEMP(&(tp->se->fd));
            ase_dfx_cnt_inc(ASE_DFX_FORCE_FAIL);
        }
        SIM_FLUSH(&(tp->sess_ctx));
        SIM_FLUSH_TEMP(&(tp->sess_ctx->conn));
        return;
    }
    ASE_TRACE_INFO(tp->sess_ctx, ASE_CATE_TCP_PORT, "%s check bypass pair(%d-%d) flag %d-%d ase_only %d",
                   get_protol_str(tp), tp->se->fd, peer->se->fd, tp->waiting_bypass,
                   ase_buf_empty(&peer->sndbuf), tp->bypass_ase_only);
    SIM_FLUSH_TEMP(&(tp->sess_ctx));
    SIM_FLUSH_INTER_PTR(&(tp->se));
    SIM_FLUSH_TEMP(&(tp->se->fd));
    SIM_FLUSH_INTER_PTR(&(peer->se));
    SIM_FLUSH_TEMP(&(peer->se->fd));
    SIM_FLUSH_TEMP(&(tp->waiting_bypass));
    SIM_FLUSH_TEMP(&(peer->sndbuf));
    if (peer->waiting_bypass && ase_buf_empty(&peer->sndbuf))
    {
        SIM_FLUSH_TEMP(&(peer->waiting_bypass));
        SIM_FLUSH_TEMP(&(peer->sndbuf));
        // 透明代理模式支持bypass，所以这里对代理模式的拦截删掉了
        // 显式代理的场景，ase_src 里拦截了，不会到这里
        if (ase_connection_forced_detect(tp->sess_ctx->conn))
        {
            SIM_FLUSH(&(tp->sess_ctx));
            SIM_FLUSH_TEMP(&(tp->sess_ctx->conn));
            ASE_TRACE_INFO(tp->sess_ctx, ASE_CATE_TCP_PORT, "fd %d ase forced but bypass pair", tp->se->fd);
            SIM_FLUSH_TEMP(&(tp->sess_ctx));
            SIM_FLUSH_INTER_PTR(&(tp->se));
            SIM_FLUSH_TEMP(&(tp->se->fd));
            ase_dfx_cnt_inc(ASE_DFX_FORCE_FAIL);
        }
        SIM_FLUSH(&(tp->sess_ctx));
        SIM_FLUSH_TEMP(&(tp->sess_ctx->conn));
        if (tp->bypass_ase_only)
        {
            ase_dfx_cnt_inc(ASE_DFX_BYPASS_ASE_ONLY);
            ase_sess_bypass_done(tp->caps);
            SIM_FLUSH(&(tp->caps));
            ase_sess_bypass_done(peer->caps);
            SIM_FLUSH(&(peer->caps));
            return;
        }
        bool is_transparent_proxy = ase_is_transparent_proxy(tp);
        int ret = tp->sk_ops->bypass_pair(tp->se->fd, peer->se->fd);
        SIM_FLUSH(&(tp->sk_ops));
        SIM_FLUSH_TEMP(&(tp->sk_ops->bypass_pair));
        SIM_FLUSH(&(tp->se));
        SIM_FLUSH_TEMP(&(tp->se->fd));
        SIM_FLUSH(&(peer->se));
        SIM_FLUSH_TEMP(&(peer->se->fd));
        if (ret < 0)
        {
            ase_dfx_cnt_inc(is_transparent_proxy ? ASE_DFX_TRANSPARENT_PROXY_BYPASS_FAILED : ASE_DFX_BYPASS_FAILED);
            ase_tcp_port_set_net_dfd(tp, peer);
            ASE_TRACE_ERR(tp->sess_ctx, ASE_CATE_TCP_PORT, "%s port bypass fd %d-%d failed,"
                                                           " forward in socket layer",
                          get_protol_str(tp),
                          tp->se->fd, peer->se->fd);
            SIM_FLUSH_TEMP(&(tp->sess_ctx));
            SIM_FLUSH_INTER_PTR(&(tp->se));
            SIM_FLUSH_TEMP(&(tp->se->fd));
            SIM_FLUSH_INTER_PTR(&(peer->se));
            SIM_FLUSH_TEMP(&(peer->se->fd));
            return;
        }
        ase_dfx_cnt_inc(is_transparent_proxy ? ASE_DFX_TRANSPARENT_PROXY_BYPASS : ASE_DFX_BYPASS);
        ase_sess_bypass_done(tp->caps);
        SIM_FLUSH(&(tp->caps));
        ase_sess_bypass_done(peer->caps);
        SIM_FLUSH(&(peer->caps));
    }
    SIM_FLUSH_TEMP(&(peer->waiting_bypass));
    SIM_FLUSH_TEMP(&(peer->sndbuf));
}