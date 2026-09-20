"""通信协议样例模块：成帧 / CRC-16 校验 / 重传窗口。

这份代码同时是三个东西的基准：
  1. 受限 C 子集的「合格样本」——静态检查应当零必查项违规；
  2. build.sh + gcov 的真实输入——覆盖率解析器按它的输出校准；
  3. 端到端验收的试点目标（scripts/make_comm_prd.py 的需求就对应这里：
     帧格式、窗口容量、错误码与 CRC 参数与本文档一字不差）。
"""

COMM_H = r"""#ifndef COMM_H
#define COMM_H

#include <stdint.h>
#include <stddef.h>   /* NULL：本模块以 NULL 表示空指针参数，stddef 才是其定义处 */

/* 帧格式：HEAD(1) SEQ(1) LEN(1) PAYLOAD(LEN) CRC16(2)
   CRC 覆盖 SEQ 与 LEN 与 PAYLOAD，大端序附在帧尾。 */
#define COMM_HEAD 0xA5
#define COMM_MAX_PAYLOAD 32
#define COMM_FRAME_OVERHEAD 5
#define COMM_FRAME_MAX (COMM_MAX_PAYLOAD + COMM_FRAME_OVERHEAD)
#define COMM_WINDOW 4

enum {
    COMM_OK = 0,
    COMM_ERR_ARG = -1,
    COMM_ERR_LEN = -2,
    COMM_ERR_CRC = -3,
    COMM_ERR_SEQ = -4,
    COMM_ERR_FULL = -5,
    COMM_ERR_EMPTY = -6
};

typedef struct {
    uint8_t seq;
    uint8_t len;
    uint8_t payload[COMM_MAX_PAYLOAD];
} CommFrame;

typedef struct {
    uint8_t tx_seq;         /* 下一帧发送序号 */
    uint8_t rx_seq;         /* 期望接收序号 */
    uint8_t pending_count;  /* 已发送未确认的帧数 */
    uint8_t retransmits;    /* 累计重传次数 */
    CommFrame pending[COMM_WINDOW];
} CommCtx;

void comm_init(CommCtx *ctx);
uint16_t comm_crc16(const uint8_t *data, int len);
int comm_pack(CommCtx *ctx, const uint8_t *payload, int len,
              uint8_t *out, int out_cap);
int comm_unpack(CommCtx *ctx, const uint8_t *raw, int raw_len, CommFrame *out);
void comm_ack(CommCtx *ctx, uint8_t seq);
int comm_next_retransmit(CommCtx *ctx, CommFrame *out);

#endif /* COMM_H */
"""

