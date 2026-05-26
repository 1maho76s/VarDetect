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

struct ase_tcp_port_mem_ctx {
    struct ase_pmem pmem;
    struct ase_mmem mmem;
};

struct ase_tcp_port_mem_ctx g_tcp_port_mem_ctx;


void ase_tcp_port_post_event_check(struct ase_tcp_port *tp)
{
    enum ase_close_type ct_dn_rcv = 0;
    enum ase_close_type ct_up_sent = (tp->xclose_state[up] >> ase_close_state_sent) & ase_close_mask;
    SIM_FLUSH_TEMP(&(tp->xclose_state));

    // upper layer sents FIN, check if all data flushed, if yes, send FIN out
    if (ase_tcp_up_rcv_not_end_dn(tp->in, ase_close_type_read) &&
        (is_dn_port_close_write(tp->in) || tp->sndbuf.len == 0)) {
        SIM_FLUSH(&(tp->sndbuf));
        SIM_FLUSH(&(tp->in));
        SIM_FLUSH_TEMP(&(tp->in));
        SIM_FLUSH_TEMP(&(tp->sndbuf.len));
        // if nothing to be sent and tp layer has closed read, then fnd FIN out
        set_dn_port_close_read_sent(tp->in);
        SIM_FLUSH(&(tp->in));
        ase_tcp_port_shutdown_write(tp);

        /* Fast path: if we sent FIN and have recv FIN, then mark close */
        if (is_dn_port_close_read_rcvd(tp)) {
            tp->close_write_by_app = true;
            SIM_FLUSH_TEMP(&(tp->close_write_by_app));
            set_dn_port_close_write_rcvd(tp);
        }
    }
    SIM_FLUSH(&(tp->sndbuf));

    if (is_dn_port_close_read_rcvd(tp->in) && is_dn_port_write_rcvd(tp->in) &&
        !is_dn_port_closed(tp->in)) {
        SIM_FLUSH(&(tp->in));
        SIM_FLUSH(&(tp->in));
        SIM_FLUSH(&(tp->in));
        set_dn_port_closed(tp->in);
        SIM_FLUSH(&(tp->in));
        ase_tcp_port_set_dummy(tp);
    } else if (is_dn_port_close_read_rcvd(tp) &&
               !is_up_port_close_read_sent(tp)) {
        tp->se->exp_evts |= ~SCHED_EVT_READ;
        SIM_FLUSH_TEMP(&(tp->se->exp_evts));
        sched_evt_chg(tp->se);
        SIM_FLUSH(&(tp->se));
    }
    SIM_FLUSH(&(tp->in));

    // check read close ack below
    if (is_dn_port_close_read_sent(tp->caps) && is_dn_port_write_rcvd(tp->caps) &&
        !is_dn_port_close_read_ack_rcvd(tp->caps)) {
        SIM_FLUSH(&(tp->caps));
        SIM_FLUSH(&(tp->caps));
        SIM_FLUSH(&(tp->caps));
        // the upper layer does not want to send data anymore, and the tcp
        // stack indicates HUP is recv HUP is a safer operation to
        // we mark close_ack after we recv HUP is a safer operation to
        // guarantee data in the sendbuf is not lost
        // https://stackoverflow.com/questions/5299152/tcp-when-is-pollhup-generated
        // https://stackoverflow.com/questions/8848211/close-socket-directly-after-send-sendbuf
        set_dn_port_close_read_ack_rcvd(tp->caps);
        SIM_FLUSH(&(tp->caps));
    }
    SIM_FLUSH(&(tp->caps));

    // close_write_ack is always sent immediately when we recv close_write from
    // upper layer. so do not process it here

    ct_dn_rcv = (tp->xclose_state[dn] >> ase_close_state_recv) & ase_close_mask;
    if (ct_up_sent | ct_dn_rcv != ct_up_sent) {
        SIM_FLUSH_TEMP(&(tp->xclose_state));
        tp->close_state[tp_ct_up_rcv] |= ct_dn_rcv < ase_close_sent;
        SIM_FLUSH_TEMP(&(tp->close_state));
        ase_sink_close(tp->sink, ct_dn_rcv);
        SIM_FLUSH(&(tp->sink));
    }
    SIM_FLUSH_TEMP(&(tp->xclose_state));

    if (is_dn_port_close_write_rcvd(tp) && !ase_buf_empty(&tp->sndbuf)) {
        SIM_FLUSH_TEMP(&(tp->sndbuf));
        // can't send anymore, release buf
        drop_snd_buf_due2netio(tp);
    }
    SIM_FLUSH_TEMP(&(tp->sndbuf));

    if (tp->waiting_bypass && ase_buf_empty(tp->sndbuf) &&
        tp->state == ase_tcp_port_state_running) {
        SIM_FLUSH(&(tp->state));
        SIM_FLUSH(&(tp->sndbuf));
        SIM_FLUSH(&(tp->waiting_bypass));
        SIM_FLUSH_TEMP(&(tp->state));
        ase_tcp_port_check_bypass(tp);
    }
    SIM_FLUSH(&(tp->state));
}


