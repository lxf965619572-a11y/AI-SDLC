"""离线演示与单测共用的固定 C 产物：订单模块（受限子集合格样本）。

为什么单独一个模块：mock_llm 负责文档模板，C 源码是另一种东西——它要能被 gcc
编译、被 core.c_static 判为零必查项违规、被 gcov 采出覆盖率。放在这里，
「离线演示跑的代码」与「单测里的基准代码」就是同一份，不会各写一套而漂移。

业务语义沿用 mock 详细设计里的订单/支付，但落成嵌入式形态：运行期状态全部收在
显式传入的 OrderCtx，库存与会员等级是只读常量表；没有动态内存、没有递归、
没有函数指针、没有文件作用域可写变量，每个函数单出口且圈复杂度远低于上限。
测试只依赖头文件（黑盒）：库存基线用 stock_of() 现场读取，不写死实现侧的数字。
"""
from __future__ import annotations

from core import c_files

# ---- 详细设计基线：mock 的 lld 分支与代码/测试实现分支共用这一份 ----
ORDER_FUNCTIONS = [
    {"name": "order_init",
     "sig": "void order_init(OrderCtx *ctx, uint32_t now_minute)",
     "desc": "初始化订单上下文：清空订单表、装载库存基线、设定当前时刻"},
    {"name": "order_create",
     "sig": "int order_create(OrderCtx *ctx, uint32_t user_id, const OrderItem *items, size_t n_items, Order *out)",
     "desc": "校验条目/金额/库存，按会员等级折算金额，生成订单并扣减库存"},
    {"name": "order_pay",
     "sig": "PayResult order_pay(OrderCtx *ctx, const char *order_no, const char *channel)",
     "desc": "支付订单；超过 30 分钟未支付则自动取消并释放库存"},
]

ORDER_DATA_STRUCTURES = ["OrderItem", "Order", "PayResult", "InventorySlot", "OrderCtx"]

ORDER_H = r"""#ifndef ORDER_H
#define ORDER_H

#include <stdint.h>
#include <stddef.h>

/* 容量与限额：全部编译期常量，运行期不得改写（WB-C-004 / WB-C-007） */
#define ORDER_MAX_ITEMS 8
#define ORDER_MAX_ORDERS 4
#define ORDER_STOCK_KINDS 4
#define ORDER_GOLD_USERS 2
#define ORDER_NO_LEN 15
#define ORDER_TICKET_LEN 12
#define ORDER_AMOUNT_MAX_CENT 5000000
#define ORDER_PAY_TIMEOUT_MIN 30
#define ORDER_DISCOUNT_NUM 95
#define ORDER_DISCOUNT_DEN 100

/* 样例数据基线：商品编号与用户等级是接口约定，库存数量由实现侧只读表提供 */
#define ORDER_SKU_A 9001u
#define ORDER_SKU_B 9002u
#define ORDER_SKU_NONE 9999u
#define ORDER_USER_GOLD 1001u
#define ORDER_USER_NORMAL 2001u

enum {
    ORDER_OK = 0,
    ORDER_ERR_ARG = -1,
    ORDER_ERR_ITEMS = -2,
    ORDER_ERR_AMOUNT = -3,
    ORDER_ERR_STOCK = -4,
    ORDER_ERR_FULL = -5
};

enum {
    ORDER_CREATED = 0,
    ORDER_PAID = 1,
    ORDER_CANCELLED = 2
};

enum {
    PAY_OK = 0,
    PAY_ERR_ARG = -1,
    PAY_NOT_FOUND = -2,
    PAY_TIMEOUT = -3,
    PAY_ALREADY = -4,
    PAY_BAD_CHANNEL = -5
};

enum {
    MEMBER_NORMAL = 0,
    MEMBER_GOLD = 1
};

typedef struct {
    uint32_t sku_id;      /* 商品编号 */
    uint32_t qty;         /* 数量 */
    int64_t  price_cent;  /* 单价（分） */
} OrderItem;

typedef struct {
    char      order_no[ORDER_NO_LEN];  /* 订单号，NUL 结尾 */
    uint32_t  user_id;
    int64_t   amount_cent;             /* 折后应付总额（分） */
    int       status;                  /* ORDER_CREATED / PAID / CANCELLED */
    uint32_t  created_minute;          /* 下单时刻（分钟） */
    size_t    n_items;
    OrderItem items[ORDER_MAX_ITEMS];
} Order;

typedef struct {
    int     rc;                        /* PAY_* */
    int64_t paid_cent;                 /* 实付金额（分），未支付为 0 */
    char    ticket[ORDER_TICKET_LEN];  /* 支付凭证号 */
} PayResult;

typedef struct {
    uint32_t sku_id;
    uint32_t qty;
} InventorySlot;

/* 模块的全部运行期状态。函数不自带隐式全局，状态由调用方持有并显式传入。 */
typedef struct {
    uint32_t      now_minute;
    uint32_t      next_seq;
    size_t        n_orders;
    InventorySlot stock[ORDER_STOCK_KINDS];
    Order         orders[ORDER_MAX_ORDERS];
} OrderCtx;

void order_init(OrderCtx *ctx, uint32_t now_minute);
int order_create(OrderCtx *ctx, uint32_t user_id, const OrderItem *items,
                 size_t n_items, Order *out);
PayResult order_pay(OrderCtx *ctx, const char *order_no, const char *channel);

#endif /* ORDER_H */
"""

