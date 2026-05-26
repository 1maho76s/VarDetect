#ifndef ASE_NET_PORT_H
#define ASE_NET_PORT_H

struct ase_net_port;

enum ase_sess_ttl_type {
    ase_sess_ttl_normal,  /* refresh hit time */
    ase_sess_ttl_bypass,  /* no refresh bypass sess after timeout */
    ase_sess_ttl_close,   /* no refresh close sess after timeout */
};

struct ase_net_port_ops {
    void (*switch_proxy)(struct ase_net_port *port);
    void (*connect_peer)(struct ase_net_port *port, struct ase_net_port *peer);
    void (*set_force_close)(struct ase_net_port *port, int on);
    void (*set_sess_ttl)(struct ase_net_port *port, uint32_t ttl, enum ase_sess_ttl_type type);
    uint32_t (*get_sess_ttl)(struct ase_net_port *port);
    uint32_t (*get_last_hit_time)(struct ase_net_port *port);
    void (*close_cap_pkt)(struct ase_net_port *port);
    bool (*has_data2send)(struct ase_net_port *port);
    int (*get_fd)(struct ase_net_port *port); // [升级兼容性] 起始版本: R23C10
    uint32_t (*clean_pkt_holding_cnt)(struct ase_net_port *port);
    void (*clean_pkt_trace)(struct ase_net_port *port);
    void (*set_quicack)(struct ase_net_port *port);
    void (*block)(struct ase_net_port *port);
    void (*pkts_drop)(struct ase_net_port *port);
};

struct ase_net_port {
    void *data;
    const struct ase_net_port_ops *ops;
};

static inline void ase_net_port_switch_proxy(struct ase_net_port *port)
{
    port->ops->switch_proxy(port);
}

static inline void ase_net_port_connect_peer(struct ase_net_port *port,
    struct ase_net_port *peer)
{
    port->ops->connect_peer(port, peer);
}

static inline void ase_net_port_set_force_close(struct ase_net_port *port, int on)
{
    port->ops->set_force_close(port, on);
}

#endif // ASE_NET_PORT_H
