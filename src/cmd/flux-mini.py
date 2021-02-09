##############################################################
# Copyright 2019 Lawrence Livermore National Security, LLC
# (c.f. AUTHORS, NOTICE.LLNS, COPYING)
#
# This file is part of the Flux resource manager framework.
# For details, see https://github.com/flux-framework.
#
# SPDX-License-Identifier: LGPL-3.0
##############################################################

# pylint: disable=duplicate-code
import os
import sys
import logging
import argparse
import json
import fnmatch
import re
import random
import itertools
from itertools import chain
from string import Template
from collections import ChainMap

import flux
from flux import job
from flux.job import JobspecV1, JobID
from flux import util
from flux import debugged
from flux.idset import IDset
from flux.progress import ProgressBar


def filter_dict(env, pattern, reverseMatch=True):
    """
    Filter out all keys that match "pattern" from dict 'env'

    Pattern is assumed to be a shell glob(7) pattern, unless it begins
    with '/', in which case the pattern is a regex.
    """
    if pattern.startswith("/"):
        pattern = pattern[1::].rstrip("/")
    else:
        pattern = fnmatch.translate(pattern)
    regex = re.compile(pattern)
    if reverseMatch:
        return dict(filter(lambda x: not regex.match(x[0]), env.items()))
    return dict(filter(lambda x: regex.match(x[0]), env.items()))


def get_filtered_environment(rules, environ=None):
    """
    Filter environment dictionary 'environ' given a list of rules.
    Each rule can filter, set, or modify the existing environment.
    """
    if environ is None:
        environ = dict(os.environ)
    if rules is None:
        return environ
    for rule in rules:
        #
        #  If rule starts with '-' then the rest of the rule is a pattern
        #   which filters matching environment variables from the
        #   generated environment.
        #
        if rule.startswith("-"):
            environ = filter_dict(environ, rule[1::])
        #
        #  If rule starts with '^', then the result of the rule is a filename
        #   from which to read more rules.
        #
        elif rule.startswith("^"):
            filename = os.path.expanduser(rule[1::])
            with open(filename) as envfile:
                lines = [line.strip() for line in envfile]
                environ = get_filtered_environment(lines, environ=environ)
        #
        #  Otherwise, the rule is an explicit variable assignment
        #   VAR=VAL. If =VAL is not provided then VAL refers to the
        #   value for VAR in the current environment of this process.
        #
        #  Quoted shell variables are expanded using values from the
        #   built environment, not the process environment. So
        #   --env=PATH=/bin --env=PATH='$PATH:/foo' results in
        #   PATH=/bin:/foo.
        #
        else:
            var, *rest = rule.split("=", 1)
            if not rest:
                #
                #  VAR alone with no set value pulls in all matching
                #   variables from current environment that are not already
                #   in the generated environment.
                env = filter_dict(os.environ, var, reverseMatch=False)
                for key, value in env.items():
                    if key not in environ:
                        environ[key] = value
            else:
                #
                #  Template lookup: use jobspec environment first, fallback
                #   to current process environment using ChainMap:
                lookup = ChainMap(environ, os.environ)
                try:
                    environ[var] = Template(rest[0]).substitute(lookup)
                except ValueError as ex:
                    LOGGER.error("--env: Unable to substitute %s", rule)
                    raise
                except KeyError as ex:
                    raise Exception(f"--env: Variable {ex} not found in {rule}")
    return environ


class EnvFileAction(argparse.Action):
    """Convenience class to handle --env-file option

    Append --env-file options to the "env" list in namespace, with "^"
    prepended to the rule to indicate further rules are to be read
    from the indicated file.

    This is required to preserve ordering between the --env and --env-file
    and --env-remove options.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, "env", [])
        if items is None:
            items = []
        items.append("^" + values)
        setattr(namespace, "env", items)


class EnvFilterAction(argparse.Action):
    """Convenience class to handle --env-remove option

    Append --env-remove options to the "env" list in namespace, with "-"
    prepended to the option argument.

    This is required to preserve ordering between the --env and --env-remove
    options.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, "env", [])
        if items is None:
            items = []
        items.append("-" + values)
        setattr(namespace, "env", items)