ORDER_C = r"""#include "order.h"

#include <string.h>

/* 只读基线数据：金卡名单与初始库存。运行期状态一律在 OrderCtx 里，
   这两张表只提供装载源，因此可以声明为 static const（WB-C-007）。 */
static const uint32_t kGoldUsers[ORDER_GOLD_USERS] = { ORDER_USER_GOLD, 1002u };

static const InventorySlot kInitStock[ORDER_STOCK_KINDS] = {
    { ORDER_SKU_A, 10u },
    { ORDER_SKU_B,  2u },
    { 9003u,       50u },
    { 9004u,        5u }
};

void order_init(OrderCtx *ctx, uint32_t now_minute)
{
    int i;

    if (ctx != NULL) {
        ctx->now_minute = now_minute;
        ctx->next_seq = 1u;
        ctx->n_orders = 0u;
        for (i = 0; i < ORDER_STOCK_KINDS; i++) {
            ctx->stock[i] = kInitStock[i];
        }
    }
}

static int member_level(uint32_t user_id)
{
    int level = MEMBER_NORMAL;
    int i;

    for (i = 0; i < ORDER_GOLD_USERS; i++) {
        if (kGoldUsers[i] == user_id) {
            level = MEMBER_GOLD;
        }
    }
    return level;
}

static int items_check(const OrderItem *items, size_t n_items)
{
    int rc = ORDER_OK;
    size_t i;

    if (items == NULL) {
        rc = ORDER_ERR_ARG;
    } else if (n_items == 0u || n_items > (size_t)ORDER_MAX_ITEMS) {
        rc = ORDER_ERR_ITEMS;
    } else {
        for (i = 0u; i < n_items; i++) {
            if (items[i].qty == 0u || items[i].price_cent < 0) {
                rc = ORDER_ERR_ITEMS;
            }
        }
    }
    return rc;
}

static int64_t items_amount(const OrderItem *items, size_t n_items)
{
    int64_t sum = 0;
    size_t i;

    for (i = 0u; i < n_items; i++) {
        sum += (int64_t)items[i].qty * items[i].price_cent;
    }
    return sum;
}

static int64_t discount_cent(int64_t amount_cent, int level)
{
    int64_t paid = amount_cent;

    if (level == MEMBER_GOLD) {
        paid = (amount_cent * ORDER_DISCOUNT_NUM) / ORDER_DISCOUNT_DEN;
    }
    return paid;
}

static int stock_find(const OrderCtx *ctx, uint32_t sku_id)
{
    int idx = -1;
    int i;

    for (i = 0; i < ORDER_STOCK_KINDS; i++) {
        if (ctx->stock[i].sku_id == sku_id) {
            idx = i;
        }
    }
    return idx;
}

static int stock_check(const OrderCtx *ctx, const OrderItem *items, size_t n_items)
{
    int rc = ORDER_OK;
    size_t i;
    int idx;

    for (i = 0u; i < n_items; i++) {
        idx = stock_find(ctx, items[i].sku_id);
        if (idx < 0 || ctx->stock[idx].qty < items[i].qty) {
            rc = ORDER_ERR_STOCK;      /* 目录外商品按无库存处理 */
        }
    }
    return rc;
}

static void stock_move(OrderCtx *ctx, const OrderItem *items, size_t n_items,
                       int sign)
{
    size_t i;
    int idx;

    for (i = 0u; i < n_items; i++) {
        idx = stock_find(ctx, items[i].sku_id);
        if (idx >= 0) {
            ctx->stock[idx].qty = (uint32_t)((int64_t)ctx->stock[idx].qty
                                             + sign * (int64_t)items[i].qty);
        }
    }
}

static void stock_restore(OrderCtx *ctx, const Order *od)
{
    stock_move(ctx, od->items, od->n_items, 1);
}

static void dec_write(char *dst, int width, uint32_t value)
{
    uint32_t v = value;
    int i;

    for (i = width - 1; i >= 0; i--) {
        dst[i] = (char)('0' + (v % 10u));
        v /= 10u;
    }
}

static void order_no_write(char *dst, uint32_t seq, uint32_t user_id)
{
    dst[0] = 'W';
    dst[1] = 'B';
    dec_write(&dst[2], 8, seq);
    dec_write(&dst[10], 4, user_id % 10000u);
    dst[ORDER_NO_LEN - 1] = '\0';
}

static void ticket_write(char *dst, uint32_t seq)
{
    dst[0] = 'P';
    dst[1] = 'T';
    dst[2] = 'Y';
    dec_write(&dst[3], 8, seq);
    dst[ORDER_TICKET_LEN - 1] = '\0';
}

static void order_fill(Order *dst, uint32_t user_id, const OrderItem *items,
                       size_t n_items, int64_t amount_cent, uint32_t created_minute)
{
    size_t i;

    dst->user_id = user_id;
    dst->amount_cent = amount_cent;
    dst->status = ORDER_CREATED;
    dst->created_minute = created_minute;
    dst->n_items = n_items;
    for (i = 0u; i < n_items; i++) {
        dst->items[i] = items[i];
    }
}

static int create_check(OrderCtx *ctx, uint32_t user_id, const OrderItem *items,
                        size_t n_items, int64_t *amount_out)
{
    int rc;
    int64_t amount = 0;

    if (ctx == NULL) {
        rc = ORDER_ERR_ARG;
    } else if (ctx->n_orders >= (size_t)ORDER_MAX_ORDERS) {
        rc = ORDER_ERR_FULL;
    } else {
        rc = items_check(items, n_items);
    }
    if (rc == ORDER_OK) {
        amount = discount_cent(items_amount(items, n_items), member_level(user_id));
        if (amount <= 0 || amount > (int64_t)ORDER_AMOUNT_MAX_CENT) {
            rc = ORDER_ERR_AMOUNT;
        } else {
            rc = stock_check(ctx, items, n_items);
        }
    }
    if (rc == ORDER_OK) {
        *amount_out = amount;
    }
    return rc;
}

int order_create(OrderCtx *ctx, uint32_t user_id, const OrderItem *items,
                 size_t n_items, Order *out)
{
    int rc;
    int64_t amount = 0;
    Order *slot;

    rc = (out == NULL) ? ORDER_ERR_ARG
                       : create_check(ctx, user_id, items, n_items, &amount);
    if (rc == ORDER_OK) {
        slot = &ctx->orders[ctx->n_orders];
        order_fill(slot, user_id, items, n_items, amount, ctx->now_minute);
        order_no_write(slot->order_no, ctx->next_seq, user_id);
        ctx->next_seq = ctx->next_seq + 1u;
        ctx->n_orders = ctx->n_orders + 1u;
        stock_move(ctx, items, n_items, -1);
        *out = *slot;
    }
    return rc;
}

static int order_find(const OrderCtx *ctx, const char *order_no)
{
    int idx = -1;
    size_t i;

    for (i = 0u; i < ctx->n_orders; i++) {
        if (strcmp(ctx->orders[i].order_no, order_no) == 0) {
            idx = (int)i;
        }
    }
    return idx;
}

static int pay_check(OrderCtx *ctx, const char *order_no, const char *channel,
                     int *idx_out)
{
    int rc;
    int idx = -1;

    if (ctx == NULL || order_no == NULL || channel == NULL) {
        rc = PAY_ERR_ARG;
    } else if (channel[0] == '\0') {
        rc = PAY_BAD_CHANNEL;
    } else {
        idx = order_find(ctx, order_no);
        if (idx < 0) {
            rc = PAY_NOT_FOUND;
        } else if (ctx->orders[idx].status == ORDER_PAID) {
            rc = PAY_ALREADY;
        } else if (ctx->orders[idx].status == ORDER_CANCELLED) {
            rc = PAY_NOT_FOUND;
        } else if (ctx->now_minute - ctx->orders[idx].created_minute
                   >= (uint32_t)ORDER_PAY_TIMEOUT_MIN) {
            ctx->orders[idx].status = ORDER_CANCELLED;
            stock_restore(ctx, &ctx->orders[idx]);
            rc = PAY_TIMEOUT;
        } else {
            *idx_out = idx;
            rc = PAY_OK;
        }
    }
    return rc;
}

PayResult order_pay(OrderCtx *ctx, const char *order_no, const char *channel)
{
    PayResult res;
    int idx = -1;
    int rc;

    ticket_write(res.ticket, 0u);
    res.paid_cent = 0;
    rc = pay_check(ctx, order_no, channel, &idx);
    res.rc = rc;
    if (rc == PAY_OK) {
        ctx->orders[idx].status = ORDER_PAID;
        res.paid_cent = ctx->orders[idx].amount_cent;
        ticket_write(res.ticket, (uint32_t)idx + 1u);
    }
    return res;
}
"""

