/************************************************************\
 * Copyright 2018 Lawrence Livermore National Security, LLC
 * (c.f. AUTHORS, NOTICE.LLNS, COPYING)
 *
 * This file is part of the Flux resource manager framework.
 * For details, see https://github.com/flux-framework.
 *
 * SPDX-License-Identifier: LGPL-3.0
\************************************************************/

#ifndef _FLUX_JOB_LIST_H
#define _FLUX_JOB_LIST_H

#include <flux/core.h>

#include "src/common/libczmqcontainers/czmq_containers.h"

#include "job_state.h"

struct list_ctx {
    flux_t *h;
    flux_msg_handler_t **handlers;
    struct job_state_ctx *jsctx;
    zlistx_t *idsync_lookups;
    zhashx_t *idsync_waits;
};

const char **job_attrs (void);

#endif /* _FLUX_JOB_LIST_H */

/*
 * vi:tabstop=4 shiftwidth=4 expandtab
 */