class Xcmd:
    """Represent a Flux job with mutable command and option args"""

    # dict of mutable argparse args. The values are used in
    #  the string representation of an Xcmd object.
    mutable_args = {
        "ntasks": "-n",
        "nodes": "-N",
        "cores_per_task": "-c",
        "gpus_per_task": "-g",
        "time_limit": "-t",
        "env": "--env=",
        "env_file": "--env-file=",
        "urgency": "--urgency=",
        "setopt": "-o ",
        "setattr": "--setattr=",
        "job_name": "--job-name=",
        "input": "--input=",
        "output": "--output=",
        "error": "--error=",
        "cc": "--cc=",
        "bcc": "--bcc=",
    }

    class Xinput:
        """A string class with convenient attributes for formatting args

        This class represents a string with special attributes specific
        for use on the bulksubmit command line, e.g.::

            {0.%}    : the argument without filename extension
            {0./}    : the argument basename
            {0.//}   : the argument dirname
            {0./%}   : the basename without filename extension
            {0.name} : the result of dynamically assigned method "name"

        """

        def __init__(self, arg, methods):
            self.methods = methods
            self.string = arg

        def __str__(self):
            return self.string

        def __getattr__(self, attr):
            if attr == "%":
                return os.path.splitext(self.string)[0]
            if attr == "/":
                return os.path.basename(self.string)
            if attr == "//":
                return os.path.dirname(self.string)
            if attr == "/%":
                return os.path.basename(os.path.splitext(self.string)[0])
            if attr in self.methods:
                #  Note: combine list return values with the special
                #   sentinel ::list::: so they can be split up again
                #   after .format() converts them to strings. This allows
                #   user-provided methods to return lists as well as
                #   single values, where each list element can become
                #   a new argument in a command
                #
                # pylint: disable=eval-used
                result = eval(self.methods[attr], globals(), dict(x=self.string))
                if isinstance(result, list):
                    return "::list::".join(result)
                return result
            raise ValueError(f"Unknown input string method '.{attr}'")

    @staticmethod
    def preserve_mustache(val):
        """Preserve any mustache template in value 'val'"""

        def subst(val):
            return val.replace("{{", "=stache=").replace("}}", "=/stache=")

        if isinstance(val, str):
            return subst(val)
        if isinstance(val, list):
            return [subst(x) for x in val]
        return val

    @staticmethod
    def restore_mustache(val):
        """Restore any mustache template in value 'val'"""

        def restore(val):
            return val.replace("=stache=", "{{").replace("=/stache=", "}}")

        if isinstance(val, str):
            return restore(val)
        if isinstance(val, list):
            return [restore(x) for x in val]
        return val

    def __init__(self, args, inputs=None, **kwargs):
        """Initialize and Xcmd (eXtensible Command) object

        Given BulkSubmit `args` and `inputs`, substitute all inputs
        in command and applicable options using string.format().

        """
        if inputs is None:
            inputs = []

        #  Save reference to original args:
        self._orig_args = args

        #  Convert all inputs to Xinputs so special attributes are
        #   available during .format() processing:
        #
        inputs = [self.Xinput(x, args.methods) for x in inputs]

        #  Format each argument in args.command, splitting on the
        #   special "::list::" sentinel to handle the case where
        #   custom input methods return a list (See Xinput.__getattr__)
        #
        self.command = []
        for arg in args.command:
            try:
                result = arg.format(*inputs, **kwargs).split("::list::")
            except IndexError:
                LOGGER.error("Invalid replacement string in command: '%s'", arg)
                sys.exit(1)
            if result:
                self.command.extend(result)

        #  Format all supported mutable options defined in `mutable_args`
        #  Note: only list and string options are supported.
        #
        self.modified = {}
        for attr in self.mutable_args:
            val = getattr(args, attr)
            if val is None:
                continue

            val = self.preserve_mustache(val)

            try:
                if isinstance(val, str):
                    newval = val.format(*inputs, **kwargs)
                elif isinstance(val, list):
                    newval = [x.format(*inputs, **kwargs) for x in val]
                else:
                    newval = val
            except IndexError:
                LOGGER.error(
                    "Invalid replacement string in %s: '%s'",
                    self.mutable_args[attr],
                    val,
                )
                sys.exit(1)

            newval = self.restore_mustache(newval)

            setattr(self, attr, newval)

            #  For better verbose and dry-run output, capture mutable
            #   args that were actually changed:
            if val != newval:
                self.modified[attr] = True

    def __getattr__(self, attr):
        """
        Fall back to original args if attribute not found.
        This allows an Xcmd object to used in place of an argparse Namespace.
        """
        return getattr(self._orig_args, attr)

    def __str__(self):
        """String representation of an Xcmd for debugging output"""
        result = []
        for attr in self.mutable_args:
            value = getattr(self, attr)
            if attr in self.modified and value:
                opt = self.mutable_args[attr]
                result.append(f"{opt}{value}")
        result.extend(self.command)
        return " ".join(result)


