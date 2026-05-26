#ifndef BITFIELD_STRUCT_H
#define BITFIELD_STRUCT_H

// 包含位域成员的普通结构体，用于测试插桩工具对位域字段的处理
struct BitFieldDevice {
    unsigned int status   : 4;   // bits 0-3:   设备状态
    unsigned int mode     : 2;   // bits 4-5:   工作模式
    unsigned int reserved : 2;   // bits 6-7:   保留位
    unsigned int channel  : 8;   // bits 8-15:  通道号
    unsigned int enabled  : 1;   // bit  16:    使能标志
    int          error_code;      // 完整字段，用于对比
    unsigned int priority : 3;   // bits 17-19: 优先级
    unsigned int : 0;            // 匿名位域：强制对齐到下一个 unsigned int
    unsigned int flags    : 12;  // 位标志集合
};

typedef struct stNGE_CFGFILE {
    char *pcKeyEn;
    char *pcKeyCh;
    char *pcDefaultValue;
    uint32_t uiModType;
    uint32_t uiModSpecType;
    uint32_t uiParseState : 1, uiIsNeedCheck : 1, uiIsNeedShow : 1, uiReserve : 29;
} NGE_CFGFILE_S;

// typedef 形式的结构体（匿名结构体 + typedef 命名）
typedef struct {
    unsigned int a : 2;
    unsigned int b : 6;
    int          c;         // 普通字段
    unsigned int d : 4;
} DeviceConfig;

// typedef 形式（命名结构体 + typedef 别名）
typedef struct TaggedSensor {
    unsigned int id     : 8;
    unsigned int active : 1;
    float        value;     // 普通字段
} Sensor;


/* source: engine/net/ase_tcp_port.h */
struct ase_tcp_port {
    uint32_t close_state[2];

    struct list_head tasks;
    struct list_head write_tasks;
    struct list_head sup_timer;
    struct list_head aging_timer;
    struct ase_port net_port;
    const struct ase_tcp_port_ops *sk_ops;

    struct ase_sink in;
    struct ase_sink *out;
    struct ase_mem *mem;
    struct ase_sess_caps *caps;
    struct ase_session_ctx *sess_ctx;
    struct {
        struct ase_tcp_port *psp;
        struct ase_sink in;
        struct ase_sink *out;
    } peer;
    struct ase_slink;
    struct ase_pkt_stats pkt_stat;
    struct sched_ent *se;
    struct ase_buf sndbuf;

    uint64_t rcv_bytes;
    uint64_t snd_bytes;
    uint32_t drop_bytes;
    uint32_t start_time;
    uint32_t ttl;
    uint16_t ref_cnt;
    uint8_t processing;
    uint8_t inc_pkttl;

    enum ase_tcp_port_state state;
    struct ase_timer_list cur_timer_list;
    ase_tcp_port_timeout_action timeout_action;

    enum bypass : 1;
    bool write_brake : 1;
    bool bypassing_bypass : 1;
    bool client : 1;
    bool waiting : 1;
    bool unspecified_ttl : 1;
    bool is_udp : 1;
    bool switching_proxy : 1;
    bool switch_proxy_done : 1;
    bool reg_svr_map : 1;
    bool not_need_cap_pkt : 1;
    bool need_more_data : 1;
    bool wait_stack_write : 1;
    bool close_stack_read : 1;
    bool close_read_by_app : 1;
    bool close_write_by_net : 1;
    bool close_write_by_nt : 1;
    bool first_pkt_procd : 1;
    bool proxy_mode : 1;
    bool cache_buf_err : 1;
    bool ignore_upper_data : 1;
    bool disable_pfd_check : 1;
    bool no_need_check_pkt_drop : 1;
    bool bypass_ase_only : 1;
    bool need_check_pkt_hold_too_long : 1;
};

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

#define offsetof(TYPE, MEMBER) ((size_t) &((TYPE *)0)->MEMBER)

#define container_of(ptr, type, member) ({          \
    typeof( ((type *)0)->member ) *__mptr = (ptr);  \
    (type *)( (char *)__mptr - offsetof(type,member) );})

#define list_entry(item, type, member) container_of(item, type, member)

#define list_for_each_entry_safe(item, n, list, member) for (item = list_entry((list)->next, typeof(*item), member), \
    n = list_entry((item)->member.next, typeof(*item), member); \
    &((item)->member) != (list); item = n, n = list_entry((n)->member.next, typeof(*item), member))

#endif // BITFIELD_STRUCT_H