COMM_C = r"""#include "comm.h"

/* CRC-16/CCITT-FALSE：poly 0x1021，init 0xFFFF，不反转。
   查表法要占 512 字节常量区，位算法更省，星载场景优先省内存。 */
uint16_t comm_crc16(const uint8_t *data, int len)
{
    uint16_t crc = 0xFFFFu;
    int i;
    int b;

    if (data != NULL && len > 0) {
        for (i = 0; i < len; i++) {
            crc = (uint16_t)(crc ^ (uint16_t)((uint16_t)data[i] << 8));
            for (b = 0; b < 8; b++) {
                crc = ((crc & 0x8000u) != 0u)
                      ? (uint16_t)((uint16_t)(crc << 1) ^ 0x1021u)
                      : (uint16_t)(crc << 1);
            }
        }
    }
    return crc;
}

void comm_init(CommCtx *ctx)
{
    int i;

    if (ctx != NULL) {
        ctx->tx_seq = 0u;
        ctx->rx_seq = 0u;
        ctx->pending_count = 0u;
        ctx->retransmits = 0u;
        for (i = 0; i < COMM_WINDOW; i++) {
            ctx->pending[i].seq = 0u;
            ctx->pending[i].len = 0u;
        }
    }
}

static int pack_check(const CommCtx *ctx, const uint8_t *payload, int len,
                      int out_cap)
{
    int rc = COMM_OK;

    if (ctx == NULL) {
        rc = COMM_ERR_ARG;
    } else if (len < 0 || len > COMM_MAX_PAYLOAD) {
        rc = COMM_ERR_LEN;
    } else if (payload == NULL) {
        rc = COMM_ERR_ARG;
    } else if (out_cap < len + COMM_FRAME_OVERHEAD) {
        rc = COMM_ERR_LEN;
    } else if (ctx->pending_count >= COMM_WINDOW) {
        rc = COMM_ERR_FULL;
    }
    return rc;
}

static void frame_write(const CommFrame *frame, uint8_t *out, int len)
{
    int i;
    uint16_t crc;

    out[0] = COMM_HEAD;
    out[1] = frame->seq;
    out[2] = frame->len;
    for (i = 0; i < len; i++) {
        out[3 + i] = frame->payload[i];
    }
    crc = comm_crc16(&out[1], len + 2);
    out[len + 3] = (uint8_t)(crc >> 8);
    out[len + 4] = (uint8_t)(crc & 0xFFu);
}

int comm_pack(CommCtx *ctx, const uint8_t *payload, int len,
              uint8_t *out, int out_cap)
{
    int rc;
    int i;
    CommFrame *slot;

    rc = (out == NULL) ? COMM_ERR_ARG : pack_check(ctx, payload, len, out_cap);
    if (rc == COMM_OK) {
        slot = &ctx->pending[ctx->pending_count];
        slot->seq = ctx->tx_seq;
        slot->len = (uint8_t)len;
        for (i = 0; i < len; i++) {
            slot->payload[i] = payload[i];
        }
        frame_write(slot, out, len);
        ctx->pending_count = (uint8_t)(ctx->pending_count + 1);
        ctx->tx_seq = (uint8_t)((ctx->tx_seq + 1) % 256);
    }
    return rc;
}

static int unpack_check(const CommCtx *ctx, const uint8_t *raw, int raw_len,
                        const CommFrame *out)
{
    int rc = COMM_OK;

    if (ctx == NULL || raw == NULL || out == NULL) {
        rc = COMM_ERR_ARG;
    } else if (raw_len < COMM_FRAME_OVERHEAD) {
        rc = COMM_ERR_LEN;
    }
    return rc;
}

int comm_unpack(CommCtx *ctx, const uint8_t *raw, int raw_len, CommFrame *out)
{
    int rc;
    int i;
    int payload_len;
    uint16_t got;
    uint16_t want;

    rc = unpack_check(ctx, raw, raw_len, out);
    if (rc == COMM_OK) {
        payload_len = (int)raw[2];
        got = 0u;
        want = 0u;
        if (raw[0] != COMM_HEAD) {
            rc = COMM_ERR_ARG;
        } else if (raw_len < payload_len + COMM_FRAME_OVERHEAD) {
            rc = COMM_ERR_LEN;
        } else {
            got = (uint16_t)(((uint16_t)raw[payload_len + 3] << 8)
                             | (uint16_t)raw[payload_len + 4]);
            want = comm_crc16(&raw[1], payload_len + 2);
            if (got != want) {
                rc = COMM_ERR_CRC;
            } else if (raw[1] != ctx->rx_seq) {
                rc = COMM_ERR_SEQ;
            } else {
                out->seq = raw[1];
                out->len = (uint8_t)payload_len;
                for (i = 0; i < payload_len; i++) {
                    out->payload[i] = raw[3 + i];
                }
                ctx->rx_seq = (uint8_t)((ctx->rx_seq + 1) % 256);
            }
        }
    }
    return rc;
}

void comm_ack(CommCtx *ctx, uint8_t seq)
{
    int i;
    int j;
    int found;

    if (ctx != NULL) {
        found = -1;
        for (i = 0; i < (int)ctx->pending_count; i++) {
            if (ctx->pending[i].seq == seq) {
                found = i;
            }
        }
        if (found >= 0) {
            for (j = found; j < (int)ctx->pending_count - 1; j++) {
                ctx->pending[j] = ctx->pending[j + 1];
            }
            ctx->pending_count = (uint8_t)(ctx->pending_count - 1);
        }
    }
}

int comm_next_retransmit(CommCtx *ctx, CommFrame *out)
{
    int rc;

    if (ctx == NULL || out == NULL) {
        rc = COMM_ERR_ARG;
    } else if (ctx->pending_count == 0u) {
        rc = COMM_ERR_EMPTY;
    } else {
        *out = ctx->pending[0];
        ctx->retransmits = (uint8_t)(ctx->retransmits + 1);
        rc = COMM_OK;
    }
    return rc;
}
"""