class MiniCmd:
    """
    MiniCmd is the base class for all flux-mini subcommands
    """

    def __init__(self, **kwargs):
        self.flux_handle = None
        self.exitcode = 0
        self.progress = None
        self.parser = self.create_parser(kwargs)

    @staticmethod
    def create_parser(exclude_io=False):
        """
        Create default parser with args for mini subcommands
        """
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument(
            "-t",
            "--time-limit",
            type=str,
            metavar="FSD",
            help="Time limit in Flux standard duration, e.g. 2d, 1.5h",
        )
        parser.add_argument(
            "--urgency",
            help="Set job urgency (0-31), hold=0, default=16, expedite=31",
            metavar="N",
            default="16",
        )
        parser.add_argument(
            "--job-name",
            type=str,
            help="Set an optional name for job to NAME",
            metavar="NAME",
        )
        parser.add_argument(
            "-o",
            "--setopt",
            action="append",
            help="Set shell option OPT. An optional value is supported with"
            + " OPT=VAL (default VAL=1) (multiple use OK)",
            metavar="OPT",
        )
        parser.add_argument(
            "--setattr",
            action="append",
            help="Set job attribute ATTR to VAL (multiple use OK)",
            metavar="ATTR=VAL",
        )
        parser.add_argument(
            "--env",
            action="append",
            help="Control how environment variables are exported. If RULE "
            + "starts with '-' apply rest of RULE as a remove filter (see "
            + "--env-remove), if '^' then read rules from a file "
            + "(see --env-file). Otherwise, set matching environment variables "
            + "from the current environment (--env=PATTERN) or set a value "
            + "explicitly (--env=VAR=VALUE). Rules are applied in the order "
            + "they are used on the command line. (multiple use OK)",
            metavar="RULE",
        )
        parser.add_argument(
            "--env-remove",
            action=EnvFilterAction,
            help="Remove environment variables matching PATTERN. "
            + "If PATTERN starts with a '/', then it is matched "
            + "as a regular expression, otherwise PATTERN is a shell "
            + "glob expression. (multiple use OK)",
            metavar="PATTERN",
        )
        parser.add_argument(
            "--env-file",
            action=EnvFileAction,
            help="Read a set of environment rules from FILE. (multiple use OK)",
            metavar="FILE",
        )
        parser.add_argument(
            "--input",
            type=str,
            help="Redirect job stdin from FILENAME, bypassing KVS"
            if not exclude_io
            else argparse.SUPPRESS,
            metavar="FILENAME",
        )
        parser.add_argument(
            "--output",
            type=str,
            help="Redirect job stdout to FILENAME, bypassing KVS"
            if not exclude_io
            else argparse.SUPPRESS,
            metavar="FILENAME",
        )
        parser.add_argument(
            "--error",
            type=str,
            help="Redirect job stderr to FILENAME, bypassing KVS"
            if not exclude_io
            else argparse.SUPPRESS,
            metavar="FILENAME",
        )
        parser.add_argument(
            "-l",
            "--label-io",
            action="store_true",
            help="Add rank labels to stdout, stderr lines"
            if not exclude_io
            else argparse.SUPPRESS,
        )
        parser.add_argument(
            "--flags",
            action="append",
            help="Set comma separated list of job submission flags. Possible "
            + "flags:  debug, waitable",
            metavar="FLAGS",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Don't actually submit job, just emit jobspec",
        )
        parser.add_argument(
            "--debug-emulate", action="store_true", help=argparse.SUPPRESS
        )
        return parser

    def init_jobspec(self, args):
        """
        Return initialized jobspec. This is an abstract method which must
        be provided by each base class
        """
        raise NotImplementedError()

    # pylint: disable=too-many-branches,too-many-statements
    def jobspec_create(self, args):
        """
        Create a jobspec from args and return it to caller
        """
        jobspec = self.init_jobspec(args)
        jobspec.cwd = os.getcwd()
        jobspec.environment = get_filtered_environment(args.env)
        if args.time_limit is not None:
            jobspec.duration = args.time_limit

        if args.job_name is not None:
            jobspec.setattr("system.job.name", args.job_name)

        if args.input is not None:
            jobspec.stdin = args.input

        if args.output is not None and args.output not in ["none", "kvs"]:
            jobspec.stdout = args.output
            if args.label_io:
                jobspec.setattr_shell_option("output.stdout.label", True)

        if args.error is not None:
            jobspec.stderr = args.error
            if args.label_io:
                jobspec.setattr_shell_option("output.stderr.label", True)

        if args.setopt is not None:
            for keyval in args.setopt:
                # Split into key, val with a default for 1 if no val given:
                key, val = (keyval.split("=", 1) + [1])[:2]
                try:
                    val = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
                jobspec.setattr_shell_option(key, val)

        if args.debug_emulate:
            debugged.set_mpir_being_debugged(1)

        if debugged.get_mpir_being_debugged() == 1:
            # if stop-tasks-in-exec is present, overwrite
            jobspec.setattr_shell_option("stop-tasks-in-exec", json.loads("1"))

        if args.setattr is not None:
            for keyval in args.setattr:
                tmp = keyval.split("=", 1)
                if len(tmp) != 2:
                    raise ValueError("--setattr: Missing value for attr " + keyval)
                key = tmp[0]
                try:
                    val = json.loads(tmp[1])
                except (json.JSONDecodeError, TypeError):
                    val = tmp[1]
                jobspec.setattr(key, val)

        return jobspec

    def submit_async(self, args, jobspec=None):
        """
        Submit job, constructing jobspec from args unless jobspec is not None.
        Returns a SubmitFuture.
        """
        if jobspec is None:
            jobspec = self.jobspec_create(args)

        if args.dry_run:
            print(jobspec.dumps(), file=sys.stdout)
            sys.exit(0)

        arg_debug = False
        arg_waitable = False
        if args.flags is not None:
            for tmp in args.flags:
                for flag in tmp.split(","):
                    if flag == "debug":
                        arg_debug = True
                    elif flag == "waitable":
                        arg_waitable = True
                    else:
                        raise ValueError("--flags: Unknown flag " + flag)

        if not self.flux_handle:
            self.flux_handle = flux.Flux()

        if args.urgency == "default":
            urgency = flux.constants.FLUX_JOB_URGENCY_DEFAULT
        elif args.urgency == "hold":
            urgency = flux.constants.FLUX_JOB_URGENCY_HOLD
        elif args.urgency == "expedite":
            urgency = flux.constants.FLUX_JOB_URGENCY_EXPEDITE
        else:
            urgency = int(args.urgency)

        return job.submit_async(
            self.flux_handle,
            jobspec.dumps(),
            urgency=urgency,
            waitable=arg_waitable,
            debug=arg_debug,
        )

    def submit(self, args, jobspec=None):
        return JobID(self.submit_async(args, jobspec).get_id())

    def get_parser(self):
        return self.parser


