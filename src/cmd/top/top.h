/************************************************************\
 * Copyright 2021 Lawrence Livermore National Security, LLC
 * (c.f. AUTHORS, NOTICE.LLNS, COPYING)
 *
 * This file is part of the Flux resource manager framework.
 * For details, see https://github.com/flux-framework.
 *
 * SPDX-License-Identifier: LGPL-3.0
\************************************************************/

#include <sys/types.h>
#include <curses.h>
#include <stdarg.h>
#include <flux/core.h>
#include <flux/optparse.h>

// set printf format attribute on this curses function for our convenience
int mvwprintw(WINDOW *win, int y, int x, const char *fmt, ...)
    __attribute__ ((format (printf, 4, 5)));

enum {
    TOP_COLOR_YELLOW = 1, // origin of 1 since 0 is an invalid color pair index
    TOP_COLOR_RED,
    TOP_COLOR_GREEN,
    TOP_COLOR_BLUE,
    TOP_COLOR_BLUE_HIGHLIGHT,
};

struct top {
    flux_t *h;
    char *title;
    flux_jobid_t id;
    uint32_t size;
    struct summary_pane *summary_pane;
    struct joblist_pane *joblist_pane;
    struct keys *keys;
    flux_watcher_t *refresh;
    flux_watcher_t *jobtimer;
    bool jobtimer_running;
    flux_msg_handler_t **handlers;
};

struct dimension {
    int x_begin;
    int y_begin;
    int x_length;
    int y_length;
};

struct top *top_create (const char *uri,
                        const char *prefix,
                        flux_error_t *errp);
void top_destroy (struct top *top);
int top_run (struct top *top, int reactor_flags);

struct summary_pane *summary_pane_create (struct top *top);
void summary_pane_destroy (struct summary_pane *sum);
void summary_pane_draw (struct summary_pane *sum);
void summary_pane_refresh (struct summary_pane *sum);
void summary_pane_query (struct summary_pane *sum);
void summary_pane_heartbeat (struct summary_pane *sum);
void summary_pane_toggle_details (struct summary_pane *sum);

struct joblist_pane *joblist_pane_create (struct top *top);
void joblist_pane_destroy (struct joblist_pane *sum);
void joblist_pane_draw (struct joblist_pane *sum);
void joblist_pane_refresh (struct joblist_pane *sum);
void joblist_pane_query (struct joblist_pane *sum);
void joblist_pane_set_current (struct joblist_pane *joblist, bool next);
void joblist_pane_enter (struct joblist_pane *joblist);

struct keys *keys_create (struct top *top);
void keys_destroy (struct keys *keys);

struct ucache *ucache_create (void);
void ucache_destroy (struct ucache *ucache);
const char *ucache_lookup (struct ucache *ucache, uid_t userid);

void fatal (int errnum, const char *fmt, ...)
    __attribute__ ((format (printf, 2, 3)));

// vi:ts=4 sw=4 expandtab