TEST_COMM_C = r"""#include <stdio.h>
#include <string.h>

#include "comm.h"
#include "wb_harness.h"

static const uint8_t kCheck[9] = { '1','2','3','4','5','6','7','8','9' };

static int pack_ok(CommCtx *ctx, const uint8_t *payload, int len,
                   uint8_t *out, int cap)
{
    return comm_pack(ctx, payload, len, out, cap);
}

int main(void)
{
    CommCtx tx;
    CommCtx rx;
    CommFrame frame;
    uint8_t raw[COMM_FRAME_MAX];
    uint8_t payload[COMM_MAX_PAYLOAD];
    int i;
    int rc;

    wb_begin();

    /* TC-001 CRC-16/CCITT-FALSE 标准校验向量 */
    WB_CHECK("TC-001", comm_crc16(kCheck, 9) == 0x29B1u,
             "crc16(\"123456789\")=0x%04X 期望 0x29B1", comm_crc16(kCheck, 9));

    /* TC-002 空输入与空指针的 CRC 边界 */
    comm_init(&tx);
    WB_CHECK("TC-002", comm_crc16(NULL, 0) == 0xFFFFu
             && comm_crc16(kCheck, 0) == 0xFFFFu && comm_crc16(kCheck, -1) == 0xFFFFu,
             "空输入应返回初值 0xFFFF");

    /* TC-003 正常成帧 */
    comm_init(&tx);
    memset(payload, 0x5A, sizeof(payload));
    memset(raw, 0, sizeof(raw));
    rc = pack_ok(&tx, payload, 8, raw, (int)sizeof(raw));
    WB_CHECK("TC-003", rc == COMM_OK && raw[0] == COMM_HEAD && raw[1] == 0u
             && raw[2] == 8u && raw[3] == 0x5Au && tx.pending_count == 1u
             && tx.tx_seq == 1u,
             "成帧失败 rc=%d head=0x%02X seq=%u len=%u", rc, raw[0], raw[1], raw[2]);

    /* TC-004 载荷超长应被拒绝 */
    comm_init(&tx);
    rc = pack_ok(&tx, payload, COMM_MAX_PAYLOAD + 1, raw, (int)sizeof(raw));
    WB_CHECK("TC-004", rc == COMM_ERR_LEN && tx.pending_count == 0u,
             "超长载荷应返回 COMM_ERR_LEN，实际 %d", rc);

    /* TC-005 非法参数 */
    comm_init(&tx);
    rc = pack_ok(NULL, payload, 4, raw, (int)sizeof(raw));
    WB_CHECK("TC-005", rc == COMM_ERR_ARG
             && pack_ok(&tx, payload, 4, NULL, 0) == COMM_ERR_ARG
             && pack_ok(&tx, NULL, 4, raw, (int)sizeof(raw)) == COMM_ERR_ARG,
             "非法参数应返回 COMM_ERR_ARG，实际 %d", rc);

    /* TC-006 输出缓冲不足 */
    comm_init(&tx);
    rc = pack_ok(&tx, payload, 8, raw, 8);
    WB_CHECK("TC-006", rc == COMM_ERR_LEN && tx.pending_count == 0u,
             "缓冲不足应返回 COMM_ERR_LEN，实际 %d", rc);

    /* TC-007 收发回环 */
    comm_init(&tx);
    comm_init(&rx);
    memset(raw, 0, sizeof(raw));
    for (i = 0; i < 8; i++) {
        payload[i] = (uint8_t)(0x10 + i);
    }
    rc = pack_ok(&tx, payload, 8, raw, (int)sizeof(raw));
    memset(&frame, 0, sizeof(frame));
    WB_CHECK("TC-007", rc == COMM_OK
             && comm_unpack(&rx, raw, 13, &frame) == COMM_OK
             && frame.seq == 0u && frame.len == 8u
             && memcmp(frame.payload, payload, 8) == 0
             && rx.rx_seq == 1u,
             "回环解帧失败 seq=%u len=%u rx_seq=%u", frame.seq, frame.len, rx.rx_seq);

    /* TC-008 CRC 损坏必须被检出 */
    comm_init(&rx);
    raw[4] = (uint8_t)(raw[4] ^ 0xFFu);
    rc = comm_unpack(&rx, raw, 13, &frame);
    WB_CHECK("TC-008", rc == COMM_ERR_CRC && rx.rx_seq == 0u,
             "CRC 损坏应返回 COMM_ERR_CRC，实际 %d", rc);

    /* TC-009 帧头错误与序号错位 */
    comm_init(&tx);
    comm_init(&rx);
    memset(raw, 0, sizeof(raw));
    (void)pack_ok(&tx, payload, 4, raw, (int)sizeof(raw));
    raw[0] = 0x00;
    rc = comm_unpack(&rx, raw, 9, &frame);
    raw[0] = COMM_HEAD;
    rx.rx_seq = 7u;
    WB_CHECK("TC-009", rc == COMM_ERR_ARG
             && comm_unpack(&rx, raw, 9, &frame) == COMM_ERR_SEQ,
             "帧头/序号校验失效 rc=%d", rc);

    /* TC-010 重传窗口满 */
    comm_init(&tx);
    for (i = 0; i < COMM_WINDOW; i++) {
        (void)pack_ok(&tx, payload, 4, raw, (int)sizeof(raw));
    }
    rc = pack_ok(&tx, payload, 4, raw, (int)sizeof(raw));
    WB_CHECK("TC-010", rc == COMM_ERR_FULL && tx.pending_count == COMM_WINDOW,
             "窗口满应返回 COMM_ERR_FULL，实际 %d count=%u", rc, tx.pending_count);

    /* TC-011 确认释放窗口 */
    comm_ack(&tx, 1u);
    WB_CHECK("TC-011", tx.pending_count == (uint8_t)(COMM_WINDOW - 1)
             && tx.pending[1].seq == 2u
             && pack_ok(&tx, payload, 4, raw, (int)sizeof(raw)) == COMM_OK,
             "确认后窗口未正确前移 count=%u", tx.pending_count);

    /* TC-012 重传取最旧未确认帧 */
    comm_init(&tx);
    memset(payload, 0x5A, sizeof(payload));
    (void)pack_ok(&tx, payload, 4, raw, (int)sizeof(raw));
    payload[0] = 0x7E;
    (void)pack_ok(&tx, payload, 4, raw, (int)sizeof(raw));
    memset(&frame, 0, sizeof(frame));
    rc = comm_next_retransmit(&tx, &frame);
    WB_CHECK("TC-012", rc == COMM_OK && frame.seq == 0u && tx.retransmits == 1u
             && frame.payload[0] == 0x5Au,
             "重传帧不是最旧未确认帧 rc=%d seq=%u", rc, frame.seq);

    /* TC-013 无待确认帧时的重传 */
    comm_init(&tx);
    rc = comm_next_retransmit(&tx, &frame);
    WB_CHECK("TC-013", rc == COMM_ERR_EMPTY
             && comm_next_retransmit(NULL, &frame) == COMM_ERR_ARG,
             "空窗口应返回 COMM_ERR_EMPTY，实际 %d", rc);

    /* TC-014 初始化与空指针健壮性 */
    comm_init(NULL);
    comm_ack(NULL, 0u);
    comm_init(&tx);
    WB_CHECK("TC-014", tx.tx_seq == 0u && tx.rx_seq == 0u
             && tx.pending_count == 0u && tx.retransmits == 0u,
             "初始化后状态非零 seq=%u count=%u", tx.tx_seq, tx.pending_count);

    /* TC-015 负长度载荷：pack_check 的 len < 0 分支 */
    comm_init(&tx);
    rc = pack_ok(&tx, payload, -1, raw, (int)sizeof(raw));
    WB_CHECK("TC-015", rc == COMM_ERR_LEN && tx.pending_count == 0u,
             "负长度应返回 COMM_ERR_LEN，实际 %d", rc);

    /* TC-016 解帧入参防护：unpack_check 的四条真分支 */
    comm_init(&rx);
    rc = comm_unpack(NULL, raw, 13, &frame);
    WB_CHECK("TC-016", rc == COMM_ERR_ARG
             && comm_unpack(&rx, NULL, 13, &frame) == COMM_ERR_ARG
             && comm_unpack(&rx, raw, 13, NULL) == COMM_ERR_ARG
             && comm_unpack(&rx, raw, COMM_FRAME_OVERHEAD - 1, &frame) == COMM_ERR_LEN
             && rx.rx_seq == 0u,
             "解帧入参防护失效 rc=%d rx_seq=%u", rc, rx.rx_seq);

    /* TC-017 声明长度超出实际缓冲：截断帧必须被拒绝 */
    comm_init(&tx);
    comm_init(&rx);
    memset(raw, 0, sizeof(raw));
    rc = pack_ok(&tx, payload, 8, raw, (int)sizeof(raw));
    WB_CHECK("TC-017", rc == COMM_OK
             && comm_unpack(&rx, raw, 10, &frame) == COMM_ERR_LEN
             && rx.rx_seq == 0u,
             "截断帧应返回 COMM_ERR_LEN，实际 rx_seq=%u", rx.rx_seq);

    /* TC-018 确认一个不存在的序号：窗口不得前移 */
    comm_init(&tx);
    (void)pack_ok(&tx, payload, 4, raw, (int)sizeof(raw));
    comm_ack(&tx, 9u);
    WB_CHECK("TC-018", tx.pending_count == 1u && tx.pending[0].seq == 0u,
             "确认未知序号不应改变窗口 count=%u", tx.pending_count);

    /* TC-019 重传出参为空指针 */
    comm_init(&tx);
    (void)pack_ok(&tx, payload, 4, raw, (int)sizeof(raw));
    rc = comm_next_retransmit(&tx, NULL);
    WB_CHECK("TC-019", rc == COMM_ERR_ARG && tx.retransmits == 0u,
             "重传出参为空应返回 COMM_ERR_ARG，实际 %d", rc);

    return wb_summary() == 0 ? 0 : 1;
}
"""