class SubmitBaseCmd(MiniCmd):
    """
    SubmitBaseCmd is an abstract class with shared code for job submission
    """

    def __init__(self):
        super().__init__()
        self.parser.add_argument(
            "-N", "--nodes", metavar="N", help="Number of nodes to allocate"
        )
        self.parser.add_argument(
            "-n",
            "--ntasks",
            metavar="N",
            default="1",
            help="Number of tasks to start",
        )
        self.parser.add_argument(
            "-c",
            "--cores-per-task",
            metavar="N",
            default=1,
            help="Number of cores to allocate per task",
        )
        self.parser.add_argument(
            "-g",
            "--gpus-per-task",
            metavar="N",
            help="Number of GPUs to allocate per task",
        )
        self.parser.add_argument(
            "-v",
            "--verbose",
            action="count",
            default=0,
            help="Increase verbosity on stderr (multiple use OK)",
        )

    def init_jobspec(self, args):
        if not args.command:
            raise ValueError("job command and arguments are missing")

        #  Ensure integer args are converted to int() here.
        #  This is done because we do not use type=int in argparse in order
        #   to allow these options to be mutable for bulksubmit:
        #
        for arg in ["ntasks", "nodes", "cores_per_task", "gpus_per_task"]:
            value = getattr(args, arg)
            if value:
                try:
                    setattr(args, arg, int(value))
                except ValueError:
                    opt = arg.replace("_", "-")
                    raise ValueError(f"--{opt}: invalid int value '{value}'")

        return JobspecV1.from_command(
            args.command,
            num_tasks=args.ntasks,
            cores_per_task=args.cores_per_task,
            gpus_per_task=args.gpus_per_task,
            num_nodes=args.nodes,
        )

    def run_and_exit(self):
        self.flux_handle.reactor_run()
        sys.exit(self.exitcode)


