// core/utils/ogg_opus_native.c

#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>

#include <ogg/ogg.h>
#include <opus/opus.h>

#define OGG_NATIVE_OK 0
#define OGG_NATIVE_ERR -1

#define MAX_FRAME_SIZE 5760   // 120ms @ 48kHz
#define ERRMSG_SIZE 256

typedef struct {
    ogg_sync_state oy;

    ogg_stream_state os;
    int stream_inited;
    int stream_serialno;

    int headers_done;
    int eos_seen;

    OpusDecoder *decoder;
    int decoder_channels;

    unsigned char *out_pcm;
    size_t out_pcm_len;
    size_t out_pcm_cap;

    char last_error[ERRMSG_SIZE];
} ogg_opus_native_t;

static void set_error(ogg_opus_native_t *ctx, const char *msg) {
    if (!ctx) return;
    snprintf(ctx->last_error, ERRMSG_SIZE, "%s", msg ? msg : "unknown error");
}

static void clear_error(ogg_opus_native_t *ctx) {
    if (!ctx) return;
    ctx->last_error[0] = '\0';
}

static int ensure_out_capacity(ogg_opus_native_t *ctx, size_t need_extra) {
    if (!ctx) return OGG_NATIVE_ERR;

    size_t need = ctx->out_pcm_len + need_extra;
    if (need <= ctx->out_pcm_cap) return OGG_NATIVE_OK;

    size_t new_cap = ctx->out_pcm_cap == 0 ? 8192 : ctx->out_pcm_cap;
    while (new_cap < need) {
        new_cap *= 2;
    }

    unsigned char *new_buf = (unsigned char *)realloc(ctx->out_pcm, new_cap);
    if (!new_buf) {
        set_error(ctx, "realloc out_pcm failed");
        return OGG_NATIVE_ERR;
    }

    ctx->out_pcm = new_buf;
    ctx->out_pcm_cap = new_cap;
    return OGG_NATIVE_OK;
}

static int append_pcm(ogg_opus_native_t *ctx, const unsigned char *data, size_t len) {
    if (!ctx || !data || len == 0) return OGG_NATIVE_OK;

    if (ensure_out_capacity(ctx, len) != OGG_NATIVE_OK) {
        return OGG_NATIVE_ERR;
    }

    memcpy(ctx->out_pcm + ctx->out_pcm_len, data, len);
    ctx->out_pcm_len += len;
    return OGG_NATIVE_OK;
}

static int is_opus_head_packet(const unsigned char *packet, long bytes) {
    return bytes >= 8 && memcmp(packet, "OpusHead", 8) == 0;
}

static int is_opus_tags_packet(const unsigned char *packet, long bytes) {
    return bytes >= 8 && memcmp(packet, "OpusTags", 8) == 0;
}

static int ensure_decoder_from_packet(ogg_opus_native_t *ctx, const unsigned char *packet, long bytes) {
    if (!ctx || !packet || bytes <= 0) return OGG_NATIVE_ERR;
    if (ctx->decoder) return OGG_NATIVE_OK;

    int channels = opus_packet_get_nb_channels(packet);
    if (channels != 1 && channels != 2) {
        set_error(ctx, "invalid opus packet channel count");
        return OGG_NATIVE_ERR;
    }

    int opus_err = OPUS_OK;
    ctx->decoder = opus_decoder_create(48000, channels, &opus_err);
    if (opus_err != OPUS_OK || !ctx->decoder) {
        set_error(ctx, "opus_decoder_create failed");
        return OGG_NATIVE_ERR;
    }

    ctx->decoder_channels = channels;
    return OGG_NATIVE_OK;
}

static int handle_packet(ogg_opus_native_t *ctx, ogg_packet *op) {
    if (!ctx || !op) return OGG_NATIVE_ERR;

    if (op->bytes <= 0 || !op->packet) {
        return OGG_NATIVE_OK;
    }

    // 跳过 Ogg Opus 头
    if (!ctx->headers_done) {
        if (is_opus_head_packet(op->packet, op->bytes)) {
            return OGG_NATIVE_OK;
        }
        if (is_opus_tags_packet(op->packet, op->bytes)) {
            ctx->headers_done = 1;
            return OGG_NATIVE_OK;
        }

        // 有些流可能不会严格按预期来，遇到第一个非头包时也认为头结束
        ctx->headers_done = 1;
    }

    if (ensure_decoder_from_packet(ctx, op->packet, op->bytes) != OGG_NATIVE_OK) {
        return OGG_NATIVE_ERR;
    }

    opus_int16 pcm[MAX_FRAME_SIZE * 2];  // 最多双声道
    int decoded_samples = opus_decode(
        ctx->decoder,
        op->packet,
        (opus_int32)op->bytes,
        pcm,
        MAX_FRAME_SIZE,
        0
    );

    if (decoded_samples < 0) {
        set_error(ctx, "opus_decode failed");
        return OGG_NATIVE_ERR;
    }

    size_t pcm_bytes = (size_t)decoded_samples * (size_t)ctx->decoder_channels * sizeof(opus_int16);
    if (append_pcm(ctx, (const unsigned char *)pcm, pcm_bytes) != OGG_NATIVE_OK) {
        return OGG_NATIVE_ERR;
    }

    return OGG_NATIVE_OK;
}