CODE_FILES = {"include/comm.h": COMM_H, "src/comm.c": COMM_C}
TEST_FILES = {"tests/test_comm.c": TEST_COMM_C}

# 详细设计 functions 清单（样例模块的设计基线，用于 WB-D-001/002 判定）
LLD_FUNCTIONS = [
    {"name": "comm_init", "sig": "void comm_init(CommCtx *ctx)", "desc": "初始化通信上下文"},
    {"name": "comm_crc16", "sig": "uint16_t comm_crc16(const uint8_t *data, int len)",
     "desc": "CRC-16/CCITT-FALSE 校验"},
    {"name": "comm_pack",
     "sig": "int comm_pack(CommCtx *ctx, const uint8_t *payload, int len, uint8_t *out, int out_cap)",
     "desc": "成帧并登记到重传窗口"},
    {"name": "comm_unpack",
     "sig": "int comm_unpack(CommCtx *ctx, const uint8_t *raw, int raw_len, CommFrame *out)",
     "desc": "解帧并校验 CRC 与序号"},
    {"name": "comm_ack", "sig": "void comm_ack(CommCtx *ctx, uint8_t seq)",
     "desc": "确认并释放重传窗口"},
    {"name": "comm_next_retransmit",
     "sig": "int comm_next_retransmit(CommCtx *ctx, CommFrame *out)",
     "desc": "取出最旧的未确认帧用于重传"},
]