class SubmitBulkCmd(SubmitBaseCmd):
    """
    SubmitBulkCmd adds options for submitting copies of jobs,
    watching progress of submission, and waiting for job completion
    to the SubmitBaseCmd class
    """

    def __init__(self):
        super().__init__()
        self.parser.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            help="Do not print jobid to stdout on submission",
        )
        self.parser.add_argument(
            "--cc",
            metavar="IDSET",
            default=None,
            help="Replicate job for each ID in IDSET. "
            "(FLUX_JOB_CC=ID will be set for each job submitted)",
        )
        self.parser.add_argument(
            "--bcc",
            metavar="IDSET",
            default=None,
            help="Like --cc, but FLUX_JOB_CC is not set",
        )
        self.parser.add_argument(
            "--wait",
            action="store_true",
            help="Wait for all jobs to complete after submission",
        )
        self.parser.add_argument(
            "--watch",
            action="store_true",
            help="Watch all job output (implies --wait)",
        )
        self.parser.add_argument(
            "--progress",
            action="store_true",
            help="Show progress of job submission or completion (with --wait)",
        )
        self.parser.add_argument(
            "--jps",
            action="store_true",
            help="With --progress, show job throughput",
        )

    @staticmethod
    def output_watch_cb(future, args, jobid, label):
        """Handle events in the guest.output eventlog"""
        event = future.get_event()
        if event and event.name == "data":
            if "stream" in event.context and "data" in event.context:
                stream = event.context["stream"]
                data = event.context["data"]
                rank = event.context["rank"]
                if args.label_io:
                    getattr(sys, stream).write(f"{jobid}: {rank}: {data}")
                else:
                    getattr(sys, stream).write(data)

    def exec_watch_cb(self, future, args, jobid, label=""):
        """Handle events in the guest.exec.eventlog"""
        event = future.get_event()
        if event and event.name == "shell.init":
            #  Once the shell.init event is posted, then it is safe to
            #   begin watching the output eventlog:
            #
            job.event_watch_async(
                self.flux_handle, jobid, eventlog="guest.output"
            ).then(self.output_watch_cb, args, jobid, label)

            #  Events from this eventlog are no longer needed
            future.cancel()

    @staticmethod
    def status_to_exitcode(status):
        """Calculate exitcode from job status"""
        if os.WIFEXITED(status):
            status = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            status = 127 + os.WTERMSIG(status)
        return status

    def event_watch_cb(self, future, args, jobinfo, label=""):
        """Handle events in the main job eventlog"""
        jobid = jobinfo["id"]
        event = future.get_event()
        self.progress_update(jobinfo, event=event)
        if event is None:
            return
        if event.name == "exception":
            #
            #  Handle an exception: update global exitcode and print
            #   an error:
            if jobinfo["state"] == "submit":
                #
                #  If job was still pending then this job failed
                #   to execute. Treat it as failure with exitcode = 1
                #
                jobinfo["state"] = "failed"
                if self.exitcode == 0:
                    self.exitcode = 1

            #  Print a human readable error:
            exception_type = event.context["type"]
            note = event.context["note"]
            print(
                f"{jobid}: exception: type={exception_type} note={note}",
                file=sys.stderr,
            )
        elif event.name == "start" and args.watch:
            #
            #  Watch the exec eventlog if the --watch option was provided:
            #
            jobinfo["state"] = "running"
            job.event_watch_async(
                self.flux_handle, jobid, eventlog="guest.exec.eventlog"
            ).then(self.exec_watch_cb, args, jobid, label)
        elif event.name == "finish":
            #
            #  Collect exit status and adust self.exitcode if necesary:
            #
            jobinfo["state"] = "done"
            status = self.status_to_exitcode(event.context["status"])
            if args.verbose:
                print(f"{jobid}: complete: status={status}", file=sys.stderr)
            if status > self.exitcode:
                self.exitcode = status

    def submit_cb(self, future, args, label=""):
        try:
            jobid = JobID(future.get_id())
            if not args.quiet:
                print(jobid)
        except OSError as exc:
            print(f"{label}{exc}", file=sys.stderr)
            self.exitcode = 1
            self.progress_update(submit_failed=True)
            return

        if args.wait or args.watch:
            #
            #  If the user requested to wait for or watch all jobs
            #   then start watching the main eventlog.
            #
            #  Carry along a bit of state for each job so that exceptions
            #   before the job is running can be handled properly
            #
            jobinfo = {"id": jobid, "state": "submit"}
            fut = job.event_watch_async(self.flux_handle, jobid)
            fut.then(self.event_watch_cb, args, jobinfo, label)
            self.progress_update(jobinfo, submit=True)
        elif self.progress:
            #  Update progress of submission only
            self.progress.update(jps=self.jobs_per_sec())

    def jobs_per_sec(self):
        return (self.progress.count + 1) / self.progress.elapsed

    def progress_start(self, args, total):
        """
        Initialize progress bar if one was requested
        """
        if not args.progress or self.progress:
            # progress bar not requested or already started
            return
        if not sys.stdout.isatty():
            LOGGER.warning("stdout is not a tty. Ignoring --progress option")

        before = (
            "PD:{pending:<{width}} R:{running:<{width}} "
            "CD:{complete:<{width}} F:{fail:<{width}} "
        )
        if not (args.wait or args.watch):
            before = "Submitting {total} jobs: "
        after = "{percent:5.1f}% {elapsed.dt}"
        if args.jps:
            after = "{percent:5.1f}% {jps:4.1f} job/s"
        self.progress = ProgressBar(
            timer=False,
            total=total,
            width=len(str(total)),
            before=before,
            after=after,
            pending=0,
            running=0,
            complete=0,
            fail=0,
            jps=0,
        ).start()

    def progress_update(
        self, jobinfo=None, submit=False, submit_failed=False, event=None
    ):
        """
        Update progress bar if one was requested
        """
        if not self.progress:
            return

        if not self.progress.timer:
            #  Start a timer to update progress bar without other events
            #  (we have to do it here since a flux handle does not exist
            #   in progress_start). We use 250ms to make the progress bar
            #   more fluid.
            timer = self.flux_handle.timer_watcher_create(
                0, lambda *x: self.progress.redraw(), repeat=0.25
            ).start()
            self.progress.update(advance=0, timer=timer)

            #  Don't let this timer watcher contribute to the reactor's
            #   "active" reference count:
            self.flux_handle.reactor_decref()

        if submit:
            self.progress.update(
                advance=0,
                pending=self.progress.pending + 1,
            )
        elif submit_failed:
            self.progress.update(
                advance=1,
                pending=self.progress.pending - 1,
                fail=self.progress.fail + 1,
                jps=self.jobs_per_sec(),
            )
        elif event is None:
            self.progress.update(jps=self.jobs_per_sec())
        elif event.name == "start":
            self.progress.update(
                advance=0,
                pending=self.progress.pending - 1,
                running=self.progress.running + 1,
            )
        elif event.name == "exception" and event.context["severity"] == 0:
            #
            #  Exceptions only need to be specially handled in the
            #   pending state. If the job is running and gets an exception
            #   then a finish event will be posted and handled below:
            #
            if jobinfo["state"] == "submit":
                self.progress.update(
                    advance=0,
                    pending=self.progress.pending - 1,
                    fail=self.progress.fail + 1,
                )
        elif event.name == "finish":
            if event.context["status"] == 0:
                self.progress.update(
                    advance=0,
                    running=self.progress.running - 1,
                    complete=self.progress.complete + 1,
                )
            else:
                self.progress.update(
                    advance=0,
                    running=self.progress.running - 1,
                    fail=self.progress.fail + 1,
                )

    @staticmethod
    def cc_list(args):
        """
        Return a list of values representing job copies given by --cc/--bcc
        """
        cclist = [""]
        if args.cc and args.bcc:
            raise ValueError("specify only one of --cc or --bcc")
        if args.cc:
            cclist = IDset(args.cc)
        elif args.bcc:
            cclist = IDset(args.bcc)
        return cclist

    def submit_async_with_cc(self, args, cclist=None):
        """
        Asynchronously submit jobs, optionally submitting a copy of
        each job for each member of a cc-list. If the cclist is not
        passed in to the method, then one is created from either
        --cc or --bcc options.
        """
        if not cclist:
            cclist = self.cc_list(args)
        label = ""
        jobspec = self.jobspec_create(args)

        if args.progress:
            self.progress_start(args, len(cclist))

        for i in cclist:
            if args.cc or args.bcc:
                label = f"cc={i}: "
                if not args.bcc:
                    jobspec.environment["FLUX_JOB_CC"] = str(i)
            self.submit_async(args, jobspec).then(self.submit_cb, args, label)

    def main(self, args):
        self.submit_async_with_cc(args)
        self.run_and_exit()