static int drain_pages_and_packets(ogg_opus_native_t *ctx) {
    if (!ctx) return OGG_NATIVE_ERR;

    ogg_page og;
    while (1) {
        int pageout_ret = ogg_sync_pageout(&ctx->oy, &og);

        if (pageout_ret == 0) {
            // 当前还没有完整 page
            break;
        }

        if (pageout_ret < 0) {
            // 脏数据 / 同步丢失，继续尝试找下一个 page
            continue;
        }

        int serialno = ogg_page_serialno(&og);

        if (!ctx->stream_inited) {
            if (ogg_stream_init(&ctx->os, serialno) != 0) {
                set_error(ctx, "ogg_stream_init failed");
                return OGG_NATIVE_ERR;
            }
            ctx->stream_inited = 1;
            ctx->stream_serialno = serialno;
        } else if (serialno != ctx->stream_serialno) {
            // 只处理第一条逻辑流，其它流忽略
            continue;
        }

        if (ogg_stream_pagein(&ctx->os, &og) != 0) {
            set_error(ctx, "ogg_stream_pagein failed");
            return OGG_NATIVE_ERR;
        }

        ogg_packet op;
        while (1) {
            int packetout_ret = ogg_stream_packetout(&ctx->os, &op);

            if (packetout_ret == 0) {
                break;
            }

            if (packetout_ret < 0) {
                // packet 存在间隙/损坏，跳过继续
                continue;
            }

            if (handle_packet(ctx, &op) != OGG_NATIVE_OK) {
                return OGG_NATIVE_ERR;
            }
        }

        if (ogg_page_eos(&og)) {
            ctx->eos_seen = 1;
        }
    }

    return OGG_NATIVE_OK;
}

ogg_opus_native_t *ogg_opus_native_create(void) {
    ogg_opus_native_t *ctx = (ogg_opus_native_t *)calloc(1, sizeof(ogg_opus_native_t));
    if (!ctx) return NULL;

    if (ogg_sync_init(&ctx->oy) != 0) {
        free(ctx);
        return NULL;
    }

    ctx->stream_inited = 0;
    ctx->stream_serialno = 0;
    ctx->headers_done = 0;
    ctx->eos_seen = 0;

    ctx->decoder = NULL;
    ctx->decoder_channels = 0;

    ctx->out_pcm = NULL;
    ctx->out_pcm_len = 0;
    ctx->out_pcm_cap = 0;

    clear_error(ctx);
    return ctx;
}

void ogg_opus_native_destroy(ogg_opus_native_t *ctx) {
    if (!ctx) return;

    if (ctx->decoder) {
        opus_decoder_destroy(ctx->decoder);
        ctx->decoder = NULL;
    }

    if (ctx->stream_inited) {
        ogg_stream_clear(&ctx->os);
        ctx->stream_inited = 0;
    }

    ogg_sync_clear(&ctx->oy);

    if (ctx->out_pcm) {
        free(ctx->out_pcm);
        ctx->out_pcm = NULL;
    }

    free(ctx);
}

void ogg_opus_native_reset(ogg_opus_native_t *ctx) {
    if (!ctx) return;

    if (ctx->decoder) {
        opus_decoder_destroy(ctx->decoder);
        ctx->decoder = NULL;
    }
    ctx->decoder_channels = 0;

    if (ctx->stream_inited) {
        ogg_stream_clear(&ctx->os);
        ctx->stream_inited = 0;
    }
    ctx->stream_serialno = 0;

    ogg_sync_clear(&ctx->oy);
    ogg_sync_init(&ctx->oy);

    ctx->headers_done = 0;
    ctx->eos_seen = 0;

    ctx->out_pcm_len = 0;
    clear_error(ctx);
}

void ogg_opus_native_clear_output(ogg_opus_native_t *ctx) {
    if (!ctx) return;
    ctx->out_pcm_len = 0;
}

const unsigned char *ogg_opus_native_get_pcm_ptr(ogg_opus_native_t *ctx) {
    if (!ctx || ctx->out_pcm_len == 0) return NULL;
    return ctx->out_pcm;
}

size_t ogg_opus_native_get_pcm_len(ogg_opus_native_t *ctx) {
    if (!ctx) return 0;
    return ctx->out_pcm_len;
}

const char *ogg_opus_native_get_last_error(ogg_opus_native_t *ctx) {
    if (!ctx) return "ctx is NULL";
    return ctx->last_error;
}

int ogg_opus_native_feed(ogg_opus_native_t *ctx, const unsigned char *data, size_t len) {
    if (!ctx) return OGG_NATIVE_ERR;
    clear_error(ctx);
    ctx->out_pcm_len = 0;

    if (!data || len == 0) {
        return OGG_NATIVE_OK;
    }

    char *sync_buf = ogg_sync_buffer(&ctx->oy, (long)len);
    if (!sync_buf) {
        set_error(ctx, "ogg_sync_buffer failed");
        return OGG_NATIVE_ERR;
    }

    memcpy(sync_buf, data, len);

    int wrote_ret = ogg_sync_wrote(&ctx->oy, (long)len);
    if (wrote_ret != 0) {
        set_error(ctx, "ogg_sync_wrote failed");
        return OGG_NATIVE_ERR;
    }

    if (drain_pages_and_packets(ctx) != OGG_NATIVE_OK) {
        return OGG_NATIVE_ERR;
    }

    return OGG_NATIVE_OK;
}

int ogg_opus_native_flush(ogg_opus_native_t *ctx) {
    if (!ctx) return OGG_NATIVE_ERR;
    clear_error(ctx);
    ctx->out_pcm_len = 0;

    // flush 阶段不追加新数据，只再尝试把当前 sync/stream 里还能取出的 page/packet 榨干
    if (drain_pages_and_packets(ctx) != OGG_NATIVE_OK) {
        return OGG_NATIVE_ERR;
    }

    return OGG_NATIVE_OK;
}

int ogg_opus_native_get_decoder_channels(ogg_opus_native_t *ctx) {
    if (!ctx) return 0;
    return ctx->decoder_channels;
}

int ogg_opus_native_is_eos(ogg_opus_native_t *ctx) {
    if (!ctx) return 0;
    return ctx->eos_seen;
}