TEST_ORDER_C = r"""#include <stdio.h>
#include <string.h>

#include "order.h"
#include "wb_harness.h"

/* 黑盒读库存：只依赖头文件里的 OrderCtx 与 ORDER_STOCK_KINDS，
   不把实现侧的库存基线数字写进断言，实现改了数据测试也不会假失败。 */
static uint32_t stock_of(const OrderCtx *ctx, uint32_t sku)
{
    uint32_t qty = 0u;
    int i;

    for (i = 0; i < ORDER_STOCK_KINDS; i++) {
        if (ctx->stock[i].sku_id == sku) {
            qty = ctx->stock[i].qty;
        }
    }
    return qty;
}

static void one_item(OrderItem *items, uint32_t sku, uint32_t qty, int64_t price_cent)
{
    items[0].sku_id = sku;
    items[0].qty = qty;
    items[0].price_cent = price_cent;
}

int main(void)
{
    OrderCtx ctx;
    Order od;
    Order od2;
    OrderItem items[ORDER_MAX_ITEMS];
    OrderItem bad[ORDER_MAX_ITEMS];
    PayResult pr;
    uint32_t base;
    int rc;
    int rc2;
    int rc3;
    int rc4;
    int rc5;

    wb_begin();
    memset(items, 0, sizeof(items));
    memset(bad, 0, sizeof(bad));

    /* TC-001 正常下单成功：可支付、状态转已支付、库存减 1 */
    order_init(&ctx, 0u);
    memset(&od, 0, sizeof(od));
    one_item(items, ORDER_SKU_A, 1u, 10000);
    base = stock_of(&ctx, ORDER_SKU_A);
    rc = order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od);
    pr = order_pay(&ctx, od.order_no, "alipay");
    WB_CHECK("TC-001", rc == ORDER_OK && od.status == ORDER_CREATED
             && ctx.n_orders == 1u && pr.rc == PAY_OK && (int)pr.paid_cent == 10000
             && ctx.orders[0].status == ORDER_PAID
             && stock_of(&ctx, ORDER_SKU_A) == base - 1u,
             "下单支付链路失败 rc=%d pay=%d status=%d 库存=%u 期望=%u",
             rc, pr.rc, ctx.orders[0].status, stock_of(&ctx, ORDER_SKU_A), base - 1u);

    /* TC-002 订单金额为 0：拒绝且不生成订单、不扣库存 */
    order_init(&ctx, 0u);
    base = stock_of(&ctx, ORDER_SKU_A);
    one_item(items, ORDER_SKU_A, 1u, 0);
    rc = order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od);
    WB_CHECK("TC-002", rc == ORDER_ERR_AMOUNT && ctx.n_orders == 0u
             && stock_of(&ctx, ORDER_SKU_A) == base,
             "金额为 0 应返回 ORDER_ERR_AMOUNT，实际 %d，订单数 %u",
             rc, (unsigned)ctx.n_orders);

    /* TC-003 金额超过 50000 元上限 */
    order_init(&ctx, 0u);
    one_item(items, ORDER_SKU_A, 1u, (int64_t)ORDER_AMOUNT_MAX_CENT + 1);
    rc = order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od);
    WB_CHECK("TC-003", rc == ORDER_ERR_AMOUNT && ctx.n_orders == 0u,
             "金额超上限应返回 ORDER_ERR_AMOUNT，实际 %d，订单数 %u",
             rc, (unsigned)ctx.n_orders);

    /* TC-004 库存不足下单：不生成订单、库存不变 */
    order_init(&ctx, 0u);
    base = stock_of(&ctx, ORDER_SKU_B);
    one_item(items, ORDER_SKU_B, base + 1u, 100);
    rc = order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od);
    WB_CHECK("TC-004", rc == ORDER_ERR_STOCK && ctx.n_orders == 0u
             && stock_of(&ctx, ORDER_SKU_B) == base,
             "库存不足应返回 ORDER_ERR_STOCK 且不生成订单，实际 %d，订单数 %u，库存 %u",
             rc, (unsigned)ctx.n_orders, stock_of(&ctx, ORDER_SKU_B));

    /* TC-005 支付超时 30 分钟：订单自动取消，库存释放 */
    order_init(&ctx, 0u);
    one_item(items, ORDER_SKU_A, 1u, 10000);
    base = stock_of(&ctx, ORDER_SKU_A);
    rc = order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od);
    ctx.now_minute = ORDER_PAY_TIMEOUT_MIN;
    pr = order_pay(&ctx, od.order_no, "alipay");
    WB_CHECK("TC-005", rc == ORDER_OK && pr.rc == PAY_TIMEOUT && (int)pr.paid_cent == 0
             && ctx.orders[0].status == ORDER_CANCELLED
             && stock_of(&ctx, ORDER_SKU_A) == base,
             "超时未支付应自动取消并释放库存 pay=%d status=%d 库存=%u 期望=%u",
             pr.rc, ctx.orders[0].status, stock_of(&ctx, ORDER_SKU_A), base);

    /* TC-006 金卡会员 95 折，普通用户不打折 */
    order_init(&ctx, 0u);
    one_item(items, ORDER_SKU_A, 1u, 10000);
    rc = order_create(&ctx, ORDER_USER_GOLD, items, 1u, &od);
    pr = order_pay(&ctx, od.order_no, "wechat");
    rc2 = order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od2);
    WB_CHECK("TC-006", rc == ORDER_OK && (int)od.amount_cent == 9500
             && pr.rc == PAY_OK && (int)pr.paid_cent == 9500
             && rc2 == ORDER_OK && (int)od2.amount_cent == 10000,
             "折扣计算错误 金卡=%d 期望 9500，普通=%d 期望 10000",
             (int)od.amount_cent, (int)od2.amount_cent);

    /* TC-007 入参防护：空指针、非法长度、非法取值一律返回错误码，不得崩溃。
       order_init(NULL) 无返回值可断言，调用本身不崩即为通过（崩了后续用例
       全部记为未执行，报告里会直接看出来）。 */
    order_init(&ctx, 0u);
    order_init(NULL, 0u);
    bad[0].sku_id = ORDER_SKU_A;
    bad[0].qty = 0u;                 /* 数量为 0 */
    bad[0].price_cent = 100;
    bad[1].sku_id = ORDER_SKU_NONE;  /* 目录外商品 */
    bad[1].qty = 1u;
    bad[1].price_cent = -1;          /* 负单价 */
    one_item(items, ORDER_SKU_A, 1u, 10000);
    rc = order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od);
    WB_CHECK("TC-007", rc == ORDER_OK
             && order_create(NULL, ORDER_USER_NORMAL, items, 1u, &od) == ORDER_ERR_ARG
             && order_create(&ctx, ORDER_USER_NORMAL, items, 1u, NULL) == ORDER_ERR_ARG
             && order_create(&ctx, ORDER_USER_NORMAL, NULL, 1u, &od) == ORDER_ERR_ARG
             && order_create(&ctx, ORDER_USER_NORMAL, items, 0u, &od) == ORDER_ERR_ITEMS
             && order_create(&ctx, ORDER_USER_NORMAL, items,
                             (size_t)ORDER_MAX_ITEMS + 1u, &od) == ORDER_ERR_ITEMS
             && order_create(&ctx, ORDER_USER_NORMAL, bad, 1u, &od) == ORDER_ERR_ITEMS
             && order_create(&ctx, ORDER_USER_NORMAL, &bad[1], 1u, &od) == ORDER_ERR_ITEMS
             && order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od) == ORDER_OK
             && order_pay(&ctx, od.order_no, "") == PAY_BAD_CHANNEL
             && order_pay(NULL, od.order_no, "alipay") == PAY_ERR_ARG
             && order_pay(&ctx, NULL, "alipay") == PAY_ERR_ARG
             && order_pay(&ctx, od.order_no, NULL) == PAY_ERR_ARG
             && order_pay(&ctx, "WB999999999999", "alipay") == PAY_NOT_FOUND
             && ctx.n_orders == 2u,
             "入参防护失效 rc=%d 订单数=%u 期望 2", rc, (unsigned)ctx.n_orders);

    /* TC-008 状态机与容量边界：重复支付、超时取消后再支付、订单表满 */
    order_init(&ctx, 0u);
    one_item(items, ORDER_SKU_A, 1u, 10000);
    (void)order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od);
    pr = order_pay(&ctx, od.order_no, "alipay");
    rc2 = order_pay(&ctx, od.order_no, "alipay").rc;          /* 已支付再支付 */
    rc3 = order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od2);
    ctx.now_minute = ORDER_PAY_TIMEOUT_MIN;                   /* 推进 30 分钟 */
    rc4 = order_pay(&ctx, od2.order_no, "alipay").rc;         /* 超时 → 自动取消 */
    rc5 = order_pay(&ctx, od2.order_no, "alipay").rc;         /* 已取消再支付 */
    (void)order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od2);
    (void)order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od2);
    rc = order_create(&ctx, ORDER_USER_NORMAL, items, 1u, &od2);
    WB_CHECK("TC-008", pr.rc == PAY_OK && rc2 == PAY_ALREADY && rc3 == ORDER_OK
             && rc4 == PAY_TIMEOUT && rc5 == PAY_NOT_FOUND
             && ctx.n_orders == (size_t)ORDER_MAX_ORDERS && rc == ORDER_ERR_FULL,
             "状态机/容量边界失效 重复支付=%d 超时=%d 取消后=%d 订单数=%u 满表=%d",
             rc2, rc4, rc5, (unsigned)ctx.n_orders, rc);

    return wb_summary() == 0 ? 0 : 1;
}
"""