class SubmitCmd(SubmitBulkCmd):
    """
    SubmitCmd submits a job, displays the jobid on stdout, and returns.

    Usage: flux mini submit [OPTIONS] cmd ...
    """

    def __init__(self):
        super().__init__()
        self.parser.add_argument(
            "command", nargs=argparse.REMAINDER, help="Job command and arguments"
        )


class BulkSubmitCmd(SubmitBulkCmd):
    """
    BulkSubmitCmd is like xargs for job submission. It takes a series of
    inputs on stdin (or the cmdline separated by :::), and substitutes them
    into the initial arguments, e.g::

       $ echo 1 2 3 | flux mini bulksubmit echo {}

    """

    def __init__(self):
        super().__init__()
        self.parser.add_argument(
            "--shuffle",
            action="store_true",
            help="Shuffle list of commands before submission",
        )
        self.parser.add_argument(
            "--sep",
            type=str,
            metavar="STRING",
            default="\n",
            help="Set the input argument separator. To split on whitespace, "
            "use --sep=none. The default is newline.",
        )
        self.parser.add_argument(
            "--define",
            action="append",
            type=lambda kv: kv.split("="),
            dest="methods",
            default=[],
            help="Define a named method for transforming any input, "
            "accessible via e.g. '{0.NAME}'. (local variable 'x' "
            "will contain the input string to be transformed)",
            metavar="NAME=CODE",
        )
        self.parser.add_argument(
            "command",
            nargs=argparse.REMAINDER,
            help="Job command and iniital arguments",
        )

    @staticmethod
    def input_file(filep, sep):
        """Read set of inputs from file object filep, using separator sep"""
        return list(filter(None, filep.read().split(sep)))

    @staticmethod
    def split_before(iterable, pred):
        """
        Like more_itertools.split_before, but if predicate returns
        True on first element, then return an empty list
        """
        buf = []
        for item in iter(iterable):
            if pred(item):
                yield buf
                buf = []
            buf.append(item)
        yield buf

    def split_command_inputs(self, command, sep="\n", delim=":::"):
        """Generate a list of inputs from command list

        Splits the command list on the input delimiter ``delim``,
        and returns 3 lists:

            - the initial command list (everything before the first delim)
            - a list of normal "input lists"
            - a list of "linked" input lists, (delim + "+")

        Special case delimiter values are handled here,
        e.g. ":::+" and ::::".

        """
        links = []

        #  Split command array into commands and inputs on ":::":
        command, *input_lists = self.split_before(
            command, lambda x: x.startswith(delim)
        )

        #  Remove ':::' separators from each input, allowing GNU parallel
        #   like alternate separators e.g. '::::' and ':::+':
        #
        for i, lst in enumerate(input_lists):
            first = lst.pop(0)
            if first == delim:
                #  Normal input
                continue
            if first == delim + ":":
                #
                #  Read input from file
                if len(lst) > 1:
                    raise ValueError("Multiple args not allowed after ::::")
                if lst[0] == "-":
                    input_lists[i] = self.input_file(sys.stdin, sep)
                else:
                    with open(lst[0]) as filep:
                        input_lists[i] = self.input_file(filep, sep)
            if first in (delim + "+", delim + ":+"):
                #
                #  "Link" input to previous, similar to GNU parallel:
                #  Clear list so this entry can be removed below after
                #   iteration is complete:
                links.append({"index": i, "list": lst.copy()})
                lst.clear()

        #  Remove empty lists (which are now links)
        input_lists = [lst for lst in input_lists if lst]

        return command, input_lists, links

    def create_commands(self, args):
        """Create bulksubmit commands list"""

        #  Expand any escape sequences in args.sep, and replace "none"
        #   with literal None:
        sep = bytes(args.sep, "utf-8").decode("unicode_escape")
        if sep.lower() == "none":
            sep = None

        #  Ensure any provided methods can compile
        args.methods = {
            name: compile(code, name, "eval")
            for name, code in dict(args.methods).items()
        }

        #  Split command into command template and input lists + links:
        args.command, input_list, links = self.split_command_inputs(
            args.command, sep, delim=":::"
        )

        #  If no command provided then the default is "{}"
        if not args.command:
            args.command = ["{}"]

        #  If no inputs on commandline, read from stdin:
        if not input_list:
            input_list = [self.input_file(sys.stdin, sep)]

        #  Take the product of all inputs in input_list
        inputs = [list(x) for x in list(itertools.product(*input_list))]

        #  Now cycle over linked inputs and insert them in result:
        for link in links:
            cycle = itertools.cycle(link["list"])
            for lst in inputs:
                lst.insert(link["index"], next(cycle))

        #  For each set of generated input lists, append a command
        #   to run. Keep a sequence counter so that {seq} can be used
        #   in the format expansion.
        return [Xcmd(args, inp, seq=i) for i, inp in enumerate(inputs)]

    def main(self, args):
        if not args.command:
            args.command = ["{}"]

        #  Create one "command" to be run for each combination of
        #   "inputs" on stdin or the command line:
        #
        commands = self.create_commands(args)

        if args.shuffle:
            random.shuffle(commands)

        #  Calculate total number of commands for use with progress:
        total = 0
        for xargs in commands:
            total += len(self.cc_list(xargs))

        #  Initialize progress bar if requested:
        if args.progress:
            if not args.dry_run:
                self.progress_start(args, total)
            else:
                print(f"flux-mini: submitting a total of {total} jobs")

        #  Loop through commands and asynchronously submit them:
        for xargs in commands:
            if args.verbose or args.dry_run:
                print(f"flux-mini: submit {xargs}")
            if not args.dry_run:
                self.submit_async_with_cc(xargs)

        if not args.dry_run:
            self.run_and_exit()


