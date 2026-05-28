/*
 * qsu — qdistro sudo replacement (C client, v1 non-pty).
 *
 * Compiled binary that connects to qdistro-root-exec over AF_UNIX,
 * sends a JSON request, and streams stdout/stderr back to the caller.
 *
 * The whole point of this binary (vs. the old bash→python wrapper) is
 * that /proc/<pid>/exe resolves to /usr/local/bin/qsu for the entire
 * lifetime of the connection, which gives qdistro-root-exec an
 * unambiguous caller_exe for audit and forensic correlation.
 *
 * Protocol (newline-delimited JSON, one frame per line):
 *   C → S  {"target_user":"root","argv":["id"],"caller_name":"qsu"}
 *   S → C  {"type":"stdout","data":"uid=0(root) ..."}
 *   S → C  {"type":"stderr","data":"..."}
 *   S → C  {"type":"exit","code":0}
 *   S → C  {"type":"error","message":"Request denied."}
 *
 * Build:  cc -O2 -Wall -Wextra -o qsu qsu.c
 * Install: install -m 0755 qsu /usr/local/bin/qsu
 */

#define _GNU_SOURCE
#include <errno.h>
#include <getopt.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define SOCKET_PATH "/run/qdistro-root-exec/sock"

/* Maximum single recv chunk. Generous for line-buffered JSON frames. */
#define RECV_BUF 8192

/* Initial capacity for the dynamic line buffer that accumulates
 * partial reads between newlines. Grown as needed up to LINE_BUF_MAX. */
#define LINE_BUF_INIT 4096

/* Hard cap on the line buffer to prevent a misbehaving server from
 * exhausting client memory. Matches the server's MAX_REQUEST_BYTES. */
#define LINE_BUF_MAX (1 * 1024 * 1024)

/* ------------------------------------------------------------------ */
/* JSON helpers — minimal, no library dependency.                      */
/* ------------------------------------------------------------------ */

/*
 * Append a JSON-escaped version of `src` into `dst` starting at *pos.
 * `cap` is the total capacity of dst. Returns 0 on success, -1 if the
 * buffer would overflow.
 *
 * Escapes: \ → \\, " → \", control chars (0x00–0x1f) → \u00XX.
 */
static int json_escape_into(char *dst, size_t cap, size_t *pos,
                            const char *src)
{
    for (; *src; src++) {
        unsigned char c = (unsigned char)*src;
        if (c == '"' || c == '\\') {
            if (*pos + 2 > cap) return -1;
            dst[(*pos)++] = '\\';
            dst[(*pos)++] = (char)c;
        } else if (c < 0x20) {
            if (*pos + 6 > cap) return -1;
            int n = snprintf(dst + *pos, cap - *pos, "\\u%04x", c);
            if (n < 0 || (size_t)n >= cap - *pos) return -1;
            *pos += (size_t)n;
        } else {
            if (*pos + 1 > cap) return -1;
            dst[(*pos)++] = (char)c;
        }
    }
    return 0;
}

/*
 * Build the request JSON into `buf` (capacity `cap`). Returns the
 * number of bytes written (excluding the NUL terminator), or -1 on
 * overflow.
 *
 * Output: {"target_user":"<user>","argv":[<elements>],"caller_name":"qsu"}\n
 */
static int build_request(char *buf, size_t cap,
                         const char *target_user,
                         int argc, char **argv)
{
    size_t pos = 0;

#define APPEND_LIT(s) do {                          \
        size_t _len = sizeof(s) - 1;                \
        if (pos + _len > cap) return -1;            \
        memcpy(buf + pos, (s), _len); pos += _len;  \
    } while (0)

    APPEND_LIT("{\"target_user\":\"");
    if (json_escape_into(buf, cap, &pos, target_user) < 0) return -1;
    APPEND_LIT("\",\"argv\":[");
    for (int i = 0; i < argc; i++) {
        if (i > 0) {
            if (pos + 1 > cap) return -1;
            buf[pos++] = ',';
        }
        if (pos + 1 > cap) return -1;
        buf[pos++] = '"';
        if (json_escape_into(buf, cap, &pos, argv[i]) < 0) return -1;
        if (pos + 1 > cap) return -1;
        buf[pos++] = '"';
    }
    APPEND_LIT("],\"caller_name\":\"qsu\"}\n");