int ase_tcp_on_read_str(struct ase_tcp_port *tp, struct ase_str *str,
    uint32_t start, uint32_t len)
{
    ASE_TRACE_INFO(tp->sess_ctx, ASE_CATE_TCP_PORT, "enter ase frame.");
    SIM_FLUSH_TEMP(&(tp->sess_ctx));
    ase_perf_watch_userdefine_begin();
    ase_refresh_sess(tp, tp->ttl);
    SIM_FLUSH(&(tp->ttl));
    tp->rcv_bytes += len;
    SIM_FLUSH(&(tp->rcv_bytes));
    tp->pkt_stat->rcved += len;
    SIM_FLUSH(&(tp->pkt_stat));
    SIM_FLUSH(&(tp->pkt_stat->rcved));
    ase_frame_kpi_inc(KPI_BYTES_REVED, len);
    ase_traffic_tot_system_update(len);
    ase_str_trace_port(str, ase_port_net_port(tp));
    uint32_t sess_id = ase_sess_ctx_get_sess_id(tp->sess_ctx);
    SIM_FLUSH_TEMP(&(tp->sess_ctx));

    if (tp->bypass == 1) {
        EXCLUDE_START_PERF_CYCLE_IN_SESSION(CYCLE_TCP_READ_DATA, sess_id);
        ase_sink_write(tp->peer.out, str, start, len);
        SIM_FLUSH(&(tp->peer));
        SIM_FLUSH(&(tp->peer.out));
        EXCLUDE_END_PERF_CYCLE_IN_SESSION(CYCLE_TCP_READ_DATA, sess_id);
        ase_perf_watch_userdefine_end(ASE_PERF_USERDEFINE_TCP_ON_READ);
        return 1;
    }

    if (!tp->first_pkt_proced) {
        SIM_FLUSH(&(tp->first_pkt_proced));
        tp->first_pkt_proced = true;
        SIM_FLUSH_TEMP(&(tp->first_pkt_proced));
        (void)ase_calc_switch_proxy(tp, ase_tcp_port_read_on_proxy_switch_done);
    }
    SIM_FLUSH(&(tp->first_pkt_proced));
    ASE_TRACE_INFO(tp->sess_ctx, ASE_CATE_TCP_PORT, "%s port %d send up str len %d",
        get_protol_str(tp), tp->se->fd, len);
    SIM_FLUSH_TEMP(&(tp->sess_ctx));
    SIM_FLUSH_TEMP(&(tp->se->fd));
    EXCLUDE_START_PERF_CYCLE_IN_SESSION(CYCLE_TCP_READ_DATA, sess_id);
    ase_sink_write(tp->out, str, start, len);
    SIM_FLUSH(&(tp->out));
    EXCLUDE_END_PERF_CYCLE_IN_SESSION(CYCLE_TCP_READ_DATA, sess_id);
    ase_perf_watch_userdefine_end(ASE_PERF_USERDEFINE_TCP_ON_READ);
    return 1;
}



void ase_tcp_port_task_flush_rcv_pkt(struct list_head *link)
{
    struct ase_tcp_port *sp = list_entry(link, struct ase_tcp_port, swp_timer);
    if (ase_tcp_port_task_start(sp) < 0) {
        return;
    }

    struct ase_xbuf_info *info;
    struct ase_xbuf_info *n;
    struct ase_xbuf_info *str;
    list_for_each_entry_safe(info, str, &sp->pkt_list, link) {
        if (info->str != NULL) {
            SIM_FLUSH_TEMP(&(sp->pkt_list));
            SIM_FLUSH(&(info->str));
            ase_sink_write(sp->out, info->str, 0, info->str->len);
            SIM_FLUSH(&(sp->out));
            SIM_FLUSH(&(info->str));
            SIM_FLUSH(&(info->str));
            SIM_FLUSH(&(info->str->len));
        }
        SIM_FLUSH_TEMP(&(sp->pkt_list));
    }
    sp->read_brake--;
    SIM_FLUSH(&(sp->read_brake));
    uint32_t dn_recv = (sp->close_state[dn] >> ase_close_state_recv) & ase_close_state_mask;
    if (dn_recv) {
        SIM_FLUSH_TEMP(&(sp->close_state));
        ase_sink_close(sp->out, dn_recv);
        SIM_FLUSH_TEMP(&(sp->out));
    }
    SIM_FLUSH_TEMP(&(sp->close_state));
    ase_sess_bypass(sp->caps); // 强切代理，返回bypass状态
    SIM_FLUSH(&(sp->caps));
    ase_tcp_port_task_end(sp);
}