class RunCmd(SubmitBaseCmd):
    """
    RunCmd is identical to SubmitCmd, except it attaches the the job
    after submission.  Some additional options are added to modify the
    attach behavior.

    Usage: flux mini run [OPTIONS] cmd ...
    """

    def __init__(self):
        super().__init__()
        self.parser.add_argument(
            "command", nargs=argparse.REMAINDER, help="Job command and arguments"
        )

    def main(self, args):
        jobid = self.submit(args)

        # Display job id on stderr if -v
        # N.B. we must flush sys.stderr due to the fact that it is buffered
        # when it points to a file, and os.execvp leaves it unflushed
        if args.verbose > 0:
            print("jobid:", jobid, file=sys.stderr)
            sys.stderr.flush()

        # Build args for flux job attach
        attach_args = ["flux-job", "attach"]
        if args.label_io:
            attach_args.append("--label-io")
        if args.verbose > 1:
            attach_args.append("--show-events")
        if args.verbose > 2:
            attach_args.append("--show-exec")
        if args.debug_emulate:
            attach_args.append("--debug-emulate")
        attach_args.append(jobid.f58.encode("utf-8", errors="surrogateescape"))

        # Exec flux-job attach, searching for it in FLUX_EXEC_PATH.
        os.environ["PATH"] = os.environ["FLUX_EXEC_PATH"] + ":" + os.environ["PATH"]
        os.execvp("flux-job", attach_args)


def add_batch_alloc_args(parser):
    """
    Add "batch"-specific resource allocation arguments to parser object
    which deal in slots instead of tasks.
    """
    parser.add_argument(
        "--broker-opts",
        metavar="OPTS",
        default=None,
        action="append",
        help="Pass options to flux brokers",
    )
    parser.add_argument(
        "-n",
        "--nslots",
        type=int,
        metavar="N",
        help="Number of total resource slots requested."
        + " The size of a resource slot may be specified via the"
        + " -c, --cores-per-slot and -g, --gpus-per-slot options."
        + " The default slot size is 1 core.",
    )
    parser.add_argument(
        "-c",
        "--cores-per-slot",
        type=int,
        metavar="N",
        default=1,
        help="Number of cores to allocate per slot",
    )
    parser.add_argument(
        "-g",
        "--gpus-per-slot",
        type=int,
        metavar="N",
        help="Number of GPUs to allocate per slot",
    )
    parser.add_argument(
        "-N",
        "--nodes",
        type=int,
        metavar="N",
        help="Distribute allocated resource slots across N individual nodes",
    )


def list_split(opts):
    """
    Return a list by splitting each member of opts on ','
    """
    if opts:
        x = chain.from_iterable([x.split(",") for x in opts])
        return list(x)
    return []


class BatchCmd(MiniCmd):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser.add_argument(
            "--wrap",
            action="store_true",
            help="Wrap arguments or stdin in a /bin/sh script",
        )
        add_batch_alloc_args(self.parser)
        self.parser.add_argument(
            "SCRIPT",
            nargs=argparse.REMAINDER,
            help="Batch script and arguments to submit",
        )

    @staticmethod
    def read_script(args):
        if args.SCRIPT:
            if args.wrap:
                #  Wrap args in /bin/sh script
                return "#!/bin/sh\n" + " ".join(args.SCRIPT) + "\n"

            # O/w, open script for reading
            name = open_arg = args.SCRIPT[0]
        else:
            name = "stdin"
            open_arg = 0  # when passed to `open`, 0 gives the `stdin` stream
        with open(open_arg, "r", encoding="utf-8") as filep:
            try:
                #  Read script
                script = filep.read()
                if args.wrap:
                    script = "#!/bin/sh\n" + script
            except UnicodeError:
                raise ValueError(
                    f"{name} does not appear to be a script, "
                    "or failed to encode as utf-8"
                )
            return script

    def init_jobspec(self, args):
        # If no script (reading from stdin), then use "flux" as arg[0]
        if not args.nslots:
            raise ValueError("Number of slots to allocate must be specified")

        jobspec = JobspecV1.from_batch_command(
            script=self.read_script(args),
            jobname=args.SCRIPT[0] if args.SCRIPT else "batchscript",
            args=args.SCRIPT[1:],
            num_slots=args.nslots,
            cores_per_slot=args.cores_per_slot,
            gpus_per_slot=args.gpus_per_slot,
            num_nodes=args.nodes,
            broker_opts=list_split(args.broker_opts),
        )

        # Default output is flux-{{jobid}}.out
        # overridden by either --output=none or --output=kvs
        if not args.output:
            jobspec.stdout = "flux-{{id}}.out"
        return jobspec

    def main(self, args):
        jobid = self.submit(args)
        print(jobid, file=sys.stdout)