# 用例 → 需求编号：test_impl 阶段的 meta.cases 就按这个填，
# 追溯矩阵「执行结果 / 分支覆盖」两列靠它把覆盖率归到具体 FR 上。
CASE_FR = [
    ("TC-001", "comm_crc16", ["FR-002"]),
    ("TC-002", "comm_crc16", ["FR-002"]),
    ("TC-003", "comm_pack", ["FR-001"]),
    ("TC-004", "comm_pack", ["FR-001", "FR-005"]),
    ("TC-005", "comm_pack", ["FR-005"]),
    ("TC-006", "comm_pack", ["FR-005"]),
    ("TC-007", "comm_unpack", ["FR-003"]),
    ("TC-008", "comm_unpack", ["FR-003"]),
    ("TC-009", "comm_unpack", ["FR-003", "FR-005"]),
    ("TC-010", "comm_pack", ["FR-004"]),
    ("TC-011", "comm_ack", ["FR-004"]),
    ("TC-012", "comm_next_retransmit", ["FR-006"]),
    ("TC-013", "comm_next_retransmit", ["FR-006", "FR-005"]),
    ("TC-014", "comm_init", ["FR-005"]),
    ("TC-015", "comm_pack", ["FR-005"]),
    ("TC-016", "comm_unpack", ["FR-005"]),
    ("TC-017", "comm_unpack", ["FR-003", "FR-005"]),
    ("TC-018", "comm_ack", ["FR-004"]),
    ("TC-019", "comm_next_retransmit", ["FR-005"]),
]

CASE_IDS = [c[0] for c in CASE_FR]