CODE_FILES = {"include/order.h": ORDER_H, "src/order.c": ORDER_C}
TEST_FILES = {"tests/test_order.c": TEST_ORDER_C}

# 用例 → 被测函数：test_impl 元数据的 fn 列按这个填。
CASE_FN = {
    "TC-001": "order_pay",
    "TC-002": "order_create",
    "TC-003": "order_create",
    "TC-004": "order_create",
    "TC-005": "order_pay",
    "TC-006": "order_create",
    "TC-007": "order_create",
    "TC-008": "order_pay",
}
DEFAULT_FN = "order_create"


def struct_block() -> str:
    """ORDER_H 里的数据结构定义段（OrderItem → OrderCtx）。

    详细设计文档直接引用它，而不是另抄一份 struct：文档与头文件各写一套时，
    字段名或数组长度的漂移要到代码阶段才暴露，而那时已经过了评审门。"""
    lines = ORDER_H.splitlines()
    start = end = None
    for i, ln in enumerate(lines):
        if start is None and ln.startswith("typedef struct {"):
            start = i
        if ln.startswith("} OrderCtx;"):
            end = i
    if start is None or end is None or end <= start:
        return ORDER_H.strip()
    return "\n".join(lines[start:end + 1])


def spread(items: list, n: int) -> list:
    """把 items 轮流分配到 n 个桶，保证每个 item 至少落一个桶。"""
    buckets: list = [[] for _ in range(max(n, 1))]
    for i, it in enumerate(items):
        b = buckets[i % len(buckets)]
        if it not in b:
            b.append(it)
    return buckets[:n]