class AllocCmd(MiniCmd):
    def __init__(self):
        super().__init__(exclude_io=True)
        add_batch_alloc_args(self.parser)
        self.parser.add_argument(
            "-v",
            "--verbose",
            action="count",
            default=0,
            help="Increase verbosity on stderr (multiple use OK)",
        )
        self.parser.add_argument(
            "COMMAND",
            nargs=argparse.REMAINDER,
            help="Set the initial COMMAND of new Flux instance."
            + "(default is an interactive shell)",
        )

    def init_jobspec(self, args):

        if not args.nslots:
            raise ValueError("Number of slots to allocate must be specified")

        jobspec = JobspecV1.from_nest_command(
            command=args.COMMAND,
            num_slots=args.nslots,
            cores_per_slot=args.cores_per_slot,
            gpus_per_slot=args.gpus_per_slot,
            num_nodes=args.nodes,
            broker_opts=list_split(args.broker_opts),
        )
        if sys.stdin.isatty():
            jobspec.setattr_shell_option("pty", True)
        return jobspec

    def main(self, args):
        jobid = self.submit(args)

        # Display job id on stderr if -v
        # N.B. we must flush sys.stderr due to the fact that it is buffered
        # when it points to a file, and os.execvp leaves it unflushed
        if args.verbose > 0:
            print("jobid:", jobid, file=sys.stderr)
            sys.stderr.flush()

        # Build args for flux job attach
        attach_args = ["flux-job", "attach"]
        attach_args.append(jobid.f58.encode("utf-8", errors="surrogateescape"))

        # Exec flux-job attach, searching for it in FLUX_EXEC_PATH.
        os.environ["PATH"] = os.environ["FLUX_EXEC_PATH"] + ":" + os.environ["PATH"]
        os.execvp("flux-job", attach_args)


LOGGER = logging.getLogger("flux-mini")


@util.CLIMain(LOGGER)
def main():

    sys.stdout = open(
        sys.stdout.fileno(), "w", encoding="utf8", errors="surrogateescape"
    )
    sys.stderr = open(
        sys.stderr.fileno(), "w", encoding="utf8", errors="surrogateescape"
    )

    parser = argparse.ArgumentParser(prog="flux-mini")
    subparsers = parser.add_subparsers(
        title="supported subcommands", description="", dest="subcommand"
    )
    subparsers.required = True

    # run
    run = RunCmd()
    mini_run_parser_sub = subparsers.add_parser(
        "run",
        parents=[run.get_parser()],
        help="run a job interactively",
        formatter_class=flux.util.help_formatter(),
    )
    mini_run_parser_sub.set_defaults(func=run.main)

    # submit
    submit = SubmitCmd()
    mini_submit_parser_sub = subparsers.add_parser(
        "submit",
        parents=[submit.get_parser()],
        help="enqueue a job",
        formatter_class=flux.util.help_formatter(),
    )
    mini_submit_parser_sub.set_defaults(func=submit.main)

    # bulksubmit
    bulksubmit = BulkSubmitCmd()
    description = """
    Submit a series of commands given on the commandline. This is useful
    with shell globs, e.g. `flux mini bulksubmit *.sh`, and will allow
    jobs to be submitted much faster than calling flux-mini submit in a
    loop.
    """
    bulksubmit_parser_sub = subparsers.add_parser(
        "bulksubmit",
        parents=[bulksubmit.get_parser()],
        help="enqueue jobs in bulk",
        usage="flux mini bulksubmit [OPTIONS...] COMMAND [COMMANDS...]",
        description=description,
        formatter_class=flux.util.help_formatter(),
    )
    bulksubmit_parser_sub.set_defaults(func=bulksubmit.main)

    # batch
    batch = BatchCmd()
    description = """
    Submit a batch SCRIPT and ARGS to be run as the initial program of
    a Flux instance.  If no batch script is provided, one will be read
    from stdin.
    """
    mini_batch_parser_sub = subparsers.add_parser(
        "batch",
        parents=[batch.get_parser()],
        help="enqueue a batch script",
        usage="flux mini batch [OPTIONS...] [SCRIPT] [ARGS...]",
        description=description,
        formatter_class=flux.util.help_formatter(),
    )
    mini_batch_parser_sub.set_defaults(func=batch.main)

    # alloc
    alloc = AllocCmd()
    description = """
    Allocate resources and start a new Flux instance. Once the instance
    has started, attach to it interactively.
    """
    mini_alloc_parser_sub = subparsers.add_parser(
        "alloc",
        parents=[alloc.get_parser()],
        help="allocate a new instance for interactive use",
        usage="flux mini alloc [COMMAND] [ARGS...]",
        description=description,
        formatter_class=flux.util.help_formatter(),
    )
    mini_alloc_parser_sub.set_defaults(func=alloc.main)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

# vi: ts=4 sw=4 expandtab
