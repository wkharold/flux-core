#!/bin/sh
#

test_description='Test KVS snapshot/restore'

# Append --logfile option if FLUX_TESTS_LOGFILE is set in environment:
test -n "$FLUX_TESTS_LOGFILE" && set -- "$@" --logfile
. `dirname $0`/sharness.sh

test_under_flux 1

CHANGECHECKPOINT=${FLUX_SOURCE_DIR}/t/kvs/change-checkpoint.py

#
# test content-sqlite backing
#

test_expect_success 'run instance with statedir set (sqlite)' '
	flux start -o,--setattr=statedir=$(pwd) \
		   flux kvs put --sequence testkey=42 > start_sequence.out
'

test_expect_success 'content.sqlite file exists after instance exited' '
	test -f content.sqlite &&
	echo Size in bytes: $(stat --format "%s" content.sqlite)
'

test_expect_success 're-run instance with statedir set (sqlite)' '
	flux start -o,--setattr=statedir=$(pwd) \
	           flux kvs get testkey >getsqlite.out
'

test_expect_success 'content from previous instance survived (sqlite)' '
	echo 42 >getsqlite.exp &&
	test_cmp getsqlite.exp getsqlite.out
'

# due to other KVS activity that testing can't control, we simply want
# to ensure the sequence number does not restart at 0, it must
# increase over several restarts

test_expect_success 're-run instance, get sequence number 1 (sqlite)' '
	flux start -o,--setattr=statedir=$(pwd) \
	           flux kvs version > restart_version1.out
'

test_expect_success 'restart sequence number increasing 1 (sqlite)' '
	seq1=$(cat start_sequence.out) &&
	seq2=$(cat restart_version1.out) &&
	test $seq1 -lt $seq2
'

test_expect_success 're-run instance, get sequence number 2 (sqlite)' '
	flux start -o,--setattr=statedir=$(pwd) \
	           flux kvs version > restart_version2.out
'

test_expect_success 'restart sequence number increasing 2 (sqlite)' '
	seq1=$(cat restart_version1.out) &&
	seq2=$(cat restart_version2.out) &&
	test $seq1 -lt $seq2
'

test_expect_success 're-run instance, verify checkpoint date saved (sqlite)' '
	flux start -o,--setattr=statedir=$(pwd) \
	           flux dmesg >dmesgsqlite1.out
'

# just check for todays date, not time for obvious reasons
test_expect_success 'verify date in flux logs (sqlite)' '
	today=`date --iso-8601` &&
	grep checkpoint dmesgsqlite1.out | grep ${today}
'

test_expect_success 're-run instance, get rootref (sqlite)' '
	flux start -o,--setattr=statedir=$(pwd) \
	           flux kvs getroot -b > getrootsqlite.out
'

test_expect_success 'write rootref to checkpoint path, emulating checkpoint version=0 (sqlite)' '
        rootref=$(cat getrootsqlite.out) &&
        ${CHANGECHECKPOINT} $(pwd)/content.sqlite "kvs-primary" ${rootref}
'

test_expect_success 're-run instance, verify checkpoint correctly loaded (sqlite)' '
	flux start -o,--setattr=statedir=$(pwd) \
	           flux dmesg >dmesgsqlite2.out
'

test_expect_success 'verify checkpoint loaded with no date (sqlite)' '
	grep checkpoint dmesgsqlite2.out | grep "N\/A"
'

#
# test content-files backing
#

test_expect_success 'generate rc1/rc3 for content-files backing' '
	cat >rc1-content-files <<EOF &&
#!/bin/bash -e
flux module load content-files
flux module load kvs
EOF
	cat >rc3-content-files <<EOF &&
#!/bin/bash -e
flux module remove kvs
flux module remove content-files
EOF
        chmod +x rc1-content-files &&
        chmod +x rc3-content-files
'

test_expect_success 'run instance with statedir set (files)' '
	flux start -o,--setattr=statedir=$(pwd) \
                   -o,--setattr=broker.rc1_path=$(pwd)/rc1-content-files \
                   -o,--setattr=broker.rc3_path=$(pwd)/rc3-content-files \
	           flux kvs put testkey=43
'

test_expect_success 'content.files dir and kvs-primary exist after instance exit' '
	test -d content.files &&
	test -e content.files/kvs-primary
'

test_expect_success 're-run instance with statedir set (files)' '
	flux start -o,--setattr=statedir=$(pwd) \
                   -o,--setattr=broker.rc1_path=$(pwd)/rc1-content-files \
                   -o,--setattr=broker.rc3_path=$(pwd)/rc3-content-files \
	           flux kvs get testkey >getfiles.out
'

test_expect_success 'content from previous instance survived (files)' '
	echo 43 >getfiles.exp &&
	test_cmp getfiles.exp getfiles.out
'

test_expect_success 're-run instance, verify checkpoint date saved (files)' '
	flux start -o,--setattr=statedir=$(pwd) \
                   -o,--setattr=broker.rc1_path=$(pwd)/rc1-content-files \
                   -o,--setattr=broker.rc3_path=$(pwd)/rc3-content-files \
	           flux dmesg >dmesgfiles.out
'

# just check for todays date, not time for obvious reasons
test_expect_success 'verify date in flux logs (files)' '
	today=`date --iso-8601` &&
	grep checkpoint dmesgfiles.out | grep ${today}
'

test_done