def derived_from(frs: list) -> dict:
    """函数 → 需求编号。详细设计的实现对照表与代码元数据共用这一份分配，
    否则追溯矩阵的「详细设计」列与「代码单元」列会各说一套。"""
    names = [f["name"] for f in ORDER_FUNCTIONS]
    buckets = spread(list(frs or []), len(names))
    return {names[i]: buckets[i] for i in range(len(names))}


def code_markdown(frs: list, derived: dict | None = None) -> str:
    """代码阶段的 mock 产物：固定源码 + 元数据（files / derived_from）。

    derived 给定时（来自详细设计的实现对照表）原样沿用：代码是设计的落地，
    追溯分配不该在实现环节重新洗牌，否则矩阵的「详细设计」列与「代码单元」列
    会各自指向不同需求。缺键或键名不符时退回按 frs 重新分配。"""
    import json

    intro = [
        "按《详细设计说明书》实现订单模块，源码落在受限 C 子集内：",
        "",
        "- 运行期状态全部收在调用方传入的 `OrderCtx`，无文件作用域可写变量；",
        "- 库存基线与金卡名单是 `static const` 只读表，无动态内存、无递归、无函数指针；",
        "- 数组维度全部是头文件里的编译期常量，无变长数组；",
        "- 每个函数单出口，圈复杂度最高 8（上限见规则表 WB-C-006）；",
        "- 对外接口只有详细设计列出的 3 个函数，内部辅助函数一律 `static`。",
    ]
    names = {f["name"] for f in ORDER_FUNCTIONS}
    given = {str(k).strip(): list(v or [])
             for k, v in (derived or {}).items() if str(k).strip() in names}
    meta = {"module": "order", "files": c_files.file_list(CODE_FILES),
            "derived_from": given if set(given) == names else derived_from(frs)}
    body = c_files.render_files(CODE_FILES, title="代码实现", intro=intro)
    return body + "\n```json\n" + json.dumps(meta, ensure_ascii=False) + "\n```\n"