#undef APPEND_LIT

    if (pos >= cap) return -1;
    buf[pos] = '\0';
    return (int)pos;
}

/* ------------------------------------------------------------------ */
/* Tiny JSON field extractors — no full parser, just enough for the   */
/* four frame shapes the server sends.                                */
/* ------------------------------------------------------------------ */

/*
 * Find the value of a string-typed key in a flat JSON object.
 * Writes into `out` (up to `out_cap - 1` chars + NUL). Returns 0 on
 * success, -1 if the key is absent or the value is not a string.
 *
 * Limitations: does not handle escaped quotes inside string values
 * (the server never sends them in the fields we care about: "type",
 * "data", "message"). Good enough for this protocol.
 */
static int json_get_str(const char *json, const char *key,
                        char *out, size_t out_cap)
{
    /* Build the search needle: "<key>":"  */
    char needle[128];
    int n = snprintf(needle, sizeof(needle), "\"%s\":\"", key);
    if (n < 0 || (size_t)n >= sizeof(needle)) return -1;

    const char *p = strstr(json, needle);
    if (!p) return -1;
    p += (size_t)n;  /* skip past the opening quote of the value */

    size_t i = 0;
    while (*p && *p != '"' && i + 1 < out_cap) {
        if (*p == '\\' && *(p + 1)) {
            p++;  /* skip backslash, take next char literally */
        }
        out[i++] = *p++;
    }
    out[i] = '\0';
    return 0;
}

/*
 * Find the value of an integer-typed key. Returns the value via *val;
 * returns 0 on success, -1 if absent.
 */
static int json_get_int(const char *json, const char *key, int *val)
{
    char needle[128];
    int n = snprintf(needle, sizeof(needle), "\"%s\":", key);
    if (n < 0 || (size_t)n >= sizeof(needle)) return -1;

    const char *p = strstr(json, needle);
    if (!p) return -1;
    p += (size_t)n;

    /* Skip whitespace between colon and value */
    while (*p == ' ' || *p == '\t') p++;

    char *end = NULL;
    long v = strtol(p, &end, 10);
    if (end == p) return -1;
    *val = (int)v;
    return 0;
}

/* ------------------------------------------------------------------ */
/* Frame dispatch                                                      */
/* ------------------------------------------------------------------ */

/*
 * Process a single newline-delimited JSON frame from the server.
 * Returns: >= 0  if the stream should continue (0 = normal frame),
 *          < 0   the negated exit code if "exit" or "error" was seen
 *                (caller should terminate with -retval).
 *
 * Convention: on "exit" frame, returns -(code). On "error", returns
 * -(1). Since exit code 0 maps to -0 == 0, we use a separate flag.
 */
