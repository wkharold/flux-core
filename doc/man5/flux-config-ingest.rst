=====================
flux-config-ingest(5)
=====================


DESCRIPTION
===========

The Flux **job-ingest** service verifies and validates job requests
before announcing new jobs to the **job-manager**. Configuration of the
**job-ingest** module can be accomplished either via the module command
line or an ``ingest`` TOML table. See the KEYS section below for supported
``ingest`` table keys.

The **job-ingest** module validates jobspec using a work crew of
``flux job-validator`` processes. The validator supports a set of plugins,
and each plugin may consume additional arguments from the command line
for specific configuration. The validator plugins and any arguments are
configured in the ``ingest.validator`` TOML table. See the VALIDATOR KEYS
section below for supported ``ingest.validator`` keys.

KEYS
====

batch-count
   (optional) The job-ingest module batches sets of jobs together
   for efficiency. Normally this is done using a timer, but if the
   ``batch-count`` key is nonzero then jobs are batched based on a counter
   instead. This is mostly useful for testing.

VALIDATOR KEYS
==============

disable
   (optional) A boolean indicating whether to disable job validation.
   Disabling the job validator is not recommended, but may be useful
   for testing or high job throughput scenarios.

plugins
   (optional) An array of validator plugins to use. The default
   value is ``[ "jobspec" ]``, which uses the Python Jobspec class as
   a validator.  For a list of supported plugins on your system run
   ``flux job-validator --list-plugins``

args
   (optional) An array of extra arguments to pass on the validator
   command line. Valid arguments can be found by running
   ``flux job-validator --plugins=LIST --help``

EXAMPLE
=======

::

   [ingest.validator]
   plugins = [ "jobspec", "feasibility" ]
   args =  [ "--require-version=1" ]


RESOURCES
=========

Flux: http://flux-framework.org


SEE ALSO
========

:man5:`flux-config`