def test_impl_markdown(case_frs: dict) -> str:
    """测试实现阶段的 mock 产物：固定测试源码 + 元数据（files / cases）。

    case_frs: {用例编号: [需求编号]}，来自提示词里的用例设计表；
    设计里没出现的编号不会被登记，避免 mock 凭空造出用例。"""
    import json

    import re

    ids = sorted(case_frs or {})
    present = set(re.findall(r'"(TC-\d+)"', TEST_ORDER_C))
    cases = [{"id": cid, "fn": CASE_FN.get(cid, DEFAULT_FN),
              "fr_ids": list(case_frs.get(cid) or [])}
             for cid in ids if cid in present]
    intro = [
        "把《测试用例设计》逐条翻译成可执行 C 测试，判据取自设计里的「预期结果」：",
        "",
        "- 只包含被测模块头文件与测试桩，不读实现，实现有缺陷时用例必须失败；",
        "- 库存基线用 `stock_of()` 现场读取，断言不写死实现侧数字；",
        "- 每条用例前重新 `order_init()`，用例之间不互相污染。",
    ]
    meta = {"files": c_files.file_list(TEST_FILES), "cases": cases}
    body = c_files.render_files(TEST_FILES, title="测试实现", intro=intro)
    return body + "\n```json\n" + json.dumps(meta, ensure_ascii=False) + "\n```\n"


def attribution_markdown(decision: str = "fix_code") -> str:
    """失败归因的 mock 结论：只在 exec 未通过时被调用，判为代码缺陷走重生。

    离线演示没有真实工具链事实，这里给出的是「按保守原则判给代码」的结论——
    与 attribution 智能体的系统提示一致：拿不准时倾向判为代码缺陷。"""
    import json

    meta = {
        "decision": decision,
        "reason": "离线演示模式：无真实工具链事实，按保守原则判为被测代码缺陷",
        "evidence": ["mock 模式下 exec 结论来自内置脚本，不具备定位到行的事实依据"],
        "hint": ["核对 src/order.c 中与失败用例相关的分支判据是否与详细设计一致"],
    }
    return "```json\n" + json.dumps(meta, ensure_ascii=False) + "\n```\n"