static int dispatch_frame(const char *line, int *got_exit, int *exit_code)
{
    char type[32] = {0};
    if (json_get_str(line, "type", type, sizeof(type)) < 0)
        return 0;  /* no "type" field — ignore */

    if (strcmp(type, "stdout") == 0) {
        /* Stream raw "data" to stdout.  We need to handle JSON string
         * unescaping for common sequences (\n, \t, \\, \"). */
        char needle[] = "\"data\":\"";
        const char *p = strstr(line, needle);
        if (!p) return 0;
        p += sizeof(needle) - 1;
        while (*p && *p != '"') {
            if (*p == '\\') {
                p++;
                switch (*p) {
                case 'n':  fputc('\n', stdout); break;
                case 't':  fputc('\t', stdout); break;
                case 'r':  fputc('\r', stdout); break;
                case '\\': fputc('\\', stdout); break;
                case '"':  fputc('"', stdout);  break;
                case '/':  fputc('/', stdout);  break;
                case 'u':
                    /* \uXXXX — for common control chars only */
                    if (p[1] && p[2] && p[3] && p[4]) {
                        char hex[5] = {p[1], p[2], p[3], p[4], 0};
                        unsigned int cp = (unsigned int)strtoul(hex, NULL, 16);
                        if (cp < 0x80) {
                            fputc((int)cp, stdout);
                        } else {
                            /* UTF-8 encode: 2-byte case (BMP only) */
                            if (cp < 0x800) {
                                fputc((int)(0xC0 | (cp >> 6)), stdout);
                                fputc((int)(0x80 | (cp & 0x3F)), stdout);
                            } else {
                                fputc((int)(0xE0 | (cp >> 12)), stdout);
                                fputc((int)(0x80 | ((cp >> 6) & 0x3F)), stdout);
                                fputc((int)(0x80 | (cp & 0x3F)), stdout);
                            }
                        }
                        p += 4;
                    }
                    break;
                default: fputc(*p, stdout); break;
                }
            } else {
                fputc(*p, stdout);
            }
            if (*p) p++;
        }
        fflush(stdout);

    } else if (strcmp(type, "stderr") == 0) {
        char needle[] = "\"data\":\"";
        const char *p = strstr(line, needle);
        if (!p) return 0;
        p += sizeof(needle) - 1;
        while (*p && *p != '"') {
            if (*p == '\\') {
                p++;
                switch (*p) {
                case 'n':  fputc('\n', stderr); break;
                case 't':  fputc('\t', stderr); break;
                case 'r':  fputc('\r', stderr); break;
                case '\\': fputc('\\', stderr); break;
                case '"':  fputc('"', stderr);  break;
                case '/':  fputc('/', stderr);  break;
                case 'u':
                    if (p[1] && p[2] && p[3] && p[4]) {
                        char hex[5] = {p[1], p[2], p[3], p[4], 0};
                        unsigned int cp = (unsigned int)strtoul(hex, NULL, 16);
                        if (cp < 0x80) {
                            fputc((int)cp, stderr);
                        } else if (cp < 0x800) {
                            fputc((int)(0xC0 | (cp >> 6)), stderr);
                            fputc((int)(0x80 | (cp & 0x3F)), stderr);
                        } else {
                            fputc((int)(0xE0 | (cp >> 12)), stderr);
                            fputc((int)(0x80 | ((cp >> 6) & 0x3F)), stderr);
                            fputc((int)(0x80 | (cp & 0x3F)), stderr);
                        }
                        p += 4;
                    }
                    break;
                default: fputc(*p, stderr); break;
                }
            } else {
                fputc(*p, stderr);
            }
            if (*p) p++;
        }
        fflush(stderr);

    } else if (strcmp(type, "exit") == 0) {
        int code = 1;
        json_get_int(line, "code", &code);
        *got_exit = 1;
        *exit_code = code;
        return -1;

    } else if (strcmp(type, "error") == 0) {
        char msg[1024] = {0};
        if (json_get_str(line, "message", msg, sizeof(msg)) == 0)
            fprintf(stderr, "qsu: %s\n", msg);
        else
            fprintf(stderr, "qsu: server error (no message)\n");
        *got_exit = 1;
        *exit_code = 1;
        return -1;

    } else {
        fprintf(stderr, "qsu: unknown frame type: %s\n", type);
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/* Stream loop                                                         */
/* ------------------------------------------------------------------ */

static int stream_response(int fd)
{
    char recv_buf[RECV_BUF];
    char *line_buf = NULL;
    size_t line_len = 0;
    size_t line_cap = 0;
    int got_exit = 0;
    int exit_code = 1;

    line_cap = LINE_BUF_INIT;
    line_buf = malloc(line_cap);
    if (!line_buf) {
        fprintf(stderr, "qsu: out of memory\n");
        return 1;
    }

    while (!got_exit) {
        ssize_t nr = recv(fd, recv_buf, sizeof(recv_buf), 0);
        if (nr < 0) {
            if (errno == EINTR) continue;
            fprintf(stderr, "qsu: recv: %s\n", strerror(errno));
            break;
        }
        if (nr == 0) break;  /* server closed connection */

        /* Append to line buffer, then process complete lines. */
        size_t needed = line_len + (size_t)nr;
        if (needed > LINE_BUF_MAX) {
            fprintf(stderr, "qsu: server frame too large (>%d bytes)\n",
                    LINE_BUF_MAX);
            free(line_buf);
            return 1;
        }
        if (needed > line_cap) {
            while (line_cap < needed) line_cap *= 2;
            if (line_cap > LINE_BUF_MAX) line_cap = LINE_BUF_MAX;
            char *tmp = realloc(line_buf, line_cap);
            if (!tmp) {
                fprintf(stderr, "qsu: out of memory\n");
                free(line_buf);
                return 1;
            }
            line_buf = tmp;
        }
        memcpy(line_buf + line_len, recv_buf, (size_t)nr);
        line_len += (size_t)nr;

        /* Process complete newline-delimited frames. */
        size_t start = 0;
        for (size_t i = 0; i < line_len; i++) {
            if (line_buf[i] == '\n') {
                line_buf[i] = '\0';
                if (i > start) {
                    dispatch_frame(line_buf + start, &got_exit, &exit_code);
                    if (got_exit) break;
                }
                start = i + 1;
            }
        }
        /* Move unconsumed bytes to the front. */
        if (start > 0) {
            line_len -= start;
            if (line_len > 0)
                memmove(line_buf, line_buf + start, line_len);
        }
    }

    free(line_buf);
    return exit_code;
}

/* ------------------------------------------------------------------ */
/* main                                                                */
/* ------------------------------------------------------------------ */

static void usage(void)
{
    fprintf(stderr,
        "Usage: qsu [-u USER] command [args...]\n"
        "\n"
        "qdistro sudo replacement. Connects to qdistro-root-exec and\n"
        "runs `command` as USER (default: root) after admin approval.\n");
}

int main(int argc, char **argv)
{
    /* Ignore SIGPIPE so that writing to a broken stdout/stderr pipe
     * (e.g. `qsu id | head -1`) returns an error instead of killing
     * the process. Send-side SIGPIPE is already handled via
     * MSG_NOSIGNAL on the socket. */
    signal(SIGPIPE, SIG_IGN);

    /* Set the kernel task name so /proc/<pid>/comm reads "qsu". */
    prctl(PR_SET_NAME, "qsu", 0, 0, 0);

    const char *target_user = "root";
    int opt;

    /* Parse -u/--user. Stop at the first non-option (the command). */
    static struct option long_opts[] = {
        {"user", required_argument, NULL, 'u'},
        {"help", no_argument,       NULL, 'h'},
        {NULL, 0, NULL, 0}
    };

    /* We need POSIX-style option parsing: stop at first non-option. */
    opterr = 0;  /* suppress getopt's own error messages */
    while ((opt = getopt_long(argc, argv, "+u:h", long_opts, NULL)) != -1) {
        switch (opt) {
        case 'u':
            target_user = optarg;
            break;
        case 'h':
            usage();
            return 0;
        default:
            usage();
            return 2;
        }
    }

    /* Everything after the parsed options is the command. If "--" was
     * used, getopt already consumed it. */
    int cmd_argc = argc - optind;
    char **cmd_argv = argv + optind;

    if (cmd_argc < 1) {
        fprintf(stderr, "qsu: command required\n");
        usage();
        return 2;
    }

    /* Build the JSON request. 256 KiB should be more than enough
     * for any reasonable argv. */
    size_t req_cap = 256 * 1024;
    char *req_buf = malloc(req_cap);
    if (!req_buf) {
        fprintf(stderr, "qsu: out of memory\n");
        return 1;
    }

    int req_len = build_request(req_buf, req_cap,
                                target_user, cmd_argc, cmd_argv);
    if (req_len < 0) {
        fprintf(stderr, "qsu: argv too large for request buffer\n");
        free(req_buf);
        return 1;
    }

    /* Connect to qdistro-root-exec. */
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        fprintf(stderr, "qsu: socket: %s\n", strerror(errno));
        free(req_buf);
        return 2;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (strlen(SOCKET_PATH) >= sizeof(addr.sun_path)) {
        fprintf(stderr, "qsu: socket path too long\n");
        close(fd);
        free(req_buf);
        return 2;
    }
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path) - 1);

    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr,
            "qsu: cannot reach qdistro-root-exec (%s). "
            "Is the service active? "
            "`systemctl status qdistro-root-exec.socket`\n",
            strerror(errno));
        close(fd);
        free(req_buf);
        return 2;
    }

    /* Send the request. */
    const char *p = req_buf;
    int remaining = req_len;
    while (remaining > 0) {
        ssize_t nw = send(fd, p, (size_t)remaining, MSG_NOSIGNAL);
        if (nw < 0) {
            if (errno == EINTR) continue;
            fprintf(stderr, "qsu: send: %s\n", strerror(errno));
            close(fd);
            free(req_buf);
            return 2;
        }
        p += nw;
        remaining -= (int)nw;
    }
    free(req_buf);

    /* Stream the response. */
    int rc = stream_response(fd);
    close(fd);
    return rc;
